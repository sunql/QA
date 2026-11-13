"""回填 Milvus 本体向量（一次性数据修复）。

背景：本体向量残缺——类向量有、绝大多数属性向量缺失（历史 490 个种子属性从未建
向量），`_selectRelevantClasses` 按类/属性文本向量召回，属性向量缺失会让基于属性
语义的检索失灵。本脚本为本体类**及其属性**生成向量并写入 Milvus。

文本构造（与运行时 `/ontology/embeddings/sync` 及 apply_price_ontology.py 一致）：
- 类：`" ".join(class_name, class_alias, description)`
- 属性：`" ".join(property_name, *business_aliases, description)`（中文名+业务别名+说明）
- 指标：**复用** `app.services.ontology_service._metricEmbeddingText`
- KPI：**复用** `app.services.kpi_vector_graph_sync.kpiEmbeddingText`

密钥来源（只读，不写入代码）：与 app 运行配置一致——`Settings` 从进程环境变量及 cwd 的 `.env`
读取，embedding 取 EMBEDDING_API_KEY 或 OPENAI_API_KEY（见 app/config.py）；显式配置 base_url 时用
占位 key（keyless 本地服务）。Milvus 默认连 localhost:19530。注意：不要加载 ../docker/.env——
那是容器内配置，其 DATABASE_URL 指向 postgres 主机，本地无法解析。

幂等：syncEmbedding 按 ontology_id 先删后插，重复运行安全。

用法：
    EMBEDDING_API_KEY=... uv run python scripts/backfill_milvus_embeddings.py [--dry-run]
    uv run python scripts/backfill_milvus_embeddings.py --sources PPRICCONF,PPRICLIST
    uv run python scripts/backfill_milvus_embeddings.py --skip-classes
    uv run python scripts/backfill_milvus_embeddings.py --cleanup

[--dry-run] 只验证全链路（连库、取类/属性/指标/KPI、embedding 生成），不写入 Milvus；
注意 dry-run 仍会调用 embedding API 产生费用。
[--sources A,B] 只处理 source_table 在列表中的类（含其属性）；缺省为全部类。
[--skip-classes] 只回填属性，跳过类的 embedding（类向量已就绪时省调用）。
[--cleanup] 确定性收敛：以 PG 为唯一真源，把集合重建为「去重后 PG 全量」。
**全量 = class + property + metric + kpi 四类**（metric 全表；KPI 仅 PUBLISHED）。
Milvus 批量 delete-then-insert 在重负载下不可靠，故 cleanup 改为 读全量 -> 按
(ontology_id, type) 去重（保留最新）-> 删集重建 -> 一次整批插入，并补回 PG 有而
Milvus 缺失的向量。与普通回填互斥。

⚠️ **四类都必须参与**：rebuildOntologyCollections() drop 的是 **4** 个集合
（class / property / metric / kpi_catalog）。若 cleanup 的期望集漏掉 metric 或
kpi ⇒ 跑一次 --cleanup 就把这两类向量**清空**（且脚本照样打印「已收敛」⇒ 静默
数据丢失）。这是本脚本 2026-11 修复的核心雷。

注意（2026-09-30 修复）：删集重建走 `milvus.rebuildOntologyCollections()`。此前调的是
`milvus.dropCollection()` / `ensureCollection()` —— Task 14 丢弃旧 ontology_embeddings 后二者
已成 **no-op**，cleanup 静默退化为纯 append（每跑一次就多一整套向量，检索 topK 窗口被重复项
吃光）⇒ 改动前跑过 cleanup 的库**必须**再跑一次收敛。
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyProperty,
)
from app.infrastructure import milvus_client as milvus
from app.infrastructure.database import getSessionFactory
from app.services.embedding_service import EmbeddingService
from app.services.id_mapping_service import IdMappingService
from app.services.kpi_vector_graph_sync import kpiEmbeddingText, syncKpiVectorNow
from app.services.ontology_service import OntologyService, _metricEmbeddingText

# 4 个 type-routed 向量集合（milvus_client re-export 的常量与字段定义）
_ALL_TYPES = ("class", "property", "metric", "kpi")
_PUBLISHED_STATUS = "PUBLISHED"

# id_mapping 的 business_object（与 knowledge_compiler_service._TYPE_TO_BUSINESS_OBJECT 同口径）
_TYPE_TO_BUSINESS_OBJECT = {
    "class": "CLASS",
    "property": "PROPERTY",
    "metric": "METRIC",
}


def _parseArgs(
    argv: list[str],
) -> tuple[bool, set[str] | None, bool, bool]:
    dryRun = "--dry-run" in argv
    skipClasses = "--skip-classes" in argv
    cleanup = "--cleanup" in argv
    sources: set[str] | None = None
    if "--sources" in argv:
        idx = argv.index("--sources")
        if idx + 1 >= len(argv):
            raise SystemExit("用法错误：--sources 需要参数（逗号分隔的 source_table 列表）")
        raw = argv[idx + 1]
        sources = {s.strip() for s in raw.split(",") if s.strip()}
    if cleanup and (sources or skipClasses):
        raise SystemExit("用法错误：--cleanup 与 --sources/--skip-classes 互斥")
    return dryRun, sources, skipClasses, cleanup


def _classText(cls: OntologyClass) -> str:
    return " ".join(x for x in (cls.class_name, cls.class_alias, cls.description) if x)


def _propertyText(prop: OntologyProperty) -> str:
    return " ".join(
        filter(
            None,
            [
                prop.property_name,
                *(prop.business_aliases or []),
                prop.description or "",
            ],
        )
    )


def _rowDetails(row: dict) -> tuple[str, str, str]:
    """Milvus 行的 (name, alias, description)；None 归一为 ""（与 PG 比对口径一致）。"""
    return (row.get("name") or "", row.get("alias") or "", row.get("description") or "")


def _embeddingTextFor(typ: str, entity: object, metricAlias: str | None = None) -> str:
    """按 type 分派到**既有**文本构造函数（复用，不复制口径）。

    class/property 用本脚本既有实现；metric/kpi 复用服务层函数 ⇒ 重建出来的向量
    与运行时钩子写出的向量**同口径**。
    """
    if typ == "class":
        return _classText(entity)  # type: ignore[arg-type]
    if typ == "property":
        return _propertyText(entity)  # type: ignore[arg-type]
    if typ == "metric":
        return _metricEmbeddingText(entity)  # type: ignore[arg-type]
    if typ == "kpi":
        return kpiEmbeddingText(entity, metricAlias)  # type: ignore[arg-type]
    raise ValueError(f"未知向量类型: {typ!r}")


def _detailsTriple(detail: dict) -> tuple[str, str, str]:
    """PG 详情 → (name, alias, description)；缺失/None 归一为 ""（与 _rowDetails 同口径）。"""
    return (detail.get("name") or "", detail.get("alias") or "", detail.get("description") or "")


# PG 期望集


def _classDetail(cls: OntologyClass) -> dict:
    return {"id": cls.id, "name": cls.class_name, "alias": cls.class_alias or "",
            "description": cls.description or ""}


def _propertyDetail(prop: OntologyProperty) -> dict:
    return {"id": prop.id, "name": prop.property_name, "alias": prop.property_alias or "",
            "description": prop.description or ""}


def _metricDetail(metric: OntologyMetric) -> dict:
    """指标详情：内容三元组 = (metric_name, metric_alias, agg_function)。"""
    return {"id": metric.id, "name": metric.metric_name, "alias": metric.metric_alias or "",
            "description": metric.agg_function or "", "metric_name": metric.metric_name,
            "metric_alias": metric.metric_alias or "", "agg_function": metric.agg_function or ""}


def _kpiDetail(kpi: KpiCatalog, metricAlias: str | None) -> dict:
    """KPI 详情：内容三元组 = (kpi_name, 挂靠 metric 的 metric_alias, business_definition)。"""
    return {"id": kpi.id, "name": kpi.kpi_name, "alias": metricAlias or "",
            "description": kpi.business_definition or "", "kpi_name": kpi.kpi_name,
            "metricAlias": metricAlias or "", "business_definition": kpi.business_definition or "",
            "kpi_code": kpi.kpi_code}


async def _publishedKpisWithMetricAlias(
    session: AsyncSession,
) -> list[tuple[KpiCatalog, str | None]]:
    """PUBLISHED 的 KPI + 挂靠 metric 的别名（outerjoin：metric 缺失/无别名 ⇒ None）。"""
    stmt = (
        select(KpiCatalog, OntologyMetric.metric_alias)
        .outerjoin(OntologyMetric, OntologyMetric.id == KpiCatalog.metric_id)
        .where(KpiCatalog.status == _PUBLISHED_STATUS)
        .order_by(KpiCatalog.id)
    )
    rows = (await session.execute(stmt)).all()
    return [(row[0], row[1]) for row in rows]


def _intKeyedMap(raw: dict[str, str], typ: str) -> dict[int, str]:
    """``{external_id(str): unified_id}`` → ``{pg_id(int): unified_id}``。

    entityMaps 要的是整数键（pg_id），mapExternalIds 给的是字符串键 ⇒ 必须显式
    转换。转换是安全的：CLASS/PROPERTY/METRIC 的 external_id 就是 str(pg_id)。
    非数字 external_id ⇒ **该行不参与本次映射**，并**计数上报**（不静默吞掉）。
    """
    mapped: dict[int, str] = {}
    skipped: list[str] = []
    for externalId, unifiedId in raw.items():
        try:
            pgId = int(externalId)
        except (TypeError, ValueError):
            skipped.append(str(externalId))
            continue
        mapped[pgId] = unifiedId
    if skipped:
        print(
            f"  WARN id_mapping[{typ}] 非数字 external_id 跳过 {len(skipped)} 条: "
            f"{skipped[:10]}"
        )
    return mapped


async def _entityMapsFromPg(session: AsyncSession) -> dict[str, dict[int, str]]:
    """三类本体对象的 unified_id 映射：{class|property|metric: {pg_id(int): unified_id}}。

    KPI **不在**其中（KPI 的 external_id 固定 = kpi_code，不走 id_mapping）。
    """
    idMappingSvc = IdMappingService()
    maps: dict[str, dict[int, str]] = {}
    for typ, businessObject in _TYPE_TO_BUSINESS_OBJECT.items():
        raw = await idMappingSvc.mapExternalIds(session, businessObject)
        maps[typ] = _intKeyedMap(raw, typ)
    return maps


async def _expectedFromPg(
    session: AsyncSession,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], dict[str, dict[int, str]]]:
    """PG 期望集（三件套，PG 是唯一真源）。

    - ``expected``：``{type: [实体记录 dict]}``（class/property/metric/kpi）；每条含
      ``"id"`` 与 ORM 实体（``"entity"``）与 KPI 的 ``"metricAlias"``。
    - ``expectedDetails``：``{type: [详情 dict]}``；每条含 ``id``/``name``/``alias``/
      ``description``（内容三元组，None 归一 ""）+ 原始字段名（metric_*/kpi_*）。
    - ``entityMaps``：**只有** class/property/metric 三键 ⇒ ``{pg_id(int): unified_id}``。

    口径（D3）：metric 期望集 = **OntologyMetric 全表**（维持现状 ⇒ DRAFT 也建）；
    KPI 期望集 = ``status == "PUBLISHED"``。
    """
    svc = OntologyService()
    classes = await svc.listClasses(session)
    properties: list[OntologyProperty] = []
    for cls in classes:
        properties.extend(await svc.listPropertiesByClass(session, cls.id))
    metrics = await svc.listMetrics(session)
    kpiRows = await _publishedKpisWithMetricAlias(session)

    expected: dict[str, list[dict]] = {
        "class": [{"id": c.id, "entity": c} for c in classes],
        "property": [{"id": p.id, "entity": p} for p in properties],
        "metric": [{"id": m.id, "entity": m} for m in metrics],
        "kpi": [{"id": k.id, "entity": k, "metricAlias": alias} for k, alias in kpiRows],
    }
    details: dict[str, list[dict]] = {
        "class": [_classDetail(c) for c in classes],
        "property": [_propertyDetail(p) for p in properties],
        "metric": [_metricDetail(m) for m in metrics],
        "kpi": [_kpiDetail(k, alias) for k, alias in kpiRows],
    }
    entityMaps = await _entityMapsFromPg(session)
    return expected, details, entityMaps


# ---------------------------------------------------------------------------
# (oid,type) 索引适配器：把 _expectedFromPg 的「按 type 分组」新形状转成
# _cleanup 内部用的 (oid,type) 索引。两套形状各自独立，不混在同一个变量里。
# ---------------------------------------------------------------------------


def _expectedKeySet(expected: dict[str, list[dict]]) -> set[tuple[int, str]]:
    return {(rec["id"], typ) for typ, recs in expected.items() for rec in recs}


def _entityIndex(expected: dict[str, list[dict]]) -> dict[tuple[int, str], dict]:
    return {(rec["id"], typ): rec for typ, recs in expected.items() for rec in recs}


def _detailsIndex(details: dict[str, list[dict]]) -> dict[tuple[int, str], dict]:
    return {(rec["id"], typ): rec for typ, recs in details.items() for rec in recs}


def _printExpectedCounts(expected: dict[str, list[dict]], expectedKeys: set) -> None:
    parts = " + ".join(f"{len(recs)} {typ}" for typ, recs in expected.items())
    print(f"PG 期望: {len(expectedKeys)} 个实体 = {parts}")


# Milvus 读取

_READ_PAGE = 16384


def _readAllRowsWithVectors() -> list[dict]:
    """读 **4** 个向量集合全量，**含 embedding 向量**与 name/alias/description。

    为什么不能用 milvus.listAllEmbeddings()：那条路刻意只投影元数据、不取 1024 维
    向量。但 cleanup 的收敛策略是「读全量 → 去重 → 删集重建 → 写回」，向量必须
    读得出来才写得回去（此前用元数据读取的产物去重建，在 drop 之后以
    `KeyError: 'alias'` / `'embedding'` 崩溃 —— 集合被清空、脚本半途死掉）。

    ⚠️ 集合清单必须与 `rebuildOntologyCollections()` 的 drop 清单**一致**（4 个）：
    少读一个 ⇒ 该类向量在 rebuild 后无法写回 ⇒ 被清空。
    """
    rows: list[dict] = []
    for name, fields in (
        (milvus._CLASS_COLLECTION_NAME, milvus._classFields),
        (milvus._PROPERTY_COLLECTION_NAME, milvus._propertyFields),
        (milvus._METRIC_COLLECTION_NAME, milvus._metricFields),
        (milvus._KPI_COLLECTION_NAME, milvus._kpiFields),
    ):
        collection = milvus._ensureCollection(name, fields())
        collection.load()
        page = collection.query(
            expr="id >= 0",
            output_fields=[
                "id", "ontology_id", "type", "name", "alias", "description",
                "external_id", "embedding",
            ],
            limit=_READ_PAGE,
        )
        if len(page) >= _READ_PAGE:
            raise SystemExit(f"{name}: 命中分页上限 {_READ_PAGE}，需改用 query_iterator")
        rows.extend(page)
    return rows


# 去重 / 写回记录


def _dedupeRows(
    rows: list[dict], expectedKeys: set[tuple[int, str]]
) -> dict[tuple[int, str], dict]:
    """按 (ontology_id, type) 保留 id 最大的一行，**且只保留 expectedKeys 里的 key**。

    ⚠️ 「只保留 expectedKeys」是 2026-11 修雷的关键：旧实现的 expected 只含
    class/property ⇒ metric / KPI 行在这一步被丢掉 ⇒ rebuild 后这两类向量被清空。
    """
    best: dict[tuple[int, str], dict] = {}
    for row in rows:
        key = (row["ontology_id"], row["type"])
        if key not in expectedKeys:
            continue
        current = best.get(key)
        if current is None or row["id"] > current["id"]:
            best[key] = row
    return best


def _externalIdFor(
    typ: str,
    oid: int,
    row: dict,
    detail: dict,
    entityMaps: dict[str, dict[int, str]],
) -> str:
    """external_id 的填充规则（rebuild 写回时）。

    - kpi：**固定** = kpi_code（从 PG 详情取，不从 Milvus 旧行取）。
    - class/property/metric：① 旧行 external_id 非空则沿用（读取保留值）
      → ② entityMaps[type][pg_id] → ③ 兜底 ""。

    ⚠️ external_id 缺失**不会报错**：milvus_dual_write._insertIntoNewCollection 用
    `r.get("external_id") or ""` 填充该列 ⇒ 不带就一律变 ""（静默丢值）。
    """
    if typ == "kpi":
        return (detail.get("kpi_code") or "").strip()
    kept = (row.get("external_id") or "").strip()
    if kept:
        return kept
    return entityMaps.get(typ, {}).get(oid, "")


def _buildWriteBackRecords(
    deduped: dict[tuple[int, str], dict],
    detailsByKey: dict[tuple[int, str], dict],
    entityMaps: dict[str, dict[int, str]],
) -> list[dict]:
    """输出最终 insert 的 record 列表。

    字段与 `milvus_dual_write._insertIntoNewCollection` 的 data 列**同序同义**：
    ontology_id / type / name / alias / description / embedding / **external_id**。
    """
    records: list[dict] = []
    for (oid, typ), row in deduped.items():
        detail = detailsByKey.get((oid, typ), {})
        records.append({
            "ontology_id": oid,
            "type": typ,
            "name": row.get("name") or "",
            "alias": row.get("alias") or "",
            "description": row.get("description") or "",
            "embedding": row.get("embedding"),
            "external_id": _externalIdFor(typ, oid, row, detail, entityMaps),
        })
    return records


# 普通回填（非 cleanup）


async def _backfillClass(
    session: AsyncSession,
    svc: OntologyService,
    embedding: EmbeddingService,
    cls: OntologyClass,
    *,
    dryRun: bool,
    skipClasses: bool,
) -> tuple[int, int]:
    """回填一个类及其属性；返回 (成功数, 失败数)。"""
    synced = failed = 0
    tag = f"{cls.class_name} [{cls.source_table}]"
    if not skipClasses:
        try:
            vec = await embedding.generateEmbedding(_classText(cls))
            if not dryRun:
                svc.syncEmbedding(
                    ontologyId=cls.id,
                    type="class",
                    name=cls.class_name,
                    alias=cls.class_alias,
                    description=cls.description,
                    embedding=vec,
                )
            synced += 1
            print(f"  ok class id={cls.id} {tag} dim={len(vec)}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL class id={cls.id} {tag}: {exc}")

    props = await svc.listPropertiesByClass(session, cls.id)
    for prop in props:
        try:
            vec = await embedding.generateEmbedding(_propertyText(prop))
            if not dryRun:
                svc.syncEmbedding(
                    ontologyId=prop.id,
                    type="property",
                    name=prop.property_name,
                    alias=prop.property_alias,
                    description=prop.description,
                    embedding=vec,
                )
            synced += 1
            print(f"  ok prop id={prop.id} {cls.class_name}.{prop.property_name} dim={len(vec)}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL prop id={prop.id} {cls.class_name}.{prop.property_name}: {exc}")
    return synced, failed


async def _backfillMetrics(
    session: AsyncSession,
    svc: OntologyService,
    embedding: EmbeddingService,
    dryRun: bool,
) -> tuple[int, int]:
    """指标向量回填（D3：metric 维持现状 ⇒ DRAFT 也建，期望集 = 全表）。

    文本复用 `_metricEmbeddingText`（与运行时 `_syncMetricEmbeddingNow` 同口径）；
    字段口径 name=metric_name / alias=metric_alias / description=agg_function。
    """
    synced = failed = 0
    for metric in await svc.listMetrics(session):
        try:
            vec = await embedding.generateEmbedding(_metricEmbeddingText(metric))
            if not dryRun:
                svc.syncEmbedding(
                    ontologyId=metric.id,
                    type="metric",
                    name=metric.metric_name,
                    alias=metric.metric_alias,
                    description=metric.agg_function,
                    embedding=vec,
                )
            synced += 1
            print(f"  ok metric id={metric.id} {metric.metric_name} dim={len(vec)}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL metric id={metric.id} {metric.metric_name}: {exc}")
    return synced, failed


async def _backfillKpis(
    session: AsyncSession,
    embedding: EmbeddingService,
    dryRun: bool,
) -> tuple[int, int]:
    """KPI 目录向量回填（D3：**仅 PUBLISHED**）。

    非 dry-run 走 `syncKpiVectorNow`（**复用**：自己删+插、持有 Milvus 写锁、
    external_id=kpi_code）；dry-run 只验证取数与 embedding 生成，不写 Milvus。
    """
    synced = failed = 0
    for kpi, metricAlias in await _publishedKpisWithMetricAlias(session):
        try:
            if dryRun:
                vec = await embedding.generateEmbedding(kpiEmbeddingText(kpi, metricAlias))
                print(f"  ok kpi id={kpi.id} {kpi.kpi_name} dim={len(vec)} [dry-run]")
            else:
                await syncKpiVectorNow(session, kpi.id)
                print(f"  ok kpi id={kpi.id} {kpi.kpi_name}")
            synced += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL kpi id={kpi.id} {kpi.kpi_name}: {exc}")
    return synced, failed


async def _backfill(
    dryRun: bool, sources: set[str] | None, skipClasses: bool
) -> int:
    svc = OntologyService()
    embedding = EmbeddingService()
    factory = getSessionFactory()
    synced = failed = 0

    async with factory() as session:
        classes = await svc.listClasses(session)
        for cls in classes:
            if sources and cls.source_table not in sources:
                continue
            classSynced, classFailed = await _backfillClass(
                session, svc, embedding, cls,
                dryRun=dryRun, skipClasses=skipClasses,
            )
            synced += classSynced
            failed += classFailed
        metricSynced, metricFailed = await _backfillMetrics(
            session, svc, embedding, dryRun
        )
        kpiSynced, kpiFailed = await _backfillKpis(session, embedding, dryRun)
        synced += metricSynced + kpiSynced
        failed += metricFailed + kpiFailed

    suffix = " [dry-run]" if dryRun else ""
    if sources:
        suffix += f" [sources={','.join(sorted(sources))}]"
    if skipClasses:
        suffix += " [skip-classes]"
    print(f"Milvus 本体向量回填: {synced} 成功 / {failed} 失败{suffix}")
    return failed


# cleanup（确定性收敛）


async def _generateRecordsForKeys(
    keys: list[tuple[int, str]],
    entityByKey: dict[tuple[int, str], dict],
    detailsByKey: dict[tuple[int, str], dict],
    entityMaps: dict[str, dict[int, str]],
    embedding: EmbeddingService,
    *,
    tag: str,
    oldRows: dict[tuple[int, str], dict] | None = None,
) -> tuple[list[dict], int]:
    """为给定 key 生成重建用的 record；返回 (records, 失败数)。

    name/alias/description 取 **PG 详情**（唯一真源）；
    external_id 按 `_externalIdFor` 的规则取值（KPI=kpi_code；本体行=旧行保留值 →
    entityMaps → ""）。
    """
    records: list[dict] = []
    failed = 0
    for oid, typ in keys:
        rec = entityByKey.get((oid, typ))
        detail = detailsByKey.get((oid, typ))
        if rec is None or detail is None:
            failed += 1
            print(f"  FAIL {tag} 找不到 PG 实体 type={typ} oid={oid}")
            continue
        try:
            text = _embeddingTextFor(typ, rec["entity"], rec.get("metricAlias"))
            vec = await embedding.generateEmbedding(text)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {tag} 重生成 oid={oid} type={typ}: {exc}")
            continue
        name, alias, description = _detailsTriple(detail)
        oldRow = (oldRows or {}).get((oid, typ), {})
        records.append({
            "ontology_id": oid,
            "type": typ,
            "name": name,
            "alias": alias,
            "description": description,
            "embedding": vec,
            "external_id": _externalIdFor(typ, oid, oldRow, detail, entityMaps),
        })
        print(f"  ok {tag} oid={oid} type={typ} {name} dim={len(vec)}")
    return records, failed


def _applyMissingRecords(rows: list[dict], records: list[dict], dryRun: bool) -> list[dict]:
    """整批插入补缺失的向量（单次 flush），返回最新的 Milvus 行。"""
    if not records:
        return rows
    if dryRun:
        # 不落库 ⇒ 本地补一份（id 用 inf：生成的记录没有 Milvus auto_id，
        # 去重时按「最新」处理）。
        return rows + [{"id": float("inf"), **r} for r in records]
    milvus.insertEmbeddings(records)
    print(f"  整批插入 {len(records)} 条（单次 flush）")
    newRows = _readAllRowsWithVectors()
    print(f"补缺失后 Milvus: {len(newRows)} 行")
    return newRows


def _applyStaleRecords(
    deduped: dict[tuple[int, str], dict],
    records: list[dict],
    dryRun: bool,
) -> dict[tuple[int, str], dict]:
    """内容过期重生成后的批量替换；返回更新后的 (oid,type) 索引。"""
    if not records:
        return deduped
    if not dryRun:
        for typ in _ALL_TYPES:
            ids = [r["ontology_id"] for r in records if r["type"] == typ]
            if not ids:
                continue
            # no-op shim（milvus_client.deleteByOntologyIds 是 no-op）；rebuild 步会
            # 重写一切，故此处不删也不影响最终收敛（不改为 dual delete，避免双删风暴）。
            milvus.deleteByOntologyIds(ids, typ)
        milvus.insertEmbeddings(records)
        print(f"  过期批量替换 {len(records)} 条（批量 delete + 整批 insert）")
    updated = dict(deduped)
    for r in records:
        updated[(r["ontology_id"], r["type"])] = {"id": float("inf"), **r}
    return updated


def _staleKeys(
    deduped: dict[tuple[int, str], dict],
    detailsByKey: dict[tuple[int, str], dict],
) -> list[tuple[int, str]]:
    """内容过期的 key：name/alias/description 与 PG 当前值不一致（历史 id 漂移遗留）。"""
    return sorted(
        key
        for key, row in deduped.items()
        if key in detailsByKey and _rowDetails(row) != _detailsTriple(detailsByKey[key])
    )


def _verifyConvergence(
    expectedKeys: set[tuple[int, str]],
    detailsByKey: dict[tuple[int, str], dict],
) -> int:
    """重建后校验：无重复、无缺失、无陈旧 key、内容不过期；返回失败数。"""
    finalRows = milvus.listAllEmbeddings()
    finalKeys = {(r["ontology_id"], r["type"]) for r in finalRows}
    stillMissing = sorted(expectedKeys - finalKeys)
    stale = sorted(finalKeys - expectedKeys)
    hasDuplicates = len(finalRows) != len(finalKeys)
    contentStale = sorted(
        (r["ontology_id"], r["type"])
        for r in finalRows
        if (r["ontology_id"], r["type"]) in detailsByKey
        and _rowDetails(r) != _detailsTriple(detailsByKey[(r["ontology_id"], r["type"])])
    )
    print(f"重建后: {len(finalRows)} 行（期望 {len(expectedKeys)}）")
    failed = 0
    if hasDuplicates:
        failed += 1
        print(f"  FAIL 存在重复: {len(finalRows) - len(finalKeys)} 对")
    if stillMissing:
        failed += 1
        print(f"  FAIL 仍缺失: {stillMissing}")
    if stale:
        failed += 1
        print(f"  FAIL 残留陈旧: {stale}")
    if contentStale:
        failed += 1
        print(f"  FAIL 内容过期: {contentStale}")
    return failed


async def _reconcileMissing(
    rows: list[dict],
    expectedKeys: set[tuple[int, str]],
    entityByKey: dict[tuple[int, str], dict],
    detailsByKey: dict[tuple[int, str], dict],
    entityMaps: dict[str, dict[int, str]],
    embedding: EmbeddingService,
    dryRun: bool,
) -> tuple[list[dict], int]:
    """补缺失（PG 有而 Milvus 无；如被手删的 oid=1 属性）→ 整批插入。

    先批量生成向量再一次性整批插入：逐条 syncEmbedding（delete+insert+flush）
    在当前 Milvus 部署下单条可达 10-25s，批量场景必须整批一次 flush。
    """
    present = {(r["ontology_id"], r["type"]) for r in rows}
    missingKeys = sorted(expectedKeys - present)
    if not missingKeys:
        return rows, 0
    print(f"补缺失 {len(missingKeys)} 个实体: {missingKeys}")
    records, failed = await _generateRecordsForKeys(
        missingKeys, entityByKey, detailsByKey, entityMaps, embedding,
        tag="重生成",
    )
    return _applyMissingRecords(rows, records, dryRun), failed


async def _reconcileStale(
    deduped: dict[tuple[int, str], dict],
    entityByKey: dict[tuple[int, str], dict],
    detailsByKey: dict[tuple[int, str], dict],
    entityMaps: dict[str, dict[int, str]],
    embedding: EmbeddingService,
    dryRun: bool,
) -> tuple[dict[tuple[int, str], dict], int]:
    """内容过期（name/alias/description 与 PG 不一致）→ 重生成并批量替换。"""
    staleKeys = _staleKeys(deduped, detailsByKey)
    if not staleKeys:
        return deduped, 0
    print(f"内容过期 {len(staleKeys)} 条，重生成: {staleKeys}")
    records, failed = await _generateRecordsForKeys(
        staleKeys, entityByKey, detailsByKey, entityMaps, embedding,
        tag="过期重生成", oldRows=deduped,
    )
    return _applyStaleRecords(deduped, records, dryRun), failed


def _rebuildAndVerify(
    deduped: dict[tuple[int, str], dict],
    detailsByKey: dict[tuple[int, str], dict],
    entityMaps: dict[str, dict[int, str]],
    expectedKeys: set[tuple[int, str]],
) -> int:
    """删集重建 + 整批插入 + 收敛校验；返回失败数。

    写入的 record 由 `_buildWriteBackRecords` 生成（**带 external_id**）。
    """
    milvus.rebuildOntologyCollections()
    milvus.insertEmbeddings(_buildWriteBackRecords(deduped, detailsByKey, entityMaps))
    failed = _verifyConvergence(expectedKeys, detailsByKey)
    if not failed:
        print("Milvus 向量集合已收敛（无重复、无缺失、无陈旧 key、内容与 PG 一致）。")
    return failed


async def _cleanup(dryRun: bool) -> int:
    """确定性收敛 4 个向量集合：读全量 -> 去重 -> 补缺失 -> 删集重建 -> 整批插入。

    ⚠️ **metric / kpi 均参与**（2026-11 修雷的核心）：4 类在 rebuild 段写回，
    `_cleanup` 一跑不再清空 metric / KPI 向量；期望集漏一类 ⇒ 该类向量被 rebuild
    清空且脚本照样打印「已收敛」⇒ 静默数据丢失。
    """
    embedding = EmbeddingService()
    factory = getSessionFactory()
    failed = 0

    async with factory() as session:
        expected, details, entityMaps = await _expectedFromPg(session)

    expectedKeys = _expectedKeySet(expected)
    entityByKey = _entityIndex(expected)
    detailsByKey = _detailsIndex(details)
    _printExpectedCounts(expected, expectedKeys)

    rows = _readAllRowsWithVectors()
    print(f"Milvus 当前: {len(rows)} 行")

    # 1) 补缺失（PG 有而 Milvus 无）
    rows, missingFailed = await _reconcileMissing(
        rows, expectedKeys, entityByKey, detailsByKey, entityMaps, embedding, dryRun,
    )
    failed += missingFailed

    # 2) 按 (oid,type) 去重保留最新（auto_id 最大），并剔除 PG 之外的陈旧向量
    deduped = _dedupeRows(rows, expectedKeys)

    # 2.5) 内容过期检测：name/alias/description 与 PG 不一致（历史 id 漂移遗留）→ 重生成。
    deduped, staleFailed = await _reconcileStale(
        deduped, entityByKey, detailsByKey, entityMaps, embedding, dryRun,
    )
    failed += staleFailed

    counts = " + ".join(
        f"{sum(1 for r in deduped.values() if r['type'] == typ)} {typ}"
        for typ in _ALL_TYPES
    )
    print(f"去重后: {len(deduped)} = {counts}")

    # 3) 删集重建 + 整批插入（丢弃不可靠的批量 delete 路径）+ 校验
    if dryRun:
        print("[dry-run] 未删集重建")
        return failed
    failed += _rebuildAndVerify(deduped, detailsByKey, entityMaps, expectedKeys)
    return failed


async def main() -> None:
    dryRun, sources, skipClasses, cleanup = _parseArgs(sys.argv)
    if cleanup:
        failed = await _cleanup(dryRun=dryRun)
    else:
        failed = await _backfill(dryRun=dryRun, sources=sources, skipClasses=skipClasses)
    if failed > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
