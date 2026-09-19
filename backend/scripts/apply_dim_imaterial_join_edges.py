"""DIM_IMATERIAL ↔ 采购/到货/收货明细 + 物料-厂地枢纽 JOIN 边补齐脚本（幂等，走 API 路径）。

背景（2026-09-19 需求）：DIM_IMATERIAL 是由 ODS_ITMMASTER 加工的物料维度表
（ITMREF_0 物料编码 + ITMDES1_0~3_0 物料描述 + 物料分类）。关联图零边导致
「按物料编码查物料描述」类问题无法走物料维度取属性——LLM 只能退而求其次
用 ITEM_FACILITY 或其他表。本脚本补边后，明细行 ↔ 物料维全链路连通。

连接键探针（2026-09-19，THBI，|A∩B|/min）：
    DIM_IMATERIAL.ITMREF_0 (350922) ↔ DWD_ITEM_FACILITY.MATERIAL_CODE (341194)  100%
    DIM_IMATERIAL.ITMREF_0 (350922) ↔ DWD_PURCHASE_ORDER_LINE.MATERIAL_CODE     100%
    DIM_IMATERIAL.ITMREF_0 (350922) ↔ DWD_ARRIVAL_NOTICE_LINE.MATERIAL_CODE     100%
    DIM_IMATERIAL.ITMREF_0 (350922) ↔ DWD_GOODS_RECEIPT_LINE.MATERIAL_CODE      100%

物料列只存在于明细行（单据头无物料维度，属正常）；DWD_ITEM_FACILITY 是
现网 JOIN 枢纽（FACILITY/MATERIAL_CODE 双向主键），建它也保证其他
BOM/发票/询价类表经枢纽可达物料维。

实现要点同 apply_supplier_join_edges.py / apply_isolated_join_edges.py：
走 OntologyService.createJoin（PG + audit + Neo4j 入图），join_key 预查幂等。

用法（在 backend 容器内）：
    python -m scripts.apply_dim_imaterial_join_edges
"""

from __future__ import annotations

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

DIM_CLASS_NAME = "DIM_IMATERIAL"
DIM_CODE_COLUMN = "ITMREF_0"
MATERIAL_COLUMN = "MATERIAL_CODE"

# 边方向与现网一致：source=明细行表，target=维度表
EDGES: list[dict[str, str]] = [
    {
        "sourceClass": "DWD_PURCHASE_ORDER_LINE",
        "sourceColumn": MATERIAL_COLUMN,
        "targetClass": DIM_CLASS_NAME,
        "targetColumn": DIM_CODE_COLUMN,
        "description": "采购订单行→物料维（MATERIAL_CODE=ITMREF_0）",
    },
    {
        "sourceClass": "DWD_ARRIVAL_NOTICE_LINE",
        "sourceColumn": MATERIAL_COLUMN,
        "targetClass": DIM_CLASS_NAME,
        "targetColumn": DIM_CODE_COLUMN,
        "description": "到货明细→物料维（MATERIAL_CODE=ITMREF_0）",
    },
    {
        "sourceClass": "DWD_GOODS_RECEIPT_LINE",
        "sourceColumn": MATERIAL_COLUMN,
        "targetClass": DIM_CLASS_NAME,
        "targetColumn": DIM_CODE_COLUMN,
        "description": "收货明细→物料维（MATERIAL_CODE=ITMREF_0）",
    },
    {
        "sourceClass": "DWD_ITEM_FACILITY",
        "sourceColumn": MATERIAL_COLUMN,
        "targetClass": DIM_CLASS_NAME,
        "targetColumn": DIM_CODE_COLUMN,
        "description": "物料-厂地枢纽→物料维（MATERIAL_CODE=ITMREF_0，全图可达枢纽）",
    },
]


async def apply(session, service: OntologyService, *, actor: CurrentUser) -> dict:
    """按 EDGES 补边。返回 {created, skipped, missing}。"""
    names = {name for e in EDGES for name in (e["sourceClass"], e["targetClass"])}
    classRows = await session.execute(
        select(OntologyClass.id, OntologyClass.class_name).where(
            OntologyClass.class_name.in_(names)
        )
    )
    classes = {name: cid for cid, name in classRows.all()}

    propRows = await session.execute(
        select(OntologyProperty.class_id, OntologyProperty.property_name).where(
            OntologyProperty.class_id.in_(classes.values()),
            OntologyProperty.property_name.in_(
                {c for e in EDGES for c in (e["sourceColumn"], e["targetColumn"])}
            ),
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

    for e in EDGES:
        srcName, tgtName = e["sourceClass"], e["targetClass"]
        for name, col in ((srcName, e["sourceColumn"]), (tgtName, e["targetColumn"])):
            classId = classes.get(name)
            if classId is None:
                missing.append(f"{name}:类不存在")
                break
            if col not in columnsByClass.get(classId, set()):
                missing.append(f"{name}:缺 {col} 属性")
                break
        else:
            srcId, tgtId = classes[srcName], classes[tgtName]
            joinKey = makeJoinKey(
                srcId, [e["sourceColumn"]], tgtId, [e["targetColumn"]]
            )
            if joinKey in existingKeys:
                skipped.append(f"{srcName}→{tgtName}")
                continue
            await service.createJoin(
                session,
                OntologyJoinCreate(
                    source_class_id=srcId,
                    source_columns=[e["sourceColumn"]],
                    target_class_id=tgtId,
                    target_columns=[e["targetColumn"]],
                    join_type="INNER",
                    relation_type="foreign_key",
                    description=e["description"],
                ),
                actor=actor.userId,
                actor_departments=",".join(actor.departments) if actor.departments else None,
            )
            existingKeys.add(joinKey)
            created.append(f"{srcName}→{tgtName}")

    return {"created": created, "skipped": skipped, "missing": missing}


async def main() -> int:
    actor = CurrentUser(userId="ops-join-backfill", roles=(ADMIN_ROLE,), departments=())
    sessionMaker = getSessionFactory()
    async with sessionMaker() as session:
        summary = await apply(session, OntologyService(), actor=actor)

    print(f"created: {summary['created']}")
    print(f"skipped: {summary['skipped']}")
    print(f"missing: {summary['missing']}")
    return 1 if summary["missing"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
