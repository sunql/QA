"""列出运行时 join 目录中涉及报价/物料/采购订单的表边，确认 ITMMASTER 路径。

用法：cd backend && uv run python scripts/diag_joins.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.ontology_service import OntologyService  # noqa: E402

TARGETS = {"PPRICLIST", "PPRICFICH", "ITMMASTER", "PORDERQ", "PORDER"}


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        service = OntologyService()
        classes = await service.listClasses(session)
        joins = await service.listJoins(session)
    by_id = {c.id: c for c in classes}
    print(f"类 {len(classes)} 个，join 目录 {len(joins)} 条")
    print("\n== 全部 join ==")
    for j in joins:
        src = by_id.get(j.source_class_id)
        tgt = by_id.get(j.target_class_id)
        s = f"{src.source_table if src else '?'}.{','.join(j.source_columns)}"
        t = f"{tgt.source_table if tgt else '?'}.{','.join(j.target_columns)}"
        mark = " <== 相关" if (src and src.source_table in TARGETS) or (tgt and tgt.source_table in TARGETS) else ""
        print(f"  {s} -> {t}  ({j.relation_type}){mark}")


if __name__ == "__main__":
    asyncio.run(main())
