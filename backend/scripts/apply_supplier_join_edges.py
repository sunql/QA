"""DIM_SUPPLIER ↔ 采购/收货/发票/付款 事实表 JOIN 边补齐脚本（幂等，走 API 路径）。

背景（2026-09-18 诊断）：问「三家供应商 3 月供货量 top3 物料占比」报
「以下表无法通过关联路径连通：DIM_SUPPLIER」。根因：DIM_SUPPLIER 在本体
关联图（ontology_join + Neo4j :JOIN 边）里是零边孤岛——NL2SQL 校验
（validatePlan 连通性 BFS）对任何含 DIM_SUPPLIER 的多表计划都拒绝。

连接键已用 THBI 真实数据验证（探针 2026-09-18）：
    DIM_SUPPLIER.BPSNUM_0 ↔ 事实表.SUPPLIER_CODE
    DWD_GOODS_RECEIPT(_LINE) 100%、DWD_PURCHASE_ORDER(_LINE) 100%、
    DWD_PURCHASE_INVOICE(_LINE) 100%、DWD_SUPPLIER_PAYMENT 99%
DWD_ARRIVAL_NOTICE(_LINE) 的 SUPPLIER_CODE 实测为空列（0 条非空），故意不建——
空列死边会诱导 validatePlan 放行跑出全空结果。

实现要点：
- 必须走 OntologyService.createJoin（= API 路径）：PG + audit + Neo4j 入图。
  直写 PG 不进 Neo4j（曾踩坑），Neo4j 才是 chat 召回扩边的图源。
- 幂等：先用 makeJoinKey 预查，已存在的记 skipped（createJoin 对重复会抛
  ValidationError，这里提前拦下转为 skip 语义）。
- 类缺失 / 列缺失只记 missing 继续处理其余表。

用法（在 backend 容器内）：
    python -m scripts.apply_supplier_join_edges            # 真写（幂等）
    python -m scripts.apply_supplier_join_edges --dry-run  # 只打印计划

环境：
    DATABASE_URL 指向元数据库（容器内由 compose 注入）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.dependencies import CurrentUser  # noqa: E402
from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty  # noqa: E402
from app.domain.schemas import OntologyJoinCreate  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.acl_service import ADMIN_ROLE  # noqa: E402
from app.services.ontology_service import OntologyService, makeJoinKey  # noqa: E402

DIM_CLASS_NAME = "DIM_SUPPLIER"
DIM_CODE_COLUMN = "BPSNUM_0"
SUPPLIER_CODE_COLUMN = "SUPPLIER_CODE"

# 事实表类名 → 边描述（连接键重叠率见模块 docstring 探针数据）
FACT_CLASSES: dict[str, str] = {
    "DWD_GOODS_RECEIPT": "收货单头（supplier_code → DIM_SUPPLIER.BPSNUM_0）",
    "DWD_GOODS_RECEIPT_LINE": "收货单行，供货量来源表（supplier_code → DIM_SUPPLIER.BPSNUM_0）",
    "DWD_PURCHASE_ORDER": "采购订单头（supplier_code → DIM_SUPPLIER.BPSNUM_0）",
    "DWD_PURCHASE_ORDER_LINE": "采购订单行（supplier_code → DIM_SUPPLIER.BPSNUM_0）",
    "DWD_PURCHASE_INVOICE": "采购发票头（supplier_code → DIM_SUPPLIER.BPSNUM_0）",
    "DWD_PURCHASE_INVOICE_LINE": "采购发票行（supplier_code → DIM_SUPPLIER.BPSNUM_0）",
    "DWD_SUPPLIER_PAYMENT": "供应商付款（supplier_code → DIM_SUPPLIER.BPSNUM_0）",
}


def _resolveClassId(
    classes: dict[str, int], name: str, missing: list[str]
) -> int | None:
    classId = classes.get(name)
    if classId is None:
        missing.append(f"{name}:类不存在")
    return classId


async def apply(session, service: OntologyService, *, actor: CurrentUser) -> dict:
    """补齐 DIM_SUPPLIER ↔ 事实表 JOIN 边。返回 {created, skipped, missing}。"""
    classesRows = await session.execute(
        select(OntologyClass.id, OntologyClass.class_name).where(
            OntologyClass.class_name.in_([DIM_CLASS_NAME, *FACT_CLASSES])
        )
    )
    classes = {name: cid for cid, name in classesRows.all()}

    propRows = await session.execute(
        select(OntologyProperty.class_id, OntologyProperty.property_name).where(
            OntologyProperty.property_name.in_([DIM_CODE_COLUMN, SUPPLIER_CODE_COLUMN]),
            OntologyProperty.class_id.in_(classes.values()),
        )
    )
    columnsByClass: dict[int, set[str]] = {}
    for classId, propName in propRows.all():
        columnsByClass.setdefault(classId, set()).add(propName)

    existingRows = await session.execute(select(OntologyJoin.join_key))
    existingKeys = {key for (key,) in existingRows.all()}

    missing: list[str] = []
    created: list[str] = []
    skipped: list[str] = []

    dimId = _resolveClassId(classes, DIM_CLASS_NAME, missing)
    if dimId is not None and DIM_CODE_COLUMN not in columnsByClass.get(dimId, set()):
        missing.append(f"{DIM_CLASS_NAME}:缺 {DIM_CODE_COLUMN} 属性")
        dimId = None

    if dimId is not None:
        for factName, description in FACT_CLASSES.items():
            factId = _resolveClassId(classes, factName, missing)
            if factId is None:
                continue
            if SUPPLIER_CODE_COLUMN not in columnsByClass.get(factId, set()):
                missing.append(f"{factName}:缺 {SUPPLIER_CODE_COLUMN} 属性")
                continue
            joinKey = makeJoinKey(
                factId, [SUPPLIER_CODE_COLUMN], dimId, [DIM_CODE_COLUMN]
            )
            if joinKey in existingKeys:
                skipped.append(factName)
                continue
            await service.createJoin(
                session,
                OntologyJoinCreate(
                    source_class_id=factId,
                    source_columns=[SUPPLIER_CODE_COLUMN],
                    target_class_id=dimId,
                    target_columns=[DIM_CODE_COLUMN],
                    join_type="INNER",
                    relation_type="foreign_key",
                    description=description,
                ),
                actor=actor.userId,
                actor_departments=",".join(actor.departments) if actor.departments else None,
            )
            existingKeys.add(joinKey)
            created.append(factName)

    return {"created": created, "skipped": skipped, "missing": missing}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    args = parser.parse_args()

    actor = CurrentUser(
        userId="ops-join-backfill",
        roles=(ADMIN_ROLE,),
        departments=(),
    )

    sessionMaker = getSessionFactory()
    async with sessionMaker() as session:
        if args.dry_run:
            # dry-run 只读探查：不做任何写
            service = OntologyService()
            summary = {"created": [], "skipped": [], "missing": []}
            classesRows = await session.execute(
                select(OntologyClass.id, OntologyClass.class_name).where(
                    OntologyClass.class_name.in_([DIM_CLASS_NAME, *FACT_CLASSES])
                )
            )
            classes = {name: cid for cid, name in classesRows.all()}
            print(f"类存在情况: {sorted(classes)}")
            print(f"缺失类: {sorted(set([DIM_CLASS_NAME, *FACT_CLASSES]) - set(classes))}")
            return 0
        summary = await apply(session, OntologyService(), actor=actor)

    print(f"created: {summary['created']}")
    print(f"skipped: {summary['skipped']}")
    print(f"missing: {summary['missing']}")
    if summary["missing"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
