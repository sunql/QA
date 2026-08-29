"""检查实际执行结果：看前端收到的 SQL 执行后数据是否真的为空"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        key, _, val = line.strip().partition("=")
        os.environ[key] = val.strip()

from sqlalchemy import select
from app.domain.models import DataSource
from app.infrastructure.business_db_pool import get_adapter
from app.infrastructure.database import getSessionFactory


# 前端实际收到的 SQL（用户确认已生成），分别查各子句结果
SQL_FULL = """
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
) WHERE ROWNUM <= 10
"""


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        ds = (await session.execute(select(DataSource).where(DataSource.id == 2))).scalar_one()
    adapter = get_adapter(2, ds)
    print(f"数据源: {ds.name} ({ds.host}/{ds.database_name})")

    # 1. 全 SQL 结果
    print("\n=== 全 SQL 执行结果 ===")
    rows = await adapter.execute_read_only(SQL_FULL)
    print(f"返回 {len(rows)} 行")
    for r in rows:
        print(" ", r)

    # 2. 内层子查询：2026年物料采购量
    print("\n=== 内层：2026年物料采购量 TOP10 ===")
    inner = """
    SELECT
        q.ITMREF_0,
        SUM(q.QTYUOM_0) AS TOTAL_QTY_2026,
        COUNT(*) AS rows,
        MIN(q.ORDDAT_0) AS first_order,
        MAX(q.ORDDAT_0) AS last_order
    FROM ZJTH.PORDERQ q
    WHERE q.ORDDAT_0 >= DATE '2026-01-01'
      AND q.ORDDAT_0 < DATE '2027-01-01'
      AND (q.ITMREF_0 LIKE '20%' OR q.ITMREF_0 LIKE '50%')
    GROUP BY q.ITMREF_0
    ORDER BY TOTAL_QTY_2026 DESC
    FETCH FIRST 10 ROWS ONLY
    """
    rows2 = await adapter.execute_read_only(inner)
    for r in rows2:
        print(" ", r)

    # 3. PPRICLIST 对这些物料的报价（不加日期过滤）
    print("\n=== PPRICLIST：对内层 TOP10 物料的报价（不加日期） ===")
    mat_sql = "SELECT ITMREF_0 FROM ZJTH.PORDERQ WHERE ORDDAT_0 >= DATE '2026-01-01' AND ORDDAT_0 < DATE '2027-01-01' AND (ITMREF_0 LIKE '20%' OR ITMREF_0 LIKE '50%') GROUP BY ITMREF_0 ORDER BY SUM(QTYUOM_0) DESC FETCH FIRST 10 ROWS ONLY"
    mats = await adapter.execute_read_only(mat_sql)
    mat_list = [r['ITMREF_0'] for r in mats]
    print(f"TOP10 物料: {mat_list}")
    if mat_list:
        placeholders = ",".join([f":{i}" for i in range(len(mat_list))])
        price_sql = f"""
        SELECT PLICRI2_0 AS 物料, COUNT(*) AS cnt, MIN(PRI_0) AS min_p, MAX(PRI_0) AS max_p, AVG(PRI_0) AS avg_p
        FROM ZJTH.PPRICLIST
        WHERE PLICRI2_0 IN ({placeholders}) AND PRI_0 > 0
        GROUP BY PLICRI2_0
        """
        params = {str(i): m for i, m in enumerate(mat_list)}
        price_rows = await adapter.execute_read_only(price_sql, params)
        print(f"有报价的物料数: {len(price_rows)}")
        for r in price_rows:
            print(" ", r)
        if len(price_rows) == 0:
            print("  !! PPRICLIST 中这些物料完全没有报价记录 !!")
            # 查这些物料在 PPRICLIST 中的实际记录
            debug_sql = f"""
            SELECT PLICRI2_0, COUNT(*) AS cnt, MIN(PRI_0) AS min_p, MAX(PRI_0) AS max_p
            FROM ZJTH.PPRICLIST
            WHERE PLICRI2_0 IN ({placeholders})
            GROUP BY PLICRI2_0
            """
            debug_rows = await adapter.execute_read_only(debug_sql, params)
            print(f"PPRICLIST 记录（含PRI_0<=0）: {len(debug_rows)}")
            for r in debug_rows:
                print(" ", r)

    # 4. 加日期过滤后 PPRICLIST 的记录
    print("\n=== PPRICLIST 报价（含2026日期过滤）===")
    y2026_sql = """
    SELECT PLICRI2_0 AS 物料, COUNT(*) AS cnt, MIN(PRI_0) AS min_p, MAX(PRI_0) AS max_p
    FROM ZJTH.PPRICLIST
    WHERE PLISTRDAT_0 <= DATE '2026-12-31'
      AND PLIENDDAT_0 >= DATE '2026-01-01'
      AND (PLICRI2_0 LIKE '20%' OR PLICRI2_0 LIKE '50%')
      AND PRI_0 > 0
    GROUP BY PLICRI2_0
    FETCH FIRST 10 ROWS ONLY
    """
    rows4 = await adapter.execute_read_only(y2026_sql)
    for r in rows4:
        print(" ", r)


if __name__ == "__main__":
    asyncio.run(main())
