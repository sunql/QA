"""直接执行生成的 SQL，看实际结果"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from sqlalchemy import select
from app.domain.models import DataSource
from app.infrastructure.business_db_pool import get_adapter


SQL = """
SELECT * FROM (
    SELECT
        t.ITMREF_0 AS 物料编号,
        m.ITMDES1_0 AS 物料名称,
        t.TOTAL_QTY_2026,
        t.AVG_PRICE_2026,
        p.AVG_PRICE_2025,
        ROUND(t.AVG_PRICE_2026 - p.AVG_PRICE_2025, 2) AS PRICE_DIFF
    FROM (
        SELECT
            q.ITMREF_0,
            SUM(q.QTYUOM_0) AS TOTAL_QTY_2026,
            ROUND(AVG(pl.PRI_0), 2) AS AVG_PRICE_2026
        FROM ZJTH.PORDERQ q
        JOIN ZJTH.PPRICLIST pl
            ON pl.PLICRI2_0 = q.ITMREF_0
            AND pl.PLISTRDAT_0 <= DATE '2026-12-31'
            AND pl.PLIENDDAT_0 >= DATE '2026-01-01'
        WHERE q.ORDDAT_0 >= DATE '2026-01-01'
          AND q.ORDDAT_0 < DATE '2027-01-01'
          AND (q.ITMREF_0 LIKE '20%' OR q.ITMREF_0 LIKE '50%')
        GROUP BY q.ITMREF_0
        ORDER BY TOTAL_QTY_2026 DESC
    ) t
    JOIN ZJTH.ITMMASTER m ON m.ITMREF_0 = t.ITMREF_0
    LEFT JOIN (
        SELECT
            pl.PLICRI2_0 AS ITMREF_0,
            ROUND(AVG(pl.PRI_0), 2) AS AVG_PRICE_2025
        FROM ZJTH.PPRICLIST pl
        WHERE pl.PLISTRDAT_0 <= DATE '2025-12-31'
          AND pl.PLIENDDAT_0 >= DATE '2025-01-01'
        GROUP BY pl.PLICRI2_0
    ) p ON p.ITMREF_0 = t.ITMREF_0
    ORDER BY PRICE_DIFF DESC NULLS LAST
) WHERE ROWNUM <= 5
"""


async def main() -> None:
    from app.infrastructure.database import getSessionFactory
    factory = getSessionFactory()
    async with factory() as session:
        ds = (await session.execute(select(DataSource).where(DataSource.id == 2))).scalar_one()
    adapter = get_adapter(2, ds)
    print(f"数据源: {ds.name} ({ds.host}/{ds.database_name})")
    print("\n=== 执行 LLM 生成的 SQL ===")
    try:
        rows = await adapter.execute_read_only(SQL)
        print(f"返回 {len(rows)} 行")
        for r in rows:
            print(" ", r)
    except Exception as e:
        print(f"执行错误: {e}")

    # 查内层子查询（2026年物料采购量）
    print("\n=== 内层子查询：2026年物料采购量 TOP5 ===")
    inner = """
    SELECT
        q.ITMREF_0,
        SUM(q.QTYUOM_0) AS TOTAL_QTY_2026,
        COUNT(*) AS order_count,
        ROUND(AVG(pl.PRI_0), 2) AS AVG_PRICE_2026
    FROM ZJTH.PORDERQ q
    JOIN ZJTH.PPRICLIST pl
        ON pl.PLICRI2_0 = q.ITMREF_0
        AND pl.PLISTRDAT_0 <= DATE '2026-12-31'
        AND pl.PLIENDDAT_0 >= DATE '2026-01-01'
    WHERE q.ORDDAT_0 >= DATE '2026-01-01'
      AND q.ORDDAT_0 < DATE '2027-01-01'
      AND (q.ITMREF_0 LIKE '20%' OR q.ITMREF_0 LIKE '50%')
    GROUP BY q.ITMREF_0
    ORDER BY TOTAL_QTY_2026 DESC
    FETCH FIRST 5 ROWS ONLY
    """
    try:
        rows = await adapter.execute_read_only(inner)
        for r in rows:
            print(" ", r)
    except Exception as e:
        print(f"执行错误: {e}")

    # 查2025年报价子查询
    print("\n=== 2025报价子查询（有数据的物料）===")
    y2025 = """
    SELECT
        pl.PLICRI2_0 AS ITMREF_0,
        COUNT(*) AS price_count,
        ROUND(AVG(pl.PRI_0), 2) AS AVG_PRICE_2025
    FROM ZJTH.PPRICLIST pl
    WHERE pl.PLISTRDAT_0 <= DATE '2025-12-31'
      AND pl.PLIENDDAT_0 >= DATE '2025-01-01'
      AND pl.PLICRI2_0 LIKE '20%'
    GROUP BY pl.PLICRI2_0
    FETCH FIRST 5 ROWS ONLY
    """
    try:
        rows = await adapter.execute_read_only(y2025)
        for r in rows:
            print(" ", r)
    except Exception as e:
        print(f"执行错误: {e}")


if __name__ == "__main__":
    asyncio.run(main())
