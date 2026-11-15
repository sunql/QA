"""P3a D20：Milvus **v2 集合**全量回填（复用 v1 向量，**不重算**）。

为什么单独一个脚本
------------------
P3a 的 v2 集合（`ontology_*_embeddings_v2` / `kpi_catalog_embeddings_v2`）比 v1
多带治理维度（域/组织/实体/有效期/`release_id`）。**向量本身完全一样** ——
所以回填**不需要也不允许**重新调用 embedding 模型：
一旦重算，影子比对里的差异就会混入「向量漂移」，差异不可解释
（D18 裁决 D18-②）。本脚本直接把 v1 集合里**已有的向量**搬进 v2。

四条硬约束（D18 裁决）
----------------------
1. **不重算 embedding** —— 读 v1 现有向量原样搬运。
2. **只增不删** —— 只往**新建的 v2 集合**写，v1 一行都不动。
   同一 ``(type, ontology_id)`` 的覆盖写走「先删后插」（与运行时写路径同口径），
   删除范围**只限本次回填覆盖到的那一行**，不是「清理集合」。
3. **幂等** —— 重复跑结果一致（先删后插 ⇒ 不留重复行）。
4. **写目标闸门** —— 必须过 ``scripts.milvus_write_target_guard.assertSafeMilvusTarget``：
   库名是 ``default``（生产本体集合所在，且 Milvus 无自动备份 ⇒ 误写不可逆）
   且未显式 ``--allow-prod`` ⇒ 直接拒绝执行。

用法
----
    # 推荐：目标写**隔离实验库**，v1 只从 default **只读**搬过来
    MILVUS_DB_NAME=qa_registry_v2 DATABASE_URL=postgresql+asyncpg://...@localhost:5433/qa_metadata \
        uv run python scripts/backfill_registry_v2.py --source-db default --dry-run

    MILVUS_DB_NAME=qa_registry_v2 DATABASE_URL=... \
        uv run python scripts/backfill_registry_v2.py --source-db default --types class,property

    # 同库回填（源=目标）：先试算，再实写
    MILVUS_DB_NAME=qa_test DATABASE_URL=... \
        uv run python scripts/backfill_registry_v2.py --dry-run
    MILVUS_DB_NAME=qa_test DATABASE_URL=... \
        uv run python scripts/backfill_registry_v2.py

    # 生产库（需显式 opt-in，且先做当日 PG 备份）
    uv run python scripts/backfill_registry_v2.py --allow-prod

参数
----
``--dry-run``      只读 v1 + 解析维度并打印计划，**不写任何集合**。
``--types A,B``    只处理指定类型（class/property/metric/kpi；缺省全部）。
``--limit N``      每类型最多处理 N 行（抽样调试用）。
``--source-db X``  v1 向量的**来源库（只读）**；缺省=目标库。
                   隔离实验用 ``--source-db default``：v2 落在实验库，
                   源库一个字节都不动。
``--allow-prod``   显式放行生产库 ``default``（默认拒绝）。
``--report-json``  把本次回填摘要写成 JSON（供影子比对脚本消费）。

⚠️ PG 侧说明：治理维度（域/组织/实体）来自 PG 的治理表。**要用与向量同源的那个库**
（本地 dev 是 ``qa_metadata``@5433）；测试库 ``qa_metadata_test*`` 只有种子数据，
拿它解析维度会把绝大多数行标成「未归域」⇒ 影子比对会得出「过滤把召回全干掉了」
的假结论。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from pymilvus import Collection, MilvusClient

from app.infrastructure import milvus_client as milvus
from app.infrastructure.milvus_registry_v2 import (
    REGISTRY_V2_COLLECTION_NAMES,
    ensureRegistryV2Collection,
    insertRegistryV2,
)
from app.services.ontology_dimension_service import DimensionSet, resolveDimensions
from scripts.milvus_write_target_guard import assertSafeMilvusTarget, describeMilvusTarget

# v1 → v2 集合对照（v1 名字取自 milvus_client 的常量，避免两处各写一份字符串）
V1_COLLECTION_NAMES: dict[str, str] = {
    "class": milvus._CLASS_COLLECTION_NAME,
    "property": milvus._PROPERTY_COLLECTION_NAME,
    "metric": milvus._METRIC_COLLECTION_NAME,
    "kpi": milvus._KPI_COLLECTION_NAME,
}

ALL_TYPES: tuple[str, ...] = ("class", "property", "metric", "kpi")

# v1 读取：每页行数与总行数上限（单页上限防止误读整个库）
_READ_PAGE = 1000
_MAX_ROWS_PER_TYPE = 20000

# v2 写入：delete 的 id 分片大小（Milvus 对 expr 里的 id 列表长度有上限）
_DELETE_CHUNK = 500
_INSERT_CHUNK = 1000


# ---------------------------------------------------------------------------
# 纯逻辑（单测覆盖，不连 Milvus / PG）
# ---------------------------------------------------------------------------


def _dedupeByKey(rows: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    """按 ``(type, ontology_id)`` 去重，**保留 id 最大的一行**（最新）。

    为什么需要去重：v1 集合是「单集合 → 按类型拆三集合」迁移期的产物，
    同一实体可能残留多份向量。Milvus 的 ``limit`` 按**实体**计数，
    残留副本会吃光召回窗口（D18 §3.2 同一口径）。
    """
    best: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("type") or ""), int(row.get("ontology_id") or 0))
        current = best.get(key)
        if current is None or int(row.get("id") or 0) >= int(current.get("id") or 0):
            best[key] = row
    return best


def _dimensionToDict(dim: DimensionSet) -> dict[str, Any]:
    """``DimensionSet`` → 普通 dict（写入侧只认 dict，避免两层模型耦合）。"""
    return {
        "domain_codes": tuple(dim.domainCodes),
        "org_codes": tuple(dim.orgCodes),
        "entity_type": dim.entityType,
        "valid_from": int(dim.validFrom),
        "valid_to": int(dim.validTo),
        "release_id": int(dim.releaseId),
    }


def _attachDimensions(
    rows: list[dict[str, Any]],
    dims: dict[tuple[str, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    """给 v1 行补齐治理维度，**返回新行**（原行不动 —— 不可变约定）。

    查不到治理事实 ⇒ 该行写**空维度**（显式「未归域」），**不猜**：
    猜出来的维度 = 假治理事实；空维度在域过滤下 fail-closed，
    行为可解释（= 该行本来就没有域）。
    """
    prepared: list[dict[str, Any]] = []
    for row in rows:
        type_ = str(row.get("type") or "")
        ontologyId = int(row.get("ontology_id") or 0)
        dim = dims.get((type_, ontologyId)) or {}
        prepared.append(
            {
                **row,
                "domain_codes": tuple(dim.get("domain_codes") or ()),
                "org_codes": tuple(dim.get("org_codes") or ()),
                "entity_type": str(dim.get("entity_type") or ""),
                "valid_from": int(dim.get("valid_from") or 0),
                "valid_to": int(dim.get("valid_to") or 0),
                "release_id": int(dim.get("release_id") or 0),
            }
        )
    return prepared


def _summarizeCoverage(records: list[dict[str, Any]]) -> dict[str, Any]:
    """维度覆盖统计（影子比对的「归因缺口」口径来源）。

    ``withDomain`` / ``withoutDomain`` 是 D20 影子报告的关键分解：
    过滤掉的行里，「本来就没归域」与「归了别的域」是**两回事**。
    """
    byType: dict[str, int] = {}
    withDomain = 0
    withOrg = 0
    for record in records:
        type_ = str(record.get("type") or "")
        byType[type_] = byType.get(type_, 0) + 1
        if tuple(record.get("domain_codes") or ()):
            withDomain += 1
        if tuple(record.get("org_codes") or ()):
            withOrg += 1
    return {
        "total": len(records),
        "withDomain": withDomain,
        "withoutDomain": len(records) - withDomain,
        "withOrg": withOrg,
        "byType": byType,
    }


def _resolveTargetDb(dbName: str, *, allowProdWrite: bool) -> str:
    """写目标闸门（生产库必须显式 opt-in）。"""
    return assertSafeMilvusTarget(dbName, allowProdWrite=allowProdWrite)


def _ensureDatabaseExists(dbName: str) -> None:
    """确保目标库存在（幂等）。

    库不存在时无法先连它 ⇒ 用默认库上的客户端建库
    （与 ``app/tests/milvus_isolation.ensureIsolatedDatabase`` 同款做法）。
    """
    client = MilvusClient(uri=milvus._getUri())
    try:
        if dbName not in client.list_databases():
            client.create_database(dbName)
            print(f"已创建目标库 {dbName!r}")
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Milvus 侧（真实连接）
# ---------------------------------------------------------------------------


def _readV1Rows(
    client: MilvusClient,
    type_: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """读 v1 集合全量（**含向量**），分页取。

    ``client`` 绑的是**源库**（可以与目标库不同 —— 见 ``--source-db``）：
    目标库是隔离的实验库，v1 只从别处**只读**地搬过来。
    """
    name = V1_COLLECTION_NAMES[type_]
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.query(
            name,
            filter="id >= 0",
            output_fields=[
                "id", "ontology_id", "type", "name", "alias", "description",
                "external_id", "embedding",
            ],
            limit=_READ_PAGE,
            offset=offset,
        )
        if not page:
            break
        rows.extend(page)
        offset += len(page)
        # 限流必须**先于**「取不满一页就收尾」的判断：本地 metric/kpi 只有
        # 12~18 行、行数永远小于单页 ⇒ 若顺序反了，--limit 会被静默忽略
        # ⇒ 抽样回填写出超预期行数。
        if limit is not None and len(rows) >= limit:
            return rows[:limit]
        if len(page) < _READ_PAGE:
            break
        if len(rows) > _MAX_ROWS_PER_TYPE:
            raise SystemExit(
                f"{name}: 行数超过上限 {_MAX_ROWS_PER_TYPE}，疑似读到了错库，终止"
            )
    return rows


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _overwriteRegistryV2(records: list[dict[str, Any]]) -> dict[str, int]:
    """把一批记录写进 v2 集合（**先删后插** ⇒ 幂等覆盖）。

    删除范围**只限本次写入覆盖到的 ``ontology_id``** —— 不碰别的行。

    ⚠️ **顺序有讲究**（Milvus 2.4 实测）：
    delete → flush → **release → load** → insert → flush → release → load。
    delete 之后必须先 release+load 让墓碑生效，再插新行；否则新行可能落进
    「已应用删除位图的 segment」而被一起过滤掉（新行消失，比重复更糟）。
    最后再 release+load 一次，让新行立即可检索。
    """
    byType: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        byType.setdefault(str(record.get("type") or ""), []).append(record)

    written: dict[str, int] = {}
    for type_, rows in byType.items():
        collection = ensureRegistryV2Collection(type_)
        ids = sorted({int(r["ontology_id"]) for r in rows})
        for chunk in _chunks(ids, _DELETE_CHUNK):
            collection.delete(f"ontology_id in {chunk}")
        collection.flush()
        _reloadQuietly(collection, type_, phase="删除后")
        for chunk in _chunks(rows, _INSERT_CHUNK):
            insertRegistryV2(chunk)
        collection.flush()
        _reloadQuietly(collection, type_, phase="写入后")
        written[type_] = len(rows)
    return written


def _reloadQuietly(collection: Collection, type_: str, *, phase: str) -> None:
    """release + load（删除可见性靠它）；失败只告警（不抛 —— 下一轮再覆盖一次）。"""
    try:
        collection.release()
        collection.load()
    except Exception as exc:  # noqa: BLE001
        print(
            f"⚠️ {type_}: {phase} release/load 失败（删除可见性延迟）：{exc}",
            file=sys.stderr,
        )


def _countV2Rows(type_: str) -> int:
    """v2 集合**可见**行数（按 ``ontology_id`` 去重的实体数，只读）。

    ⚠️ **不能用 ``num_entities``**：Milvus 的该统计是**物理行数**，
    包含「已删除但未 compact」的墓碑行 —— 先删后插跑两遍后它会显示 74，
    而实际可见实体只有 37（search/query 都只看到 37，墓碑已生效）。
    报物理行数会把「幂等」误判成「重复写入」。

    （删除可见性：Milvus 2.4 里 delete 之后要 release + load 才不可见；
    故 ``_overwriteRegistryV2`` 在写完做一次 release/load。）
    """
    name = REGISTRY_V2_COLLECTION_NAMES[type_]
    if not milvus.hasEmbeddingCollection(name):
        return 0
    collection = Collection(name, using=milvus._connAlias())
    collection.load()
    seen: set[int] = set()
    offset = 0
    while True:
        page = collection.query(
            expr="id >= 0",
            output_fields=["ontology_id"],
            limit=_READ_PAGE,
            offset=offset,
        )
        if not page:
            break
        seen.update(int(row["ontology_id"]) for row in page)
        offset += len(page)
        if len(page) < _READ_PAGE:
            break
    return len(seen)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def _resolveDimensionsFor(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    """按类型批量解析治理维度（PG 治理表 → DimensionSet）。"""
    from app.infrastructure.database import getSessionFactory

    byType: dict[str, list[int]] = {}
    for row in rows:
        type_ = str(row.get("type") or "")
        if type_:
            byType.setdefault(type_, []).append(int(row["ontology_id"]))

    resolved: dict[tuple[str, int], dict[str, Any]] = {}
    async with getSessionFactory()() as session:
        for type_, ids in byType.items():
            dims = await resolveDimensions(session, type_, sorted(set(ids)))
            for entityId, dim in dims.items():
                resolved[(type_, entityId)] = _dimensionToDict(dim)
    return resolved


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """执行回填（``--dry-run`` 时不写任何集合）。"""
    targetDb = _resolveTargetDb(
        milvus.getDbName(), allowProdWrite=bool(args.allow_prod)
    )
    types = tuple(t.strip() for t in str(args.types).split(",") if t.strip()) if args.types else ALL_TYPES
    unknown = [t for t in types if t not in ALL_TYPES]
    if unknown:
        raise SystemExit(f"未知类型：{unknown}；合法值：{list(ALL_TYPES)}")

    sourceDb = str(args.source_db or targetDb)
    _ensureDatabaseExists(targetDb)

    milvus._connect()
    target = describeMilvusTarget(milvus._getUri(), targetDb)
    print(f"目标：{target}；源（只读）：{sourceDb}；类型：{list(types)}；dry-run={bool(args.dry_run)}")

    sourceClient = MilvusClient(uri=milvus._getUri(), db_name=sourceDb)
    try:
        v1Rows: list[dict[str, Any]] = []
        for type_ in types:
            rows = _readV1Rows(sourceClient, type_, limit=args.limit)
            v1Rows.extend(rows)
            print(f"  v1 {type_}: 读 {len(rows)} 行")
    finally:
        sourceClient.close()

    deduped = _dedupeByKey(v1Rows)
    v1Rows = list(deduped.values())
    print(f"去重后：{len(v1Rows)} 行")

    dims = await _resolveDimensionsFor(v1Rows)
    records = _attachDimensions(v1Rows, dims)
    coverage = _summarizeCoverage(records)
    print(
        "维度覆盖："
        f"total={coverage['total']} withDomain={coverage['withDomain']} "
        f"withoutDomain={coverage['withoutDomain']} withOrg={coverage['withOrg']}"
    )
    print(f"按类型：{coverage['byType']}")

    summary: dict[str, Any] = {
        "target": target,
        "types": list(types),
        "dryRun": bool(args.dry_run),
        "coverage": coverage,
        "written": {},
    }

    if args.dry_run:
        print("dry-run：未写任何集合。")
    else:
        summary["written"] = _overwriteRegistryV2(records)
        print(f"已写入 v2：{summary['written']}")

    summary["v2Rows"] = {t: _countV2Rows(t) for t in types}
    print(f"v2 现有行数：{summary['v2Rows']}")

    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
        print(f"摘要已写入 {args.report_json}")
    return summary


def _parseArgs(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填 Milvus v2 集合（不重算向量）")
    parser.add_argument("--dry-run", action="store_true", help="只读与解析，不写集合")
    parser.add_argument("--types", default="", help="逗号分隔：class,property,metric,kpi")
    parser.add_argument("--limit", type=int, default=None, help="每类型最多处理 N 行")
    parser.add_argument("--allow-prod", action="store_true", help="显式放行生产库 default")
    parser.add_argument(
        "--source-db",
        default="",
        help="v1 向量的来源库（只读）；缺省=目标库。隔离实验常用：--source-db default",
    )
    parser.add_argument("--report-json", default="", help="摘要 JSON 输出路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parseArgs(list(sys.argv[1:] if argv is None else argv))
    # 维度解析会打大量 SQL 回显（默认 echo）⇒ 只保留 WARNING 级输出。
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
