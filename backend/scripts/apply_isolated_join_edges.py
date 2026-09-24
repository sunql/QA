"""零边孤岛类 JOIN 边补齐脚本（第二批，幂等，走 API 路径）。

背景（2026-09-18 孤岛巡检）：DIM_SUPPLIER 补边（apply_supplier_join_edges.py）
后，巡检发现还有一批零边类。本脚本补 6 条边，全部经 THBI 真实数据值域探针
验证 100% 重叠（探针口径：|A∩B| / min(|A|,|B|)，2026-09-18 执行）：

    DWD_SUPPLIER_PAYMENT_LINE.PAYMENT_NO      ↔ DWD_SUPPLIER_PAYMENT.PAYMENT_NO        100% (33576/33576)
    DWD_SUPPLIER_PRICE_LIST_HEADER.PRICE_LIST_CODE ↔ DWD_SUPPLIER_PRICE_LIST.PRICE_LIST_CODE  100% (4/4)
    DWD_SUPPLIER_PRICE_LIST_CONFIG.PRICE_LIST_CODE ↔ DWD_SUPPLIER_PRICE_LIST.PRICE_LIST_CODE  100% (4/6)
    DWD_BUSINESS_PARTNER.PARTNER_CODE         ↔ DIM_SUPPLIER.BPSNUM_0                  100% (3500/3500)
    DWD_CUSTOMER.CUSTOMER_CODE                ↔ DWD_BUSINESS_PARTNER.PARTNER_CODE      100% (708/708)
    DIM_FACILITY.FCY_0                        ↔ DWD_ITEM_FACILITY.FACILITY_CODE        100% (26/26)

刻意不建（探针 0% 重叠或无候选列）：
    DWD_SUPPLIER_PAYMENT_LINE.LINKED_INVOICE_NO ↔ DWD_PURCHASE_INVOICE.INVOICE_NO  0%（死边）
    DWD_CARRIER.CARRIER_CODE ↔ ?（全库无对应列，维持孤岛并在巡检报告中可见）

实现要点与 apply_supplier_join_edges.py 相同：走 OntologyService.createJoin
（PG + audit + Neo4j 入图），join_key 预查幂等，缺类/缺列记 missing 不中断。

用法（在 backend 容器内）：
    python -m scripts.apply_isolated_join_edges
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

# 补边清单：探针验证 100% 重叠后固化（新增边必须先探针再进这里）
EDGES: list[dict[str, str]] = [
    {
        "sourceClass": "DWD_SUPPLIER_PAYMENT_LINE",
        "sourceColumn": "PAYMENT_NO",
        "targetClass": "DWD_SUPPLIER_PAYMENT",
        "targetColumn": "PAYMENT_NO",
        "description": "付款行→付款单头（PAYMENT_NO，行/头结构边）",
    },
    {
        "sourceClass": "DWD_SUPPLIER_PRICE_LIST_HEADER",
        "sourceColumn": "PRICE_LIST_CODE",
        "targetClass": "DWD_SUPPLIER_PRICE_LIST",
        "targetColumn": "PRICE_LIST_CODE",
        "description": "价目清单头→价目清单（PRICE_LIST_CODE）",
    },
    {
        "sourceClass": "DWD_SUPPLIER_PRICE_LIST_CONFIG",
        "sourceColumn": "PRICE_LIST_CODE",
        "targetClass": "DWD_SUPPLIER_PRICE_LIST",
        "targetColumn": "PRICE_LIST_CODE",
        "description": "价目配置→价目清单（PRICE_LIST_CODE）",
    },
    {
        "sourceClass": "DWD_BUSINESS_PARTNER",
        "sourceColumn": "PARTNER_CODE",
        "targetClass": "DIM_SUPPLIER",
        "targetColumn": "BPSNUM_0",
        "description": "合作伙伴宽表→供应商维（PARTNER_CODE=BPSNUM_0，供应商侧 100% 命中）",
    },
    {
        "sourceClass": "DWD_CUSTOMER",
        "sourceColumn": "CUSTOMER_CODE",
        "targetClass": "DWD_BUSINESS_PARTNER",
        "targetColumn": "PARTNER_CODE",
        "description": "客户维→合作伙伴宽表（CUSTOMER_CODE=PARTNER_CODE）",
    },
    {
        "sourceClass": "DIM_FACILITY",
        "sourceColumn": "FCY_0",
        "targetClass": "DWD_ITEM_FACILITY",
        "targetColumn": "FACILITY_CODE",
        "description": "厂地维（X3 FCY_0）→物料-厂地事实（FACILITY_CODE）",
    },
]


async def apply(session, service: OntologyService, *, actor: CurrentUser) -> dict:
    """按 EDGES 清单补边。返回 {created, skipped, missing}。"""
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
