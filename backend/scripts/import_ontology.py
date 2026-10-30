"""本体元数据导入：ontology_export.json → 生产环境（export_ontology.py 的对偶）。

与 export_ontology.py 配对使用。导入走 OntologyService / BusinessObjectService /
KpiCatalogService 完整链路（audit + Neo4j + Milvus best-effort 同步），**不直写 PG**。

幂等 upsert 语义（可重复执行）：
- 按自然键查现有行：存在 → 逐字段比对，仅当导出值非 None 且与现值不同才 update；
  导出值 None 表示「保留现值不覆盖」（支持部分元数据下发）
- 不存在 → create
- 端点类缺失（join/relation/KPI 引用了生产没有的类）→ 记入 skipped 继续，不中断

导入顺序（依赖序）：类 → 属性 → 业务对象 → 度量 → JOIN → 语义关系 → KPI。
跨环境 id 不可通约，全部按自然键重映射（class_name / metric_name /
kpi_code / business_object.code / data_source.name）。

导入完成后仍需两步对账（脚本结尾会再次提示）：
1. 本体入图：本体管理 → 批量关系引擎 syncGraph（或 POST /ontology/graph/sync-missing）
2. 向量重建：python -m scripts.backfill_milvus_embeddings --cleanup

用法（生产 qa-backend 容器内）：
    docker exec -i qa-backend python -m scripts.import_ontology --dry-run < ontology_export.json
    docker exec -i qa-backend python -m scripts.import_ontology < ontology_export.json
    # 或先 docker cp 进容器后传文件路径
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
if env.exists():  # 宿主机布局；容器内 SECRET_KEY 已由 compose 注入环境
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("SECRET_KEY="):
            os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from sqlalchemy import select  # noqa: E402

from app.dependencies import CurrentUser  # noqa: E402
from app.domain.models import (  # noqa: E402
    BusinessObject,
    DataSource,
    KpiCatalog,
    OntologyClass,
    OntologyJoin,
    OntologyMetric,
    OntologyProperty,
    OntologyRelation,
)
from app.domain.schemas import (  # noqa: E402
    BusinessObjectCreate,
    BusinessObjectUpdate,
    KpiCatalogCreate,
    KpiCatalogUpdate,
    OntologyClassCreate,
    OntologyClassUpdate,
    OntologyJoinCreate,
    OntologyJoinUpdate,
    OntologyMetricCreate,
    OntologyMetricUpdate,
    OntologyPropertyCreate,
    OntologyPropertyUpdate,
    OntologyRelationCreate,
)
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.acl_service import ADMIN_ROLE  # noqa: E402
from app.services.business_object_service import BusinessObjectService  # noqa: E402
from app.services.kpi_catalog_service import KpiCatalogService  # noqa: E402
from app.services.ontology_service import OntologyService, makeJoinKey  # noqa: E402

ACTOR = CurrentUser(userId="ontology-migrate", roles=(ADMIN_ROLE,))


def _newSection() -> dict[str, list]:
    return {"created": [], "updated": [], "skipped": [], "failed": []}


class ImportContext:
    """导入运行的共享状态：服务句柄 + 自然键 → 生产 id 映射 + 摘要。"""

    def __init__(self) -> None:
        self.ontology = OntologyService()
        self.businessObjects = BusinessObjectService()
        self.kpiCatalog = KpiCatalogService()
        self.summary: dict[str, dict[str, list]] = {
            key: _newSection()
            for key in ("classes", "properties", "businessObjects", "metrics",
                        "joins", "relations", "kpis")
        }

    def record(self, section: str, outcome: str, key: str, detail: str = "") -> None:
        bucket = self.summary[section][outcome]
        bucket.append(f"{key}{('：' + detail) if detail else ''}")


async def _loadClassMap(session) -> dict[str, OntologyClass]:
    """当前版本（valid_to IS NULL）类名 → 行。含本轮导入前已存在的全部类。"""
    rows = (
        await session.execute(
            select(OntologyClass).where(OntologyClass.valid_to.is_(None))
        )
    ).scalars().all()
    return {c.class_name: c for c in rows}


async def importClasses(ctx: ImportContext, session, payload: dict) -> dict[str, OntologyClass]:
    """Pass 1：类本体字段（不含 parent，Pass 2 补）。返回类名 → 生产行映射。"""
    classMap = await _loadClassMap(session)
    for item in payload.get("classes", []):
        name = item.get("className") or ""
        if not name:
            ctx.record("classes", "failed", "<空类名>", "导出项缺 className")
            continue
        desired = {
            field: item.get(dtoField)
            for field, dtoField in (
                ("class_alias", "classAlias"),
                ("description", "description"),
                ("source_table", "sourceTable"),
                ("object_type", "objectType"),
            )
            if item.get(dtoField) is not None
        }
        try:
            existing = classMap.get(name)
            if existing is None:
                created = await ctx.ontology.createClass(
                    session,
                    OntologyClassCreate(class_name=name, **desired),
                    actor=ACTOR.userId,
                )
                classMap[name] = created
                ctx.record("classes", "created", name)
            else:
                changed = {
                    field: value
                    for field, value in desired.items()
                    if getattr(existing, field) != value
                }
                if changed:
                    await ctx.ontology.updateClass(
                        session, existing.id, OntologyClassUpdate(**changed), actor=ACTOR
                    )
                    ctx.record(
                        "classes", "updated", name, f"字段={sorted(changed)}"
                    )
                else:
                    ctx.record("classes", "skipped", name, "已一致")
        except Exception as exc:  # noqa: BLE001 — 单类失败不阻断整批
            ctx.record("classes", "failed", name, str(exc))

    # Pass 2：parent 引用（全部类就位后才能解析 parentClassName）
    for item in payload.get("classes", []):
        parentName = item.get("parentClassName")
        if not parentName:
            continue
        name = item.get("className") or ""
        row = classMap.get(name)
        parent = classMap.get(parentName)
        if row is None or parent is None:
            if name:
                ctx.record(
                    "classes", "skipped", name, f"父类 {parentName} 不存在，跳过继承设置"
                )
            continue
        if row.parent_class_id == parent.id:
            continue
        try:
            await ctx.ontology.updateClass(
                session,
                row.id,
                OntologyClassUpdate(parent_class_id=parent.id),
                actor=ACTOR,
            )
            ctx.record("classes", "updated", name, f"parent→{parentName}")
        except Exception as exc:  # noqa: BLE001
            ctx.record("classes", "failed", name, f"parent 设置失败：{exc}")
    return classMap


async def importProperties(ctx: ImportContext, session, payload: dict, classMap) -> None:
    existingRows = (
        await session.execute(select(OntologyProperty))
    ).scalars().all()
    byClassAndName: dict[tuple[int, str], OntologyProperty] = {
        (p.class_id, p.property_name): p for p in existingRows
    }
    for item in payload.get("properties", []):
        className = item.get("className") or ""
        propName = item.get("propertyName") or ""
        cls = classMap.get(className)
        if cls is None or not propName:
            ctx.record("properties", "skipped", f"{className}.{propName}", "类不存在")
            continue
        desired = {
            field: item.get(dtoField)
            for field, dtoField in (
                ("property_alias", "propertyAlias"),
                ("business_aliases", "businessAliases"),
                ("description", "description"),
                ("data_type", "dataType"),
                ("is_primary_key", "isPrimaryKey"),
                ("is_foreign_key", "isForeignKey"),
                ("source_column", "sourceColumn"),
            )
            if item.get(dtoField) is not None
        }
        if desired.get("data_type") is None and (cls.id, propName) not in byClassAndName:
            desired["data_type"] = "STRING"  # create 必填兜底（列上非空）
        refName = item.get("refClassName")
        if refName is not None:
            ref = classMap.get(refName)
            if ref is None:
                ctx.record(
                    "properties", "skipped", f"{className}.{propName}",
                    f"引用类 {refName} 不存在",
                )
                continue
            desired["ref_class_id"] = ref.id
        key = f"{className}.{propName}"
        try:
            existing = byClassAndName.get((cls.id, propName))
            if existing is None:
                await ctx.ontology.createProperty(
                    session,
                    OntologyPropertyCreate(
                        class_id=cls.id, property_name=propName, **desired
                    ),
                    actor=ACTOR.userId,
                )
                ctx.record("properties", "created", key)
            else:
                changed = {
                    field: value
                    for field, value in desired.items()
                    if getattr(existing, field) != value
                }
                if changed:
                    await ctx.ontology.updateProperty(
                        session, existing.id, OntologyPropertyUpdate(**changed),
                        actor=ACTOR.userId,
                    )
                    ctx.record("properties", "updated", key, f"字段={sorted(changed)}")
                else:
                    ctx.record("properties", "skipped", key, "已一致")
        except Exception as exc:  # noqa: BLE001
            ctx.record("properties", "failed", key, str(exc))


async def importBusinessObjects(ctx: ImportContext, session, payload: dict, classMap) -> None:
    for item in payload.get("businessObjects", []):
        code = item.get("code") or ""
        if not code:
            ctx.record("businessObjects", "failed", "<空 code>", "导出项缺 code")
            continue
        headerName = item.get("headerClassName")
        headerClassId = classMap[headerName].id if headerName in classMap else None
        if headerName and headerClassId is None:
            ctx.record("businessObjects", "skipped", code, f"头类 {headerName} 不存在")
            continue
        try:
            existing = await session.get(BusinessObject, code)
            if existing is None:
                await ctx.businessObjects.createObject(
                    session,
                    BusinessObjectCreate(
                        code=code,
                        name=item.get("name") or code,
                        header_class_id=headerClassId,
                        graph_label=item.get("graphLabel"),
                        description=item.get("description"),
                    ),
                    actor=ACTOR.userId,
                )
                ctx.record("businessObjects", "created", code)
            else:
                updates = {
                    field: value
                    for field, value in {
                        "name": item.get("name"),
                        "description": item.get("description"),
                    }.items()
                    if value is not None and getattr(existing, field) != value
                }
                if headerClassId is not None and existing.header_class_id != headerClassId:
                    updates["header_class_id"] = headerClassId
                    updates["graph_label"] = item.get("graphLabel")
                if updates:
                    await ctx.businessObjects.updateObject(
                        session, code, BusinessObjectUpdate(**updates)
                    )
                    ctx.record("businessObjects", "updated", code, f"字段={sorted(updates)}")
                else:
                    ctx.record("businessObjects", "skipped", code, "已一致")
        except Exception as exc:  # noqa: BLE001
            ctx.record("businessObjects", "failed", code, str(exc))


async def importMetrics(ctx: ImportContext, session, payload: dict, classMap) -> None:
    existingRows = (await session.execute(select(OntologyMetric))).scalars().all()
    byName = {m.metric_name: m for m in existingRows}
    for item in payload.get("metrics", []):
        name = item.get("metricName") or ""
        if not name:
            ctx.record("metrics", "failed", "<空指标名>", "导出项缺 metricName")
            continue
        desired = {
            field: item.get(dtoField)
            for field, dtoField in (
                ("metric_alias", "metricAlias"),
                ("formula", "formula"),
                ("agg_function", "aggFunction"),
                ("dimension_defaults", "dimensionDefaults"),
            )
            if item.get(dtoField) is not None
        }
        targetName = item.get("targetClassName")
        if targetName is not None:
            target = classMap.get(targetName)
            if target is None:
                ctx.record("metrics", "skipped", name, f"目标类 {targetName} 不存在")
                continue
            desired["target_class_id"] = target.id
        try:
            existing = byName.get(name)
            if existing is None:
                await ctx.ontology.createMetric(
                    session,
                    OntologyMetricCreate(metric_name=name, **desired),
                    actor=ACTOR.userId,
                )
                ctx.record("metrics", "created", name)
            else:
                changed = {
                    field: value
                    for field, value in desired.items()
                    if getattr(existing, field) != value
                }
                if changed:
                    await ctx.ontology.updateMetric(
                        session, existing.id, OntologyMetricUpdate(**changed),
                        actor=ACTOR.userId,
                    )
                    ctx.record("metrics", "updated", name, f"字段={sorted(changed)}")
                else:
                    ctx.record("metrics", "skipped", name, "已一致")
        except Exception as exc:  # noqa: BLE001
            ctx.record("metrics", "failed", name, str(exc))


async def importJoins(ctx: ImportContext, session, payload: dict, classMap) -> None:
    existingRows = (await session.execute(select(OntologyJoin))).scalars().all()
    existingKeys = {j.join_key for j in existingRows}
    existingByKey = {j.id: j for j in existingRows}
    for item in payload.get("joins", []):
        srcName = item.get("sourceClassName") or ""
        tgtName = item.get("targetClassName") or ""
        src = classMap.get(srcName)
        tgt = classMap.get(tgtName)
        key = f"{srcName}({','.join(item.get('sourceColumns') or [])})->" \
              f"{tgtName}({','.join(item.get('targetColumns') or [])})"
        if src is None or tgt is None:
            ctx.record("joins", "skipped", key, "端点类不存在")
            continue
        joinKey = makeJoinKey(
            src.id, item.get("sourceColumns") or [],
            tgt.id, item.get("targetColumns") or [],
        )
        try:
            if joinKey in existingKeys:
                row = next(j for j in existingByKey.values() if j.join_key == joinKey)
                desired = {
                    field: item.get(dtoField)
                    for field, dtoField in (
                        ("join_type", "joinType"),
                        ("relation_type", "relationType"),
                        ("description", "description"),
                    )
                    if item.get(dtoField) is not None
                }
                changed = {
                    field: value
                    for field, value in desired.items()
                    if getattr(row, field) != value
                }
                if changed:
                    await ctx.ontology.updateJoin(
                        session, row.id, OntologyJoinUpdate(**changed),
                        actor=ACTOR.userId,
                    )
                    ctx.record("joins", "updated", key, f"字段={sorted(changed)}")
                else:
                    ctx.record("joins", "skipped", key, "已一致")
            else:
                await ctx.ontology.createJoin(
                    session,
                    OntologyJoinCreate(
                        source_class_id=src.id,
                        source_columns=item.get("sourceColumns") or [],
                        target_class_id=tgt.id,
                        target_columns=item.get("targetColumns") or [],
                        join_type=item.get("joinType") or "INNER",
                        relation_type=item.get("relationType") or "business",
                        description=item.get("description"),
                    ),
                    actor=ACTOR.userId,
                )
                existingKeys.add(joinKey)
                ctx.record("joins", "created", key)
        except Exception as exc:  # noqa: BLE001
            ctx.record("joins", "failed", key, str(exc))


async def importRelations(ctx: ImportContext, session, payload: dict, classMap) -> None:
    """语义关系只有 create/delete API（无 update），三元组已存在即跳过。"""
    existingRows = (await session.execute(select(OntologyRelation))).scalars().all()
    existingTriples = {
        (r.source_class_id, r.target_class_id, r.relation_type) for r in existingRows
    }
    for item in payload.get("relations", []):
        srcName = item.get("sourceClassName") or ""
        tgtName = item.get("targetClassName") or ""
        relType = item.get("relationType") or ""
        key = f"{srcName}-[{relType}]->{tgtName}"
        src = classMap.get(srcName)
        tgt = classMap.get(tgtName)
        if src is None or tgt is None:
            ctx.record("relations", "skipped", key, "端点类不存在")
            continue
        if (src.id, tgt.id, relType) in existingTriples:
            ctx.record("relations", "skipped", key, "三元组已存在")
            continue
        try:
            await ctx.ontology.createRelation(
                session,
                OntologyRelationCreate(
                    source_class_id=src.id,
                    target_class_id=tgt.id,
                    relation_type=relType,
                    description=item.get("description"),
                ),
                actor=ACTOR.userId,
            )
            existingTriples.add((src.id, tgt.id, relType))
            ctx.record("relations", "created", key)
        except Exception as exc:  # noqa: BLE001
            ctx.record("relations", "failed", key, str(exc))


async def importKpis(ctx: ImportContext, session, payload: dict, classMap) -> None:
    existingRows = (await session.execute(select(KpiCatalog))).scalars().all()
    byCode = {k.kpi_code: k for k in existingRows}
    dataSources = (await session.execute(select(DataSource))).scalars().all()
    dsByName = {ds.name: ds for ds in dataSources}
    metrics = (await session.execute(select(OntologyMetric))).scalars().all()
    metricIdByName = {m.metric_name: m.id for m in metrics}

    for item in payload.get("kpis", []):
        code = item.get("kpiCode") or ""
        if not code:
            ctx.record("kpis", "failed", "<空 kpi_code>", "导出项缺 kpiCode")
            continue
        dsName = item.get("datasourceName")
        ds = dsByName.get(dsName) if dsName else None
        metricId = metricIdByName.get(item.get("metricName")) \
            if item.get("metricName") else None
        if item.get("metricName") and metricId is None:
            ctx.record(
                "kpis", "skipped", code, f"度量 {item['metricName']} 不存在"
            )
            continue
        try:
            existing = byCode.get(code)
            if existing is None:
                if ds is None:
                    ctx.record(
                        "kpis", "skipped", code,
                        f"数据源 {dsName!r} 在生产不存在（KPI 创建必须绑定数据源），"
                        "请先在生产建同名数据源后重跑",
                    )
                    continue
                await ctx.kpiCatalog.createKpi(
                    session,
                    KpiCatalogCreate(
                        kpi_code=code,
                        kpi_name=item.get("kpiName") or code,
                        business_definition=item.get("businessDefinition"),
                        formula=item.get("formula"),
                        numerator=item.get("numerator"),
                        denominator=item.get("denominator"),
                        grain=item.get("grain"),
                        unit=item.get("unit"),
                        data_source=item.get("dataSource"),
                        owner=item.get("owner"),
                        version=item.get("version"),
                        status=item.get("status") or "DRAFT",
                        datasource_id=ds.id,
                        metric_id=metricId,
                        semantic_keywords=item.get("semanticKeywords"),
                        match_threshold=item.get("matchThreshold"),
                    ),
                    actor=ACTOR,
                )
                ctx.record("kpis", "created", code)
            else:
                desired = {
                    field: item.get(dtoField)
                    for field, dtoField in (
                        ("kpi_name", "kpiName"),
                        ("business_definition", "businessDefinition"),
                        ("formula", "formula"),
                        ("numerator", "numerator"),
                        ("denominator", "denominator"),
                        ("grain", "grain"),
                        ("unit", "unit"),
                        ("data_source", "dataSource"),
                        ("owner", "owner"),
                        ("semantic_keywords", "semanticKeywords"),
                    )
                    if item.get(dtoField) is not None
                }
                # metric_id 仅在有可解析目标时回绑，避免把生产已有绑定误清成 NULL
                if metricId is not None:
                    desired["metric_id"] = metricId
                if ds is not None:
                    desired["datasource_id"] = ds.id
                changed = {
                    field: value
                    for field, value in desired.items()
                    if getattr(existing, field) != value
                }
                if changed:
                    await ctx.kpiCatalog.updateKpi(
                        session, existing.id, KpiCatalogUpdate(**changed), actor=ACTOR
                    )
                    ctx.record("kpis", "updated", code, f"字段={sorted(changed)}")
                else:
                    ctx.record("kpis", "skipped", code, "已一致")
        except Exception as exc:  # noqa: BLE001
            ctx.record("kpis", "failed", code, str(exc))


def _printSummary(summary: dict[str, dict[str, list]]) -> None:
    for section, buckets in summary.items():
        created, updated = buckets["created"], buckets["updated"]
        skipped, failed = buckets["skipped"], buckets["failed"]
        print(f"\n[{section}] created={len(created)} updated={len(updated)} "
              f"skipped={len(skipped)} failed={len(failed)}")
        for label, rows in (("created", created), ("updated", updated),
                            ("skipped", skipped), ("failed", failed)):
            for row in rows:
                print(f"  {label}: {row}")


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dryRun = "--dry-run" in sys.argv
    if args:
        srcPath = Path(args[0])
        if not srcPath.exists():
            print(f"导出文件不存在：{srcPath.resolve()}", file=sys.stderr)
            sys.exit(2)
        payload: dict[str, Any] = json.loads(srcPath.read_text(encoding="utf-8"))
    else:
        # 无路径参数 → 从 stdin 读（docker exec -i qa-backend python -m scripts.import_ontology < 导出文件）
        payload: dict[str, Any] = json.loads(sys.stdin.read())

    if dryRun:
        counts = {
            key: len(value)
            for key, value in payload.items()
            if isinstance(value, list)
        }
        print(f"[dry-run] 待导入（来源={'文件 ' + str(srcPath) if args else 'stdin'}）：{counts}")
        print("[dry-run] 正式执行：docker exec -i qa-backend python -m scripts.import_ontology < 导出文件")
        return

    ctx = ImportContext()
    factory = getSessionFactory()
    async with factory() as session:
        classMap = await importClasses(ctx, session, payload)
        await importProperties(ctx, session, payload, classMap)
        await importBusinessObjects(ctx, session, payload, classMap)
        await importMetrics(ctx, session, payload, classMap)
        await importJoins(ctx, session, payload, classMap)
        await importRelations(ctx, session, payload, classMap)
        await importKpis(ctx, session, payload, classMap)
    _printSummary(ctx.summary)
    print(
        "\n导入完成。收尾两步（必做）：\n"
        "  1. 本体入图：本体管理 → 批量关系引擎 syncGraph（或 POST /ontology/graph/sync-missing）\n"
        "  2. 向量重建：python -m scripts.backfill_milvus_embeddings --cleanup\n"
        "  （若导入了 KPI，另跑 python -m scripts.seed_kpi_semantic_index）"
    )


if __name__ == "__main__":
    asyncio.run(main())
