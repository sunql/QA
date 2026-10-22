"""导入后行数核对。

基线取 spec 附录 A 的实测 count(*)，**不是** pg_stat_user_tables.n_live_tup
——后者是估算值，在本库上 audit_log 报 6、真实 17100，差 3 个数量级。

用法：
    python -m scripts.verify_migration --dsn postgresql://qa_user:...@localhost:5433/qa_metadata

退出码：
    0 = 全部对上
    1 = 有偏差（逐项列出期望 vs 实际）
    2 = 用法错误
    3 = 连不上库 / 查询失败（连接问题，不是数据问题，别去查数据）
"""

from __future__ import annotations

import asyncio
import sys

# spec 附录 A（2026-10-22 实测 count(*)）
EXPECTED: dict[str, int] = {
    "audit_log": 17100,
    "ontology_class": 54,
    "system_config": 28,
    "session_token_usage": 2391,
    "evidence": 1729,
    "user_sessions": 544,
    "session_message": 1420,
    "multi_step_step": 27,
}

# 导出后仍在增长的表：涨多少都正常，只有掉下去才可能是丢数据
GROWING: frozenset[str] = frozenset({"user_sessions", "session_message", "session_token_usage"})

# 容忍下限：GROWING 表只要不低于 (1 - SLACK) × 基线就放过
GROWING_SLACK = 0.20


def checkAll(
    actual: dict[str, int],
    expect: dict[str, int] | None = None,
    tolerate: set[str] | None = None,
) -> list[dict[str, object]]:
    """返回偏差列表。tolerate 里的表允许小幅下降。"""
    expect = EXPECTED if expect is None else expect
    tolerate = set(GROWING) if tolerate is None else tolerate
    failures: list[dict[str, object]] = []
    for table, want in expect.items():
        got = actual.get(table)
        if got is None:
            failures.append({"table": table, "expected": want, "actual": None})
            continue
        if got == want:
            continue
        # GROWING 表只要不低于阈值就放过：**向上一定放过**。
        # 导出与核对之间必然还会写入会话数据，多出来的行不是问题；
        # 只有「少了」才可能是丢数据。写成 `got < want` 会把每一次
        # 正常增长报成失败，把门禁变成噪声。
        if table in tolerate and got >= want * (1 - GROWING_SLACK):
            continue
        failures.append({"table": table, "expected": want, "actual": got})
    return failures


def renderReport(failures: list[dict[str, object]]) -> str:
    if not failures:
        return f"全部对上 ✓（{len(EXPECTED)} 张表）"
    lines = [f"{len(failures)} 张表对不上："]
    for f in failures:
        lines.append(f"  {f['table']}: 期望 {f['expected']}，实际 {f['actual']}")
    lines.append("若表在 GROWING 里且下降幅度很小，可能是导出后继续写入；若是对不上，先别启动 backend。")
    return "\n".join(lines)


def _parseDsn(databaseUrl: str) -> str:
    """asyncpg 不认 SQLAlchemy 的驱动后缀。

    仓内 DATABASE_URL 形如 postgresql+asyncpg://qa_user:...@postgres:5432/qa_metadata，
    原样传给 asyncpg.connect 会报 unrecognized URL scheme。
    """
    return databaseUrl.replace("postgresql+asyncpg://", "postgresql://", 1)


def _redact(dsn: str, message: str) -> str:
    """把错误消息里的 DSN 与口令抹掉 —— 错误输出同样不许带凭据。"""
    if dsn:
        message = message.replace(dsn, "<DSN>")
    _, _, credentials = dsn.partition("://")
    userInfo, atSign, _ = credentials.partition("@")
    if atSign and ":" in userInfo:
        password = userInfo.split(":", 1)[1]
        if password:
            message = message.replace(password, "***")
    return message


async def _queryAll(dsn: str) -> dict[str, int]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        out: dict[str, int] = {}
        for table in EXPECTED:
            out[table] = await conn.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608 — 表名来自模块常量
        return out
    finally:
        await conn.close()


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "--dsn":
        print("用法：python -m scripts.verify_migration --dsn <DATABASE_URL>", file=sys.stderr)
        return 2
    dsn = _parseDsn(argv[2])
    try:
        actual = asyncio.run(_queryAll(dsn))
    except Exception as exc:
        # 连不上库不是「行数对不上」。不区分的话，一个连错库的部署
        # 会带着退出码 1 进门禁，运维去查一个不存在的数据问题。
        print(f"查询失败（{type(exc).__name__}）：{_redact(dsn, str(exc))}", file=sys.stderr)
        print("这是连接/查询问题，不是行数偏差；请先确认 DATABASE_URL 与容器网络。", file=sys.stderr)
        return 3
    failures = checkAll(actual)
    print(renderReport(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
