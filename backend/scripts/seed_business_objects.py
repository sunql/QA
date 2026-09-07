"""业务对象种子（Phase 4.4）。

6 行：SUPPLIER / MATERIAL / PO / GR / IQC / NCR。
幂等：ON CONFLICT (code) DO NOTHING。

header_class_id 由 class_name 派生（先查 ontology_class.id）；
graph_label = header_class.class_name；NCR 无 header_class，二者皆 NULL。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import BusinessObject, OntologyClass


async def _header_class_id_map(session: AsyncSession) -> dict[str, int]:
    """把 class_name 解析为 id；用于 seed 行填充 header_class_id."""
    rows = (
        await session.execute(
            select(OntologyClass.id, OntologyClass.class_name).where(
                OntologyClass.class_name.in_(
                    [
                        "Supplier",
                        "ItemMaster",
                        "PurchaseOrder",
                        "Receipt",
                        "IncomingInspection",
                    ]
                )
            )
        )
    ).all()
    return {name: int(_id) for _id, name in rows}


def _seed_rows(header_map: dict[str, int]) -> list[dict[str, Any]]:
    return [
        {
            "code": "SUPPLIER",
            "name": "供应商",
            "header_class_id": header_map.get("Supplier"),
            "graph_label": "Supplier",
            "description": "向企业提供物料或服务的外部组织",
        },
        {
            "code": "MATERIAL",
            "name": "物料",
            "header_class_id": header_map.get("ItemMaster"),
            "graph_label": "ItemMaster",
            "description": "企业采购和使用的物料",
        },
        {
            "code": "PO",
            "name": "采购订单",
            "header_class_id": header_map.get("PurchaseOrder"),
            "graph_label": "PurchaseOrder",
            "description": "企业向供应商下达的采购订单",
        },
        {
            "code": "GR",
            "name": "收货",
            "header_class_id": header_map.get("Receipt"),
            "graph_label": "Receipt",
            "description": "企业确认收到货物",
        },
        {
            "code": "IQC",
            "name": "来料检验",
            "header_class_id": header_map.get("IncomingInspection"),
            "graph_label": "IncomingInspection",
            "description": "对采购物料进行质量检验（结构性建模，0 行）",
        },
        {
            "code": "NCR",
            "name": "不合格处理",
            "header_class_id": None,
            "graph_label": None,
            "description": "来料不合格记录（本期不建本体类，不入 Neo4j 图）",
        },
    ]


async def seedBusinessObjects(session: AsyncSession) -> int:
    """幂等 seed 业务对象（upsert）。

    行为：每次执行都把 6 行的 name/header_class_id/graph_label/description
    同步到当前 ontology_class 派生出的值。即使先前以 NULL header_class_id
    INSERT（如 ontology_class 迁移尚未就绪时），后续重跑会自动回填。

    返回：本次受影响行数（新增 + 更新）。
    """
    header_map = await _header_class_id_map(session)
    rows = _seed_rows(header_map)

    stmt = pg_insert(BusinessObject).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code"],
        set_={
            "name": stmt.excluded.name,
            "header_class_id": stmt.excluded.header_class_id,
            "graph_label": stmt.excluded.graph_label,
            "description": stmt.excluded.description,
        },
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount or 0


async def main() -> None:
    """CLI 入口：连接默认 DB，跑 seed."""
    import asyncio

    from app.infrastructure.database import getSessionFactory

    session_maker = getSessionFactory()
    async with session_maker() as session:
        upserted = await seedBusinessObjects(session)
        print(f"[seed_business_objects] 本次 upsert {upserted} 条（新增 + 回填）")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
