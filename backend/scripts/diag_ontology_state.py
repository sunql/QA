"""查运行时本体状态：JOIN边 + PPRICLIST属性 + ITMMASTER属性"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from sqlalchemy import select
from app.domain.models import OntologyJoin, OntologyProperty, OntologyClass
from app.infrastructure.database import getSessionFactory


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        joins = (await session.execute(select(OntologyJoin))).scalars().all()
        print("=== 所有 JOIN ===")
        for j in joins:
            print(f"  id={j.id} src={j.source_class_id} tgt={j.target_class_id} src_cols={j.source_columns} tgt_cols={j.target_columns}")

        ppr = (
            await session.execute(select(OntologyClass).where(OntologyClass.source_table == "PPRICLIST"))
        ).scalar_one_or_none()
        if ppr:
            props = (
                await session.execute(select(OntologyProperty).where(OntologyProperty.class_id == ppr.id))
            ).scalars().all()
            print(f"\n=== PPRICLIST 属性 (id={ppr.id}) ===")
            for p in props:
                print(f"  id={p.id} name={p.property_name} alias={p.property_alias} col={p.source_column} fk={p.is_foreign_key} ref={p.ref_class_id}")
        else:
            print("PPRICLIST 类不存在")

        # PORDERQ 关键属性
        po = (
            await session.execute(select(OntologyClass).where(OntologyClass.source_table == "PORDERQ"))
        ).scalar_one_or_none()
        if po:
            props = (
                await session.execute(select(OntologyProperty).where(OntologyProperty.class_id == po.id))
            ).scalars().all()
            print(f"\n=== PORDERQ 属性 (id={po.id}) ===")
            for p in props:
                if p.source_column in ("ITMREF_0", "CPRPRI_0", "UOM_0", "ORDDAT_0", "QTYUOM_0"):
                    print(f"  id={p.id} name={p.property_name} alias={p.property_alias} col={p.source_column} fk={p.is_foreign_key}")
        else:
            print("PORDERQ 类不存在")


if __name__ == "__main__":
    asyncio.run(main())
