"""实证：采购订单单位 vs 供应商报价单位，对同一物料是否一致。

关联列：PORDERQ.ITMREF_0 = PPRICLIST.PLICRI2_0（补五已修正）。
目标：
- 行级：q.UOM_0 与 pl.UOM_0 相同/不同的行占比
- 物料级：每个物料的订单单位集合 与 报价单位集合 是否一致
- 差异样本：单位不同的物料，其 合同价(CPRPRI_0) vs 报价(PRI_0) 是否量级不同
- 与主数据单位(PUU_0/STU_0) 的对照

用法：cd backend && uv run python scripts/diag_unit_consistency.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from sqlalchemy import select

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from app.domain.models import DataSource  # noqa: E402
from app.infrastructure.business_db_pool import get_adapter  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        ds = (
            await session.execute(select(DataSource).where(DataSource.id == 2))
        ).scalar_one()
    adapter = get_adapter(2, ds)
    print(f"数据源: {ds.name}")

    async def q(sql: str) -> list[dict]:
        return await adapter.execute_read_only(sql)

    print("\n== 1) 行级单位一致率（join 全量）==")
    for r in await q(
        "SELECT COUNT(*) AS total_rows,"
        " SUM(CASE WHEN q.UOM_0 = pl.UOM_0 THEN 1 ELSE 0 END) AS same_unit,"
        " SUM(CASE WHEN q.UOM_0 <> pl.UOM_0 THEN 1 ELSE 0 END) AS diff_unit"
        " FROM ZJTH.PORDERQ q JOIN ZJTH.PPRICLIST pl ON q.ITMREF_0 = pl.PLICRI2_0"
    ):
        print(r)

    print("\n== 2) 物料级：每个物料的 订单单位集合 vs 报价单位集合 ==")
    for r in await q(
        "SELECT COUNT(*) AS total_materials,"
        " SUM(CASE WHEN n_order = 1 AND n_price = 1 AND o_unit = p_unit"
        "       THEN 1 ELSE 0 END) AS consistent,"
        " SUM(CASE WHEN n_order = 1 AND n_price = 1 AND o_unit = p_unit"
        "       THEN 0 ELSE 1 END) AS inconsistent"
        " FROM ("
        "  SELECT q.ITMREF_0, COUNT(DISTINCT q.UOM_0) AS n_order,"
        "   COUNT(DISTINCT pl.UOM_0) AS n_price,"
        "   MIN(q.UOM_0) AS o_unit, MIN(pl.UOM_0) AS p_unit"
        "  FROM ZJTH.PORDERQ q JOIN ZJTH.PPRICLIST pl ON q.ITMREF_0 = pl.PLICRI2_0"
        "  GROUP BY q.ITMREF_0"
        " )"
    ):
        print(r)

    print("\n== 3) 差异样本：单位不同 + 双方都有价的物料（合同价 vs 报价）==")
    for r in await q(
        "SELECT q.ITMREF_0 AS 物料, q.UOM_0 AS 订单单位, pl.UOM_0 AS 报价单位,"
        " q.CPRPRI_0 AS 合同价, pl.PRI_0 AS 报价"
        " FROM ZJTH.PORDERQ q JOIN ZJTH.PPRICLIST pl ON q.ITMREF_0 = pl.PLICRI2_0"
        " WHERE q.UOM_0 <> pl.UOM_0 AND q.CPRPRI_0 > 0 AND pl.PRI_0 > 0"
        " FETCH FIRST 10 ROWS ONLY"
    ):
        print(r)

    print("\n== 4) 上轮遗留物料 206028007002650885B 的单位与价格明细 ==")
    for r in await q(
        "SELECT q.UOM_0 AS 订单单位, q.CPRPRI_0 AS 合同价,"
        " pl.UOM_0 AS 报价单位, pl.PRI_0 AS 报价, pl.PLICRD_0 AS 报价单,"
        " i.PUU_0 AS 采购单位, i.STU_0 AS 库存单位"
        " FROM ZJTH.PORDERQ q"
        " JOIN ZJTH.PPRICLIST pl ON q.ITMREF_0 = pl.PLICRI2_0"
        " JOIN ZJTH.ITMMASTER i ON i.ITMREF_0 = q.ITMREF_0"
        " WHERE q.ITMREF_0 = '206028007002650885B' AND q.CPRPRI_0 > 0 AND pl.PRI_0 > 0"
        " FETCH FIRST 5 ROWS ONLY"
    ):
        print(r)

    print("\n== 5) 单位取值分布（两表各 top10）==")
    print("-- PORDERQ.UOM_0:")
    for r in await q(
        "SELECT UOM_0, COUNT(*) AS n FROM ZJTH.PORDERQ"
        " WHERE UOM_0 IS NOT NULL GROUP BY UOM_0 ORDER BY n DESC FETCH FIRST 10 ROWS ONLY"
    ):
        print("  ", r)
    print("-- PPRICLIST.UOM_0:")
    for r in await q(
        "SELECT UOM_0, COUNT(*) AS n FROM ZJTH.PPRICLIST"
        " WHERE UOM_0 IS NOT NULL GROUP BY UOM_0 ORDER BY n DESC FETCH FIRST 10 ROWS ONLY"
    ):
        print("  ", r)

    print("\n== 6) 单位相同的物料：合同价与报价的量级关系（比值分布抽样）==")
    for r in await q(
        "SELECT q.ITMREF_0 AS 物料, q.CPRPRI_0 AS 合同价, pl.PRI_0 AS 报价,"
        " ROUND(q.CPRPRI_0 / NULLIF(pl.PRI_0,0), 2) AS 比值"
        " FROM ZJTH.PORDERQ q JOIN ZJTH.PPRICLIST pl ON q.ITMREF_0 = pl.PLICRI2_0"
        " WHERE q.UOM_0 = pl.UOM_0 AND q.CPRPRI_0 > 0 AND pl.PRI_0 > 0"
        " FETCH FIRST 10 ROWS ONLY"
    ):
        print(r)


if __name__ == "__main__":
    asyncio.run(main())
