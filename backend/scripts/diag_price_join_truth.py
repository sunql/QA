"""实证 PORDERQ ↔ PPRICLIST 的正确物料关联列：PLICRI2_0 还是 CPNITMREF_0？

用户指出正确 join 是 q.ITMREF_0 = pl.PLICRI2_0，而种子/BUSINESS_JOINS 用了
CPNITMREF_0。本脚本在真实 ZJTH.Oracle 上量化两列与 PORDERQ.ITMREF_0 的命中率。

用法：cd backend && uv run python scripts/diag_price_join_truth.py
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
    print(f"数据源: {ds.name} {ds.host}:{ds.port}/{ds.database_name}")

    async def q(sql: str) -> list[dict]:
        rows = await adapter.execute_read_only(sql)
        return rows

    print("\n== PPRICLIST 全表两列填充情况 ==")
    for row in await q(
        "SELECT COUNT(*) AS total,"
        " COUNT(DISTINCT CPNITMREF_0) AS distinct_cpn,"
        " COUNT(DISTINCT PLICRI2_0) AS distinct_plicri2,"
        " SUM(CASE WHEN CPNITMREF_0 IS NULL THEN 1 ELSE 0 END) AS cpn_null,"
        " SUM(CASE WHEN PLICRI2_0 IS NULL THEN 1 ELSE 0 END) AS plicri2_null"
        " FROM ZJTH.PPRICLIST"
    ):
        print(row)

    print("\n== 与 PORDERQ.ITMREF_0 的命中行数（正确 join 应能大量命中）==")
    for label, col in (("CPNITMREF_0", "CPNITMREF_0"), ("PLICRI2_0", "PLICRI2_0")):
        sql = (
            "SELECT COUNT(DISTINCT pl." + col + ") AS matched_items, COUNT(*) AS matched_rows"
            " FROM ZJTH.PPRICLIST pl"
            " JOIN ZJTH.PORDERQ q ON q.ITMREF_0 = pl." + col
        )
        for row in await q(sql):
            print(f"  q.ITMREF_0 = pl.{label:<12} -> {row}")

    print("\n== 抽样：某物料的 PPRICLIST 行（PLICRI2_0 vs CPNITMREF_0）==")
    sample = await q(
        "SELECT PLI_0, PLICRD_0, PLILIN_0, CPNITMREF_0, PLICRI_0, PLICRI1_0,"
        " PLICRI2_0, PLICRI3_0, PRI_0, PLISTRDAT_0, PLIENDDAT_0"
        " FROM ZJTH.PPRICLIST"
        " WHERE PLICRI2_0 IN (SELECT ITMREF_0 FROM ZJTH.PORDERQ WHERE ROWNUM <= 1)"
        " FETCH FIRST 5 ROWS ONLY"
    )
    for row in sample:
        print(row)


if __name__ == "__main__":
    asyncio.run(main())
