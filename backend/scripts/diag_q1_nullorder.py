"""验证跨年差异查询「返回物料对不上 + 2025 值全空」的根因。

怀疑点：Oracle `ORDER BY ... DESC` 默认 NULLS FIRST —— 只有 2026 年采购、
2025 年无记录的物料其 TOTAL_QTY_2025 为 NULL，会排到真实 top10 之前，
被外层 WHERE ROWNUM <= 10 抓到。

对照三组：
1. 用户问题2 的原样 SQL（ORDER BY TOTAL_QTY_2025 DESC）→ 看返回物料是否全是 NULL
2. 同 SQL 加 NULLS LAST → 应返回真实 2025 top10（504014710001 等）
3. 物料按年份分布：仅2025 / 仅2026 / 两年都有

用法：cd backend && uv run python scripts/diag_q1_nullorder.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from app.domain.models import DataSource  # noqa: E402
from app.infrastructure.business_db_pool import get_adapter  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402


SQL_USER = """SELECT * FROM (
    SELECT
        ITMREF_0 AS 物料编号,
        SUM(CASE WHEN ORDDAT_0 >= DATE '2025-01-01' AND ORDDAT_0 < DATE '2026-01-01' THEN QTYUOM_0 END) AS TOTAL_QTY_2025,
        ROUND(AVG(CASE WHEN ORDDAT_0 >= DATE '2025-01-01' AND ORDDAT_0 < DATE '2026-01-01' THEN CPRPRI_0 END), 2) AS AVG_PRICE_2025,
        ROUND(AVG(CASE WHEN ORDDAT_0 >= DATE '2026-01-01' AND ORDDAT_0 < DATE '2027-01-01' THEN CPRPRI_0 END), 2) AS AVG_PRICE_2026
    FROM ZJTH.PORDERQ
    WHERE ORDDAT_0 >= DATE '2025-01-01'
      AND ORDDAT_0 < DATE '2027-01-01'
    GROUP BY ITMREF_0
    ORDER BY TOTAL_QTY_2025 DESC
) WHERE ROWNUM <= 10"""

SQL_NULLS_LAST = SQL_USER.replace("ORDER BY TOTAL_QTY_2025 DESC", "ORDER BY TOTAL_QTY_2025 DESC NULLS LAST")

SQL_YEAR_DIST = """SELECT
    SUM(CASE WHEN y25=1 AND y26=0 THEN 1 ELSE 0 END) AS only_2025,
    SUM(CASE WHEN y25=0 AND y26=1 THEN 1 ELSE 0 END) AS only_2026,
    SUM(CASE WHEN y25=1 AND y26=1 THEN 1 ELSE 0 END) AS both_years
FROM (
    SELECT ITMREF_0,
           MAX(CASE WHEN EXTRACT(YEAR FROM ORDDAT_0)=2025 THEN 1 ELSE 0 END) AS y25,
           MAX(CASE WHEN EXTRACT(YEAR FROM ORDDAT_0)=2026 THEN 1 ELSE 0 END) AS y26
    FROM ZJTH.PORDERQ
    GROUP BY ITMREF_0
)"""


def _fmt(row: dict) -> str:
    keys = list(row.keys())
    vals = [f"{k}={row[k]}" for k in keys]
    return " | ".join(vals)


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        ds = await session.get(DataSource, 2)
    adapter = get_adapter(ds.id, ds)

    print("=== 1. 用户问题2 原样 SQL（ORDER BY ... DESC）===")
    rows = await adapter.execute_read_only(SQL_USER)
    print(f"返回 {len(rows)} 行")
    for r in rows:
        print("  " + _fmt(r))

    print("\n=== 2. 同 SQL + NULLS LAST ===")
    rows2 = await adapter.execute_read_only(SQL_NULLS_LAST)
    print(f"返回 {len(rows2)} 行")
    for r in rows2:
        print("  " + _fmt(r))

    print("\n=== 3. 物料年份分布 ===")
    dist = await adapter.execute_read_only(SQL_YEAR_DIST)
    print("  " + _fmt(dist[0]))


if __name__ == "__main__":
    asyncio.run(main())
