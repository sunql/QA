"""内察 Oracle ZJTH.PPRICCONF 真实列结构，供本体类/属性建模。

输出：ALL_TAB_COLUMNS 全列 + 主键 + 行数 + 抽样一行（找真实填充的业务列）。
用法：cd backend && uv run python scripts/diag_ppricconf_schema.py
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

TABLE = "PPRICCONF"


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        ds = await session.get(DataSource, 2)
    adapter = get_adapter(ds.id, ds)

    cols = await adapter.execute_read_only(
        f"""SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE, DATA_PRECISION, DATA_SCALE
            FROM ALL_TAB_COLUMNS
            WHERE OWNER='ZJTH' AND TABLE_NAME='{TABLE}'
            ORDER BY COLUMN_ID"""
    )
    print(f"=== {TABLE} 列（{len(cols)}） ===")
    for c in cols:
        print(f"  {str(c['COLUMN_NAME']):24s} {str(c['DATA_TYPE']):14s} len={str(c['DATA_LENGTH']):>5} "
              f"{'NULL' if c['NULLABLE']=='Y' else 'NOT NULL'} prec={c['DATA_PRECISION']} scale={c['DATA_SCALE']}")

    try:
        pk = await adapter.execute_read_only(
            f"""SELECT cc.COLUMN_NAME, cc.POSITION
                FROM ALL_CONSTRAINTS c
                JOIN ALL_CONS_COLUMNS cc ON c.OWNER=cc.OWNER AND c.CONSTRAINT_NAME=cc.CONSTRAINT_NAME
                WHERE c.OWNER='ZJTH' AND c.TABLE_NAME='{TABLE}' AND c.CONSTRAINT_TYPE='P'
                ORDER BY cc.POSITION"""
        )
        print("PK:", [r["COLUMN_NAME"] for r in pk] if pk else "NONE")
    except Exception as exc:  # noqa: BLE001
        print("PK ERR:", getattr(exc, "message", exc))

    try:
        n = await adapter.execute_read_only(f"SELECT COUNT(*) FROM ZJTH.{TABLE}")
        print("行数:", n[0]["COUNT(*)"])
    except Exception as exc:  # noqa: BLE001
        print("COUNT ERR:", getattr(exc, "message", exc))

    # 与既有价格表的关联验证：PLI_0 是否能在 PPRICFICH / PPRICLIST 中命中
    for join_sql, label in [
        (f"SELECT COUNT(DISTINCT c.PLI_0) AS n FROM ZJTH.PPRICCONF c "
         f"JOIN ZJTH.PPRICFICH h ON h.PLI_0 = c.PLI_0", "PPRICCONF→PPRICFICH 命中清单数"),
        (f"SELECT COUNT(DISTINCT c.PLI_0) AS n FROM ZJTH.PPRICCONF c "
         f"JOIN ZJTH.PPRICLIST l ON l.PLI_0 = c.PLI_0", "PPRICCONF→PPRICLIST 命中清单数"),
    ]:
        try:
            rows = await adapter.execute_read_only(join_sql)
            print(f"{label}: {rows[0]['N'] if rows else '?'}")
        except Exception as exc:  # noqa: BLE001
            print(f"{label} ERR:", getattr(exc, "message", exc))

    try:
        dist = await adapter.execute_read_only(
            f"SELECT PLI_0, COUNT(*) AS c FROM ZJTH.{TABLE} GROUP BY PLI_0 ORDER BY c DESC"
        )
        print("PLI_0 分布:", {r["PLI_0"]: r["C"] for r in dist})
    except Exception as exc:  # noqa: BLE001
        print("DIST ERR:", getattr(exc, "message", exc))

    # 抽样一行：找出非空业务列（排除常见审计/扩展列）
    try:
        sample = await adapter.execute_read_only(
            f"""SELECT * FROM ZJTH.{TABLE} WHERE ROWNUM <= 1"""
        )
        if sample:
            print("抽样行（非空列）:")
            for name, v in sample[0].items():
                if v is not None and str(v).strip() not in ("", "0", "0.0"):
                    print(f"  {str(name):24s} = {v}")
    except Exception as exc:  # noqa: BLE001
        print("SAMPLE ERR:", getattr(exc, "message", exc))


if __name__ == "__main__":
    asyncio.run(main())
