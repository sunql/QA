"""查询采购主表/明细表 ORDDAT_0 年份分布，确认 2025/2026 数据量与物料数。

用法：cd backend && uv run python scripts/diag_year_dist.py
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


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        ds = await session.get(DataSource, 2)
    adapter = get_adapter(ds.id, ds)
    queries = [
        "SELECT EXTRACT(YEAR FROM ORDDAT_0) AS y, COUNT(*) AS c FROM ZJTH.PORDER WHERE ORDDAT_0 IS NOT NULL GROUP BY EXTRACT(YEAR FROM ORDDAT_0) ORDER BY y",
        "SELECT COUNT(*) AS n2025 FROM ZJTH.PORDER WHERE EXTRACT(YEAR FROM ORDDAT_0)=2025",
        "SELECT COUNT(*) AS n2026 FROM ZJTH.PORDER WHERE EXTRACT(YEAR FROM ORDDAT_0)=2026",
        "SELECT COUNT(DISTINCT ITMREF_0) AS items2025 FROM ZJTH.PORDERQ d JOIN ZJTH.PORDER h ON h.POHNUM_0=d.POHNUM_0 WHERE EXTRACT(YEAR FROM h.ORDDAT_0)=2025",
        "SELECT COUNT(DISTINCT ITMREF_0) AS items2026 FROM ZJTH.PORDERQ d JOIN ZJTH.PORDER h ON h.POHNUM_0=d.POHNUM_0 WHERE EXTRACT(YEAR FROM h.ORDDAT_0)=2026",
        # 恒假条件模拟：同年既等 2025 又等 2026 → 0 行
        "SELECT COUNT(*) AS fake FROM ZJTH.PORDER WHERE EXTRACT(YEAR FROM ORDDAT_0)=2025 AND EXTRACT(YEAR FROM ORDDAT_0)=2026",
    ]
    for q in queries:
        try:
            rows = await adapter.execute_read_only(q)
            print(f"{q[:78]} => {rows}")
        except Exception as exc:  # noqa: BLE001
            print(f"{q[:78]} => ERR {getattr(exc, 'message', exc)}")


if __name__ == "__main__":
    asyncio.run(main())
