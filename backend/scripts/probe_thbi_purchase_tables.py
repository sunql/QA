"""一次性只读探针：THBI Oracle 里有没有采购订单类事实表（为业务图回填选源）。

只读 metadata + count + 3 行采样，不写任何库。用法：
    ./scripts/run_local.sh python -m scripts.probe_thbi_purchase_tables
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.domain.models import DataSource  # noqa: E402
from app.infrastructure.business_db_pool import (  # noqa: E402
    dispose_adapter,
    get_adapter,
)
from app.infrastructure.database import getSessionFactory  # noqa: E402

CANDIDATE_LIKE = ("%PURCHASE_ORDER%", "%GOODS_RECEIPT%")


def _col(row: dict, name: str) -> str:
    """execute_read_only 把列名转小写，这里做大小写无关取值。"""
    return str(row.get(name) or row.get(name.lower()) or "")


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        dsList = (
            await session.execute(
                select(DataSource).where(DataSource.is_active.is_(True))
            )
        ).scalars().all()
        thbi = next(d for d in dsList if d.type == "oracle")
    print(f"[probe] data_source: id={thbi.id} name={thbi.name} db={thbi.database_name}")

    adapter = get_adapter(thbi.id, thbi)
    try:
        where = " OR ".join(f"table_name LIKE '{p}'" for p in CANDIDATE_LIKE)
        rows = await adapter.execute_read_only(
            f"SELECT table_name FROM all_tables WHERE owner='THBI' AND ({where}) "
            "ORDER BY table_name"
        )
        names = [_col(r, "TABLE_NAME") for r in rows]
        print(f"[probe] 候选表 {len(names)} 张: {names}")

        for name in names:
            if not name.replace("_", "").isalnum():  # 防奇怪表名进拼接（表名含下划线合法）
                continue
            try:
                cntRows = await adapter.execute_read_only(
                    f"SELECT count(*) AS cnt FROM THBI.{name}"
                )
                cnt = list(cntRows[0].values())[0]
                sample = await adapter.execute_read_only(
                    f"SELECT * FROM THBI.{name} WHERE ROWNUM <= 3"
                )
                cols = list(sample[0].keys()) if sample else []
                print(f"\n=== THBI.{name}  rows={cnt}  cols={cols[:12]}")
                for r in sample:
                    print("   ", {k: r[k] for k in list(r)[:12]})
            except Exception as exc:  # noqa: BLE001 —— 探针逐表容错
                print(f"\n=== THBI.{name}  ERROR: {exc}")
    finally:
        await dispose_adapter(thbi.id)


if __name__ == "__main__":
    asyncio.run(main())
