"""为指定本体属性补充 business_aliases（近义词），供 LLM 引用名消歧。

业务别名是运行时数据（2-2 字段），不经 seed（seed 只创建不更新，memory 已记录），
本脚本以幂等方式 merge 进 DB，可重复运行。别名来自问题 3 等实测 LLM 用过的近义词
（"到货行号"→"行号"、"到货日期"→"收货日期"等）。

用法：cd backend && uv run python scripts/seed_business_aliases.py
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.domain.models import OntologyClass, OntologyProperty
from app.infrastructure.database import getSessionFactory

# (source_table, property_name, aliases)
ALIASES: list[tuple[str, str, list[str]]] = [
    # 到货明细：LLM 按"到货单号"前缀对称推断"到货行号"/"到货行"
    ("YPRECEIPTD", "行号", ["到货行号", "到货行"]),
    # 到货/到货明细：近义词"到货日期"→"收货日期"
    ("YPRECEIPTD", "收货日期", ["到货日期"]),
    ("YPRECEIPT", "收货日期", ["到货日期"]),
    # 到货明细：近义词"到货数量"→"收货数量"
    ("YPRECEIPTD", "收货数量", ["到货数量"]),
    # 到货明细：近义词"收货行号"→"收货行"（PTDLIN_0 关联的收货单行）
    ("YPRECEIPTD", "收货行", ["收货行号"]),
]


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        updated = 0
        for table, prop_name, aliases in ALIASES:
            cls = (
                await session.execute(
                    select(OntologyClass).where(
                        OntologyClass.source_table == table,
                        OntologyClass.valid_to.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if cls is None:
                print(f"  ! 找不到活跃类 {table}")
                continue
            prop = (
                await session.execute(
                    select(OntologyProperty).where(
                        OntologyProperty.class_id == cls.id,
                        OntologyProperty.property_name == prop_name,
                    )
                )
            ).scalar_one_or_none()
            if prop is None:
                print(f"  ! {table}.{prop_name} 属性不存在")
                continue
            merged = list(dict.fromkeys((prop.business_aliases or []) + aliases))
            prop.business_aliases = merged
            updated += 1
            print(f"  {table}.{prop_name} -> {merged}")
        await session.commit()
        print(f"更新 {updated} 个属性")


if __name__ == "__main__":
    asyncio.run(main())
