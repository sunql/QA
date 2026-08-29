"""深查 PPRICLIST 关键属性：business_aliases + 描述 + 日期字段 + 实际数据采样"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from sqlalchemy import select, text
from app.domain.models import OntologyProperty, OntologyClass, OntologyJoin
from app.infrastructure.database import getSessionFactory
from app.infrastructure.business_db_pool import get_adapter
from app.domain.models import DataSource


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        ppr = (
            await session.execute(select(OntologyClass).where(OntologyClass.source_table == "PPRICLIST"))
        ).scalar_one()

        # 深查 business_aliases + description
        props = (
            await session.execute(
                select(OntologyProperty).where(OntologyProperty.class_id == ppr.id)
            )
        ).scalars().all()

        print("=== PPRICLIST 完整属性（含 business_aliases）===")
        for p in sorted(props, key=lambda x: x.source_column or ""):
            print(f"  [{p.source_column}] name={p.property_name}  alias={p.property_alias}  aliases={p.business_aliases}  desc={p.description}")

        # PPRICLIST 实际数据采样
        ds = (await session.execute(select(DataSource).where(DataSource.id == 2))).scalar_one()
        adapter = get_adapter(2, ds)

        async def q(sql: str):
            return await adapter.execute_read_only(sql)

        print("\n=== PPRICLIST 有效报价采样（2026年有价格）===")
        rows = await q("""
            SELECT PLICRI2_0 AS 物料编码, PLILIN_0 AS 行号, PRI_0 AS 单价,
                   PLISTRDAT_0 AS 生效日, PLIENDDAT_0 AS 失效日,
                   PLI_0 AS 价格表, PLICRD_0 AS 记录号
            FROM ZJTH.PPRICLIST
            WHERE PLICRI2_0 LIKE '20%%' AND PRI_0 > 0
            FETCH FIRST 5 ROWS ONLY
        """)
        for r in rows:
            print(" ", r)

        print("\n=== PPRICLIST 2026年有PRI_0的记录数===")
        for r in await q("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN PRI_0 > 0 THEN 1 ELSE 0 END) AS has_price,
                   SUM(CASE WHEN PLISTRDAT_0 >= DATE '2026-01-01' AND PRI_0 > 0 THEN 1 ELSE 0 END) AS price_2026
            FROM ZJTH.PPRICLIST
            WHERE PLICRI2_0 IN (SELECT ITMREF_0 FROM ZJTH.PORDERQ WHERE ITMREF_0 LIKE '20%%' OR ITMREF_0 LIKE '50%%')
        """):
            print(" ", r)

        print("\n=== PORDERQ 物料20开头 + PPRICLIST 联合（有价格的年份分布）===")
        for r in await q("""
            SELECT EXTRACT(YEAR FROM pl.PLISTRDAT_0) AS 年份, COUNT(*) AS 行数,
                   COUNT(DISTINCT pl.PLICRI2_0) AS 物料数,
                   AVG(pl.PRI_0) AS 平均报价
            FROM ZJTH.PORDERQ q
            JOIN ZJTH.PPRICLIST pl ON q.ITMREF_0 = pl.PLICRI2_0
            WHERE q.ITMREF_0 LIKE '20%%' AND pl.PRI_0 > 0
            GROUP BY EXTRACT(YEAR FROM pl.PLISTRDAT_0)
            ORDER BY 1
        """):
            print(" ", r)

        print("\n=== PPRICLIST 日期字段数据情况===")
        for r in await q("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN PLISTRDAT_0 IS NOT NULL THEN 1 ELSE 0 END) AS has_start,
                   SUM(CASE WHEN PLIENDDAT_0 IS NOT NULL THEN 1 ELSE 0 END) AS has_end,
                   SUM(CASE WHEN PRI_0 > 0 AND PLISTRDAT_0 IS NOT NULL THEN 1 ELSE 0 END) AS price_with_date
            FROM ZJTH.PPRICLIST
            WHERE PLICRI2_0 LIKE '20%%' OR PLICRI2_0 LIKE '50%%'
        """):
            print(" ", r)

        print("\n=== PORDERQ 2026年的物料（20开头）的合同价 vs PPRICLIST 报价===")
        for r in await q("""
            SELECT q.ITMREF_0 AS 物料, q.ORDDAT_0 AS 订单日期,
                   q.CPRPRI_0 AS 合同价, pl.PRI_0 AS 报价, pl.PLISTRDAT_0 AS 价格生效日
            FROM ZJTH.PORDERQ q
            LEFT JOIN ZJTH.PPRICLIST pl ON q.ITMREF_0 = pl.PLICRI2_0 AND pl.PRI_0 > 0
            WHERE q.ITMREF_0 LIKE '20%%'
              AND q.ORDDAT_0 >= DATE '2026-01-01'
              AND ROWNUM <= 10
        """):
            print(" ", r)


if __name__ == "__main__":
    asyncio.run(main())
