"""THBI 采购域 -> Neo4j 业务关系图回填脚本（幂等 + 可 dry-run）。

背景：业务图 tab 查真实编码 404 的根因是 BusinessEntity 子图为空 ——
entity_mapping 同步（sync_entity_mapping_from_thbi）只写 PG，业务图唯一
回填入口是 Sheet 16 demo 种子（seed_graph_relations），键位与真实编码无关。

数据源（THBI 数仓；2026-10-25 探针实测与 entity_mapping 编码 100% 交集）：
- DWD_PURCHASE_ORDER       20 万单头（po_no / supplier_code / supplier_name / order_date）
- DWD_PURCHASE_ORDER_LINE  92 万行（po_no / material_code / supplier_code）

图形状（复用现有白名单 + 新增 PLACES）::

    (:Supplier {key: 供应商编码})-[:PLACES]->(:PurchaseOrder {key: po_no})
    (:Supplier)-[:SUPPLIES]->(:ItemMaster)     # distinct (supplier, material) 供应对
    (:PurchaseOrder)-[:CONTAINS]->(:ItemMaster)  # 行级（ItemMaster = Phase 4.4 后的物料 label）

规模参考（全量）：节点 ~51 万、边 ~147 万 → 必须走 UNWIND 批量写
（neo4j_client.upsertBusinessEntityNodes / linkBusinessRelations），
**不可**复用 seedGraphRelations 的逐节点单 session MERGE（几十小时级）。
收货单链路（GoodsReceipt，97 万节点）本期不入图，语义见仓库变更记录。

用法：
    python -m scripts.sync_business_graph_from_thbi --dry-run        # 只打印计划
    python -m scripts.sync_business_graph_from_thbi --since 2024     # 只回填该年及以后
    python -m scripts.sync_business_graph_from_thbi                  # 全量

环境：
    QUERY_TIMEOUT_SECONDS=600   # 92 万行拉取默认 30s 不够，必须显式放大
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.domain.models import DataSource  # noqa: E402
from app.infrastructure import neo4j_client as neo4j  # noqa: E402
from app.infrastructure.business_db_pool import (  # noqa: E402
    dispose_adapter,
    get_adapter,
)
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.graph_relation_service import GraphRelationService  # noqa: E402

# 头表取数（DISTINCT 压内存：一单多行同供应商在数仓里物理重复）
_PO_HEADER_SQL = (
    "SELECT DISTINCT po_no, supplier_code, supplier_name "
    "FROM THBI.DWD_PURCHASE_ORDER h {where}"
)
# 行表取数：JOIN 头表过滤（--since 场景下避免 92 万行全捞）+ DISTINCT 三元组
_PO_LINE_SQL = (
    "SELECT DISTINCT l.po_no, l.material_code, l.supplier_code "
    "FROM THBI.DWD_PURCHASE_ORDER_LINE l "
    "JOIN THBI.DWD_PURCHASE_ORDER h ON h.po_no = l.po_no {where}"
)
_SOURCE_TAG = "thbi_dwd"


@dataclass(frozen=True)
class GraphPlan:
    """一次回填的完整写入计划（纯数据，便于 dry-run 与测试断言）。"""

    suppliers: tuple[dict, ...]
    purchaseOrders: tuple[dict, ...]
    materials: tuple[dict, ...]
    places: tuple[dict, ...]    # Supplier -> PurchaseOrder
    supplies: tuple[dict, ...]  # Supplier -> Material（distinct 供应对）
    contains: tuple[dict, ...]  # PurchaseOrder -> Material（行级）
    stats: dict


def buildGraphPlan(poHeaders: list[dict], poLines: list[dict]) -> GraphPlan:
    """把 Oracle 头/行行集聚合成图写入计划（纯函数，无任何 IO）。

    - 跳过空 po_no / 空 supplier_code（头表）/ 空 material_code（行表），
      计入 stats.skippedHeaders / stats.skippedLines；
    - po_no 不在头表的行表行计 stats.orphanLines（不写边——linkBusinessRelations
      是 MATCH 语义，孤儿边会静默丢失，提前拦截并计数）；
    - 供应商名 enrichment：同一编码取首个非空 supplier_name（数仓同名重复时
      首见优先，保证确定性）；
    - 行表 supplier_code 为空时回落头表 po_no -> supplier_code 映射。
    """
    supplierNames: dict[str, str] = {}
    supplierOfPo: dict[str, str] = {}
    skippedHeaders = 0
    for row in poHeaders:
        poNo = str(row.get("po_no") or "").strip()
        supCode = str(row.get("supplier_code") or "").strip()
        if not poNo or not supCode:
            skippedHeaders += 1
            continue
        supplierOfPo.setdefault(poNo, supCode)
        name = str(row.get("supplier_name") or "").strip()
        if name and supCode not in supplierNames:
            supplierNames[supCode] = name

    poSet: set[str] = set(supplierOfPo)
    skippedLines = 0
    orphanLines = 0
    materialSet: set[str] = set()
    containsSet: set[tuple[str, str]] = set()
    suppliesSet: set[tuple[str, str]] = set()
    for row in poLines:
        poNo = str(row.get("po_no") or "").strip()
        matCode = str(row.get("material_code") or "").strip()
        if not poNo or not matCode:
            skippedLines += 1
            continue
        if poNo not in poSet:
            orphanLines += 1
            continue
        materialSet.add(matCode)
        containsSet.add((poNo, matCode))
        supCode = str(row.get("supplier_code") or "").strip() or supplierOfPo[poNo]
        suppliesSet.add((supCode, matCode))

    # 边端点必须都有节点：供应商 = 头表编码 ∪ 供应对左端；物料 = 供应对右端超集
    supplierCodes = {s for s, _ in suppliesSet} | set(supplierOfPo.values())
    materialCodes = {m for _, m in suppliesSet} | {m for _, m in containsSet}

    suppliers = tuple(
        {"key": c, "code": c, "name": supplierNames.get(c), "source": _SOURCE_TAG}
        for c in sorted(supplierCodes)
    )
    purchaseOrders = tuple(
        {"key": p, "code": p, "name": p, "source": _SOURCE_TAG} for p in sorted(poSet)
    )
    materials = tuple(
        {"key": m, "code": m, "name": None, "source": _SOURCE_TAG}
        for m in sorted(materialCodes)
    )
    places = tuple(
        {"fromKey": s, "toKey": p}
        for p, s in sorted(supplierOfPo.items(), key=lambda kv: (kv[1], kv[0]))
    )
    supplies = tuple(
        {"fromKey": s, "toKey": m} for s, m in sorted(suppliesSet)
    )
    contains = tuple(
        {"fromKey": p, "toKey": m} for p, m in sorted(containsSet)
    )

    stats = {
        "poHeaders": len(poSet),
        "suppliers": len(supplierCodes),
        "materials": len(materialCodes),
        "skippedHeaders": skippedHeaders,
        "skippedLines": skippedLines,
        "orphanLines": orphanLines,
    }
    return GraphPlan(
        suppliers=suppliers,
        purchaseOrders=purchaseOrders,
        materials=materials,
        places=places,
        supplies=supplies,
        contains=contains,
        stats=stats,
    )


async def applyGraphPlan(plan: GraphPlan) -> dict:
    """计划入图（UNWIND 批量 MERGE，幂等）。节点先于边（边是 MATCH 两端语义）。

    同步驱动用 asyncio.to_thread 包装，避免阻塞事件循环（同 traversal 约定）。
    """
    def _write() -> dict:
        # 索引前置：无索引时 MERGE/MATCH on key 全 label 扫描，批量写是 O(n²)
        neo4j.ensureBusinessKeyIndex()
        nodes = (
            neo4j.upsertBusinessEntityNodes("Supplier", list(plan.suppliers))
            + neo4j.upsertBusinessEntityNodes("PurchaseOrder", list(plan.purchaseOrders))
            + neo4j.upsertBusinessEntityNodes("ItemMaster", list(plan.materials))
        )
        edges = (
            neo4j.linkBusinessRelations("PLACES", "Supplier", "PurchaseOrder", list(plan.places))
            + neo4j.linkBusinessRelations("SUPPLIES", "Supplier", "ItemMaster", list(plan.supplies))
            + neo4j.linkBusinessRelations("CONTAINS", "PurchaseOrder", "ItemMaster", list(plan.contains))
        )
        return {"nodes": nodes, "edges": edges}

    return await asyncio.to_thread(_write)


def _whereClause(since: int | None) -> str:
    """--since 年份转 Oracle WHERE 片段（int 已被 argparse+校验钉死，无注入面）。"""
    return f"WHERE h.order_date >= DATE '{since}-01-01'" if since else ""


async def main() -> None:
    parser = argparse.ArgumentParser(description="THBI 采购域 -> Neo4j 业务图回填")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印计划与规模，不写 Neo4j"
    )
    parser.add_argument(
        "--since", type=int, default=None, help="只回填该年份及以后的采购订单（如 2024）"
    )
    args = parser.parse_args()
    if args.since is not None and not 2000 <= args.since <= 2100:
        sys.exit(f"[sync_business_graph] --since 需为 2000..2100 的年份，收到 {args.since}")

    violations = GraphRelationService().validateSchema()
    if violations:
        sys.exit(f"[sync_business_graph] 白名单校验失败（常量漂移）: {violations}")

    factory = getSessionFactory()
    async with factory() as session:
        dsList = (
            await session.execute(
                select(DataSource).where(
                    DataSource.type == "oracle", DataSource.is_active.is_(True)
                )
            )
        ).scalars().all()
    if not dsList:
        sys.exit("[sync_business_graph] data_source 里没有活跃的 Oracle 数据源")
    thbi = dsList[0]
    print(f"[sync_business_graph] mode={'DRY-RUN' if args.dry_run else 'WRITE'} "
          f"since={args.since or 'ALL'} datasource={thbi.name}({thbi.id})")

    where = _whereClause(args.since)
    adapter = get_adapter(thbi.id, thbi)
    try:
        headers = await adapter.execute_read_only(_PO_HEADER_SQL.format(where=where))
        lines = await adapter.execute_read_only(_PO_LINE_SQL.format(where=where))
    finally:
        await dispose_adapter(thbi.id)
    print(f"[sync_business_graph] oracle 拉取完成: headers={len(headers)} lines={len(lines)}")

    plan = buildGraphPlan(headers, lines)
    print(
        f"[sync_business_graph] 计划: 节点 supplier={len(plan.suppliers)} "
        f"po={len(plan.purchaseOrders)} material={len(plan.materials)}; "
        f"边 PLACES={len(plan.places)} SUPPLIES={len(plan.supplies)} "
        f"CONTAINS={len(plan.contains)}; 跳过 {plan.stats}"
    )
    if args.dry_run:
        print("[sync_business_graph] dry-run 结束，未写图 ✅")
        return

    result = await applyGraphPlan(plan)
    print(f"[sync_business_graph] 入图完成: nodes={result['nodes']} edges={result['edges']}")

    snapshot = GraphRelationService().getGraphSnapshot()
    print(
        f"[sync_business_graph] 图快照: 节点 {len(snapshot['nodes'])} "
        f"边 {len(snapshot['edges'])} ✅"
    )


if __name__ == "__main__":
    asyncio.run(main())
