"""检查运行时本体 PPRICCONF 类当前状态（名称/别名/说明/属性）。

用法：cd backend && uv run python scripts/diag_ppricconf_class.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from sqlalchemy import select  # noqa: E402

from app.domain.models import OntologyClass, OntologyProperty  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        c = (
            await session.execute(
                select(OntologyClass).where(OntologyClass.source_table == "PPRICCONF")
            )
        ).scalar_one()
        print(f"id={c.id} class_name={c.class_name!r} class_alias={c.class_alias!r}")
        print(f"description={c.description!r} valid_to={c.valid_to} created_by={c.created_by!r}")
        props = (
            await session.execute(
                select(OntologyProperty)
                .where(OntologyProperty.class_id == c.id)
                .order_by(OntologyProperty.id)
            )
        ).scalars().all()
        print(f"properties={len(props)}")
        for p in props:
            print(f"  {p.property_name} <- {p.property_alias} ({p.data_type}) "
                  f"pk={p.is_primary_key} src={p.source_column} aliases={p.business_aliases}")


if __name__ == "__main__":
    asyncio.run(main())
