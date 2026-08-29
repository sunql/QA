"""查运行时本体里 SupplierPriceDetail/SupplierPriceList 的状态与属性，对比 PORDERQ。

背景：用户问"查询采购价格为什么没查 SupplierPriceDetail/SupplierPriceList"。
需确认：这两个类是否在本体库中、is_active 是否激活、LLM 实际看到什么属性。

用法：cd backend && uv run python scripts/diag_price_tables.py
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

TARGETS = {
    "PPRICLIST": "SupplierPriceDetail",
    "PPRICFICH": "SupplierPriceList",
    "PORDERQ": "PurchaseOrderDetail",
    "ITMMASTER": "ItemMaster",
}


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        classes = await OntologyService().listClasses(session)
    by_table = {c.source_table: c for c in classes}
    print(f"本体类总数: {len(classes)}")
    for tbl, name in TARGETS.items():
        c = by_table.get(tbl)
        if c is None:
            print(f"\n{tbl} ({name}): 不在本体中")
            continue
        props = [p.property_name for p in c.properties] if c.properties else []
        print(
            f"\n{tbl} ({c.class_name} / {c.class_alias}): "
            f"valid_to={c.valid_to} props={len(props)}"
        )
        # 只打印与价格/数量/日期相关的属性
        for p in c.properties or []:
            nm = p.property_name
            if any(k in nm for k in ("单价", "价格", "数量", "金额", "生效", "失效", "物料", "日期")):
                print(f"    {p.property_name}  <-  {p.property_alias}  ({p.data_type})")


if __name__ == "__main__":
    asyncio.run(main())
