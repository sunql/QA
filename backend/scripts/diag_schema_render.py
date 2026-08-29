"""渲染 buildSchemaText，确认 LLM 实际看到的 PPRICLIST 属性行 + JOIN 关系 + 间接路径提示。

用法：cd backend && uv run python scripts/diag_schema_render.py
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
from app.services.nl2sql_service import Nl2SqlService
from app.services.ontology_service import OntologyService  # noqa: E402


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        service = OntologyService()
        classes = await service.listClasses(session)
        joins = await service.listJoins(session)
    text = Nl2SqlService().buildSchemaText(classes, joins=joins)
    lines = text.splitlines()
    print(f"schema 文本 {len(lines)} 行\n")
    print("== PPRICLIST 段 ==")
    in_sec = False
    for ln in lines:
        if ln.startswith("### "):
            in_sec = ln.startswith("### SupplierPriceDetail")
            if in_sec:
                print(ln)
            continue
        if in_sec:
            print(ln)
    print("\n== JOIN 关系（涉及 PPRIC/ITMMASTER/PORDER）==")
    in_join = False
    for ln in lines:
        if ln.startswith("### "):
            in_join = ln.startswith("### JOIN 关系")
            continue
        if in_join and any(k in ln for k in ("PPRICLIST", "PPRICFICH", "ITMMASTER", "PORDER")):
            print(ln)
    print("\n== 间接 JOIN 路径提示 ==")
    for ln in lines:
        if "间接 JOIN 路径" in ln or "->" in ln and ln.startswith("  "):
            print(ln)


if __name__ == "__main__":
    asyncio.run(main())
