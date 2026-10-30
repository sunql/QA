"""业务对象种子（Phase 4.4；feat-data-domain-layer 调整）。

SUPPLIER / MATERIAL / PO / GR 四行必 seed；IQC 仅当 ontology_class 已有
IncomingInspection 时 seed；NCR（原「本期不建本体类」）自 0133 收紧
header_class_id NOT NULL 后不再 seed——无本体类的 BO 无法落库，其业务语义
待本体建模后经 /business-objects 正式创建。

header_class_id 由 class_name 派生（先查 ontology_class.id）；
graph_label = header_class.class_name；primary_domain_id 统一 PROCUREMENT
（采购域 Sheet 03 业务对象目录，与 ontology_class 治理字段同源）。
幂等：ON CONFLICT (code) DO UPDATE；header 缺失时不回写 NULL（NOT NULL 列）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import BusinessObject, OntologyClass

# header 解析所需的类名清单（测试库最小补种集合，见 app/tests/_pg_support.py）
HEADER_CLASS_NAMES = (
    "Supplier",
    "ItemMaster",
    "PurchaseOrder",
    "Receipt",
    "IncomingInspection",
)


async def _header_class_id_map(session: AsyncSession) -> dict[str, int]:
    """把 class_name 解析为 id；用于 seed 行填充 header_class_id."""
    rows = (
        await session.execute(
            select(OntologyClass.id, OntologyClass.class_name).where(
                OntologyClass.class_name.in_(HEADER_CLASS_NAMES)
            )
        )
    ).all()
    return {name: int(_id) for _id, name in rows}


def _seed_rows(header_map: dict[str, int]) -> list[dict[str, Any]]:
    rows = [
        {
            "code": "SUPPLIER",
            "name": "供应商",
            "header_class_id": header_map.get("Supplier"),
            "primary_domain_id": "PROCUREMENT",
            "graph_label": "Supplier",
            "description": "向企业提供物料或服务的外部组织",
        },
        {
            "code": "MATERIAL",
            "name": "物料",
            "header_class_id": header_map.get("ItemMaster"),
            "primary_domain_id": "PROCUREMENT",
            "graph_label": "ItemMaster",
            "description": "企业采购和使用的物料",
        },
        {
            "code": "PO",
            "name": "采购订单",
            "header_class_id": header_map.get("PurchaseOrder"),
            "primary_domain_id": "PROCUREMENT",
            "graph_label": "PurchaseOrder",
            "description": "企业向供应商下达的采购订单",
        },
        {
            "code": "GR",
            "name": "收货",
            "header_class_id": header_map.get("Receipt"),
            "primary_domain_id": "PROCUREMENT",
            "graph_label": "Receipt",
            "description": "企业确认收到货物",
        },
        {
            "code": "IQC",
            "name": "来料检验",
            "header_class_id": header_map.get("IncomingInspection"),
            "primary_domain_id": "PROCUREMENT",
            "graph_label": "IncomingInspection",
            "description": "对采购物料进行质量检验（结构性建模，0 行）",
        },
    ]
    # 0133 收紧 header_class_id NOT NULL：类未建的行跳过（IQC 类缺失时），
    # NCR（无类）不再 seed——见模块 docstring。
    return [r for r in rows if r["header_class_id"] is not None]


async def seedBusinessObjects(session: AsyncSession) -> int:
    """幂等 seed 业务对象（upsert）。

    行为：每次执行都把必 seed 行的 name/header_class_id/graph_label/description
    同步到当前 ontology_class 派生出的值。header_class_id 仅在解析到类时回写
    （COALESCE 保 NULL→值单向推进；NOT NULL 列不允许回写 NULL）。
    依赖：data_domain.PROCUREMENT 行须先落库（conftest/lifespan 先跑
    seedDataDomains）。

    返回：本次受影响行数（新增 + 更新）。
    """
    header_map = await _header_class_id_map(session)
    rows = _seed_rows(header_map)
    # ontology_class 为空（测试库 truncate 后未补种类）时全部行被过滤：
    # 空 values 的 INSERT 会退化成只含时间戳的语句并撞 NOT NULL，直接跳过
    if not rows:
        return 0

    stmt = pg_insert(BusinessObject).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code"],
        set_={
            "name": stmt.excluded.name,
            # NOT NULL 列：类缺失时不回写 NULL，保留既有绑定（单向推进）
            "header_class_id": func.coalesce(
                stmt.excluded.header_class_id, BusinessObject.header_class_id
            ),
            "primary_domain_id": stmt.excluded.primary_domain_id,
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
