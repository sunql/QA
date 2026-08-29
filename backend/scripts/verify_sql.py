"""验证问题 5 修复后生成的复合键 JOIN SQL 在真实 Oracle 能跑出数据。

复用 ab 脚本的环境加载（SECRET_KEY 用于解密数据源密码）。
用法：cd backend && uv run python scripts/verify_sql.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path


def _loadSecretKey() -> None:
    """从 docker/.env 读完整 SECRET_KEY（base64 含 =，shell cut 会截断 padding）。"""
    env = Path(__file__).resolve().parents[2] / "docker" / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("SECRET_KEY="):
            os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()


_loadSecretKey()

from app.domain.models import DataSource  # noqa: E402
from app.infrastructure.business_db_pool import get_adapter  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402

DATASOURCE_ID = 2  # ZJTH-Oracle

# 问题 5 修复后生成的 SQL（A=B 一致，复合键 JOIN POHNUM_0 + POPLIN_0）
SQL_Q5 = """SELECT
    d.PTHNUM_0 AS 收货单号,
    d.PTDLIN_0 AS 行号,
    d.POHNUM_0 AS 采购订单号,
    d.POPLIN_0 AS 订单行,
    q.LINAMT_0 AS 行金额
FROM ZJTH.PRECEIPTD d
JOIN ZJTH.PORDERQ q
    ON q.POHNUM_0 = d.POHNUM_0
    AND q.POPLIN_0 = d.POPLIN_0
WHERE ROWNUM <= 100"""

# 问题 4 修复后 A（注入 description）生成的多表 JOIN SQL
SQL_Q4 = """SELECT
    s.BPSNUM_0 AS 供应商编号,
    s.BPSNAM_0 AS 供应商名称,
    COUNT(a.YPTHNUM_0) AS 到货单数量
FROM ZJTH.YPRECEIPT a
JOIN ZJTH.BPSUPPLIER s ON a.BPSNUM_0 = s.BPSNUM_0
GROUP BY s.BPSNUM_0, s.BPSNAM_0
ORDER BY 到货单数量 DESC"""


async def _run(label: str, sql: str, ds: DataSource) -> None:
    print(f"\n=== {label} ===")
    print(sql.strip())
    adapter = get_adapter(ds.id, ds)
    try:
        rows = await adapter.execute_read_only(sql)
    except Exception as exc:  # noqa: BLE001
        print(f"执行失败: {type(exc).__name__}: {exc}")
        return
    print(f"返回行数: {len(rows)}")
    for i, row in enumerate(rows[:3]):
        print(f"  [{i}] {row}")
    if rows:
        print("列:", list(rows[0].keys()))


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        ds = await session.get(DataSource, DATASOURCE_ID)
        if ds is None:
            print(f"数据源 {DATASOURCE_ID} 不存在")
            return
        print(f"数据源: {ds.name} ({ds.type}) host={ds.host}:{ds.port} schema={ds.username}")
        await _run("问题5 收货明细对应的采购订单行金额（复合键 JOIN）", SQL_Q5, ds)
        await _run("问题4 各供应商的到货单数量（多表 JOIN）", SQL_Q4, ds)


if __name__ == "__main__":
    asyncio.run(main())
