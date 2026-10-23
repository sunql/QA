"""导入后行数核对。

基线取 **Task 8 导出那一刻实测的 count(*)**（`--baseline rowcounts.baseline.json`），
不是 pg_stat_user_tables.n_live_tup —— 后者是估算值，在本库上 audit_log 报 6、
真实 17100，差 3 个数量级。

不传 `--baseline` 时回落到 spec 附录 A（2026-10-22 的一次快照）。**这份快照已知漂移**：
源库一直在被写入，静态表的实测值已经与附录 A 不一致，照它核对会把正常环境判成迁移失败。
所以回落会打一条醒目的告警；计划里的 Step 6 始终传 `--baseline`。

传了 `--baseline` 时同时做两件事：
    目标 = 导出时的实测值（判断传输有没有丢数据）
    下限 = spec 附录 A（绝对下限，防止基线本身不可信，比如导错库）
低于下限的表会以「低于附录 A 下限」单独标出，与传输漂移区分开。

用法：
    python -m scripts.verify_migration --dsn postgresql://qa_user:...@localhost:5433/qa_metadata
    python -m scripts.verify_migration --dsn <DSN> --baseline backups/rowcounts.baseline.json

退出码：
    0 = 全部对上
    1 = 有偏差（逐项列出期望 vs 实际与原因）
    2 = 用法错误
    3 = 环境不可用（基线读不了 / 连不上库 / 查询失败，不是数据问题，别去查数据）
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# spec 附录 A（2026-10-22 实测 count(*)）。既是回落时的目标，也是传了基线时的绝对下限。
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

# 容忍下限：GROWING 表只要不低于 (1 - SLACK) × 目标就放过
GROWING_SLACK = 0.20

# 三种偏差各有各的诊断，报告里必须能分开
REASON_MISSING_TABLE = "表不存在"
REASON_BELOW_FLOOR = "低于附录 A 下限"
REASON_DRIFT = "行数偏差"

USAGE = (
    "用法：python -m scripts.verify_migration --dsn <DATABASE_URL> "
    "[--baseline <rowcounts.baseline.json>]"
)


class UsageError(Exception):
    """命令行用法错误 ⇒ 退出码 2。"""


class BaselineError(Exception):
    """基线文件不可用 ⇒ 退出码 3（环境问题，不是行数偏差）。"""


def loadBaseline(path: str) -> dict[str, int]:
    """读 Task 8 导出时刻的实测行数。

    读不了、坏 JSON、缺表、行数不是整数 —— 全部收敛成 BaselineError，
    因为它们都是同一件事：这份环境没准备好，别去查数据。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise BaselineError(f"读不到基线文件 {path}（{type(exc).__name__}）") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BaselineError(f"基线文件不是合法 JSON：{path}") from exc
    tables = parsed.get("tables") if isinstance(parsed, dict) else None
    if not isinstance(tables, dict):
        raise BaselineError(f"基线文件缺 tables 段：{path}")
    missing = [table for table in EXPECTED if table not in tables]
    if missing:
        # 漏表 ≠ 通过。少核一张表就等于把「有没有丢数据」这件事留了个洞。
        raise BaselineError(f"基线漏了 {len(missing)} 张表：{', '.join(sorted(missing))}")
    counts: dict[str, int] = {}
    for table in EXPECTED:
        value = tables[table]
        if isinstance(value, bool) or not isinstance(value, int):
            raise BaselineError(f"基线里 {table} 的行数不是整数：{value!r}")
        counts[table] = value
    return counts


def parseArgs(argv: list[str]) -> tuple[str, str | None]:
    """→ (dsn, baselinePath | None)。用法错误抛 UsageError。

    空 --dsn 是用法错误（退出码 2），不是「让 asyncpg 用默认值」。
    否则 --dsn "" 会真的去连 ::1:5432 / 127.0.0.1:5432，也就是
    「悄悄查了别的库」→ 好的环境报「迁移失败」。
    """
    dsn: str | None = None
    baselinePath: str | None = None
    index = 1
    while index < len(argv):
        flag = argv[index]
        if flag == "--dsn":
            if dsn is not None or index + 1 >= len(argv):
                raise UsageError("--dsn 需要一个值，且只能给一次")
            dsn = argv[index + 1]
            index += 2
            continue
        if flag == "--baseline":
            if baselinePath is not None or index + 1 >= len(argv):
                raise UsageError("--baseline 需要一个值，且只能给一次")
            baselinePath = argv[index + 1]
            index += 2
            continue
        raise UsageError(f"无法识别的参数：{flag}")
    if dsn is None:
        raise UsageError("缺少 --dsn")
    if not dsn.strip():
        raise UsageError("--dsn 是空的")
    if baselinePath is not None and not baselinePath.strip():
        raise UsageError("--baseline 路径是空的")
    return dsn, baselinePath


def checkAll(
    actual: dict[str, int | None],
    expect: dict[str, int] | None = None,
    floor: dict[str, int] | None = None,
    tolerate: set[str] | None = None,
) -> list[dict[str, object]]:
    """返回偏差列表。

    expect = 目标行数（缺省 EXPECTED）；floor = 绝对下限（缺省不设）。
    tolerate 里的表允许小幅下降。actual 里的 None 表示这张表没查到。
    """
    expect = EXPECTED if expect is None else expect
    tolerate = set(GROWING) if tolerate is None else tolerate
    floor = {} if floor is None else floor
    failures: list[dict[str, object]] = []
    for table, want in expect.items():
        got = actual.get(table)
        if got is None:
            failures.append({
                "table": table, "expected": want, "actual": None,
                "reason": REASON_MISSING_TABLE,
            })
            continue
        lowerBound = floor.get(table)
        if lowerBound is not None and got < lowerBound:
            # 下限是绝对的，连 GROWING 的 slack 也盖不过它。
            failures.append({
                "table": table, "expected": want, "actual": got,
                "reason": REASON_BELOW_FLOOR,
            })
            continue
        if got == want:
            continue
        # GROWING 表只要不低于阈值就放过：**向上一定放过**。
        # 导出与核对之间必然还会写入会话数据，多出来的行不是问题；
        # 只有「少了」才可能是丢数据。写成 `got < want` 会把每一次
        # 正常增长报成失败，把门禁变成噪声。
        if table in tolerate and got >= want * (1 - GROWING_SLACK):
            continue
        failures.append({
            "table": table, "expected": want, "actual": got, "reason": REASON_DRIFT,
        })
    return failures


def renderReport(
    failures: list[dict[str, object]],
    tableCount: int | None = None,
    targetLabel: str = "",
) -> str:
    checked = len(EXPECTED) if tableCount is None else tableCount
    if not failures:
        suffix = f"，目标：{targetLabel}" if targetLabel else ""
        return f"全部对上 ✓（{checked} 张表{suffix}）"
    lines = [f"{len(failures)} 张表对不上："]
    for f in failures:
        reason = f.get("reason", REASON_DRIFT)
        lines.append(f"  {f['table']}: 期望 {f['expected']}，实际 {f['actual']}（{reason}）")
    lines.append("『表不存在』与『低于附录 A 下限』先查迁移与备份，别去查网络。")
    lines.append("若是 GROWING 表且下降幅度很小，可能是导出后继续写入；若是对不上，先别启动 backend。")
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


async def _queryAll(dsn: str) -> dict[str, int | None]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        out: dict[str, int | None] = {}
        for table in EXPECTED:
            try:
                out[table] = await conn.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608 — 表名来自模块常量
            except asyncpg.exceptions.UndefinedTableError:
                # 半恢复的库（表没建出来）正是 spec §6.4 铁律场景。
                # 记 None 交给 checkAll 报成「表不存在」，退出码仍是 1：
                # 失败关闭不变，只有诊断变了。让异常冒上去会被 main 归成
                # 「连接/查询问题」，把运维送去查一个明明在库里的问题；
                # 中断整轮统计则会让后面所有表都查不成。
                out[table] = None
        return out
    finally:
        await conn.close()


def _warnAppendixSnapshot() -> None:
    """回落提醒。静默回落会让运维重新制造这次要消除的困惑。"""
    print(
        "=" * 70 + "\n"
        "!! 未提供 --baseline：正在用 2026-10-22 spec 附录 A 的快照核对。\n"
        "!! 这份快照已知漂移 —— 源库一直在被写入，audit_log / evidence /\n"
        "!! multi_step_step 的实测值已经与附录 A 不一致，照它核对会把正常环境\n"
        "!! 判成「迁移失败」。计划里的 Step 6 始终传 --baseline；只有临时排查才不带。\n"
        + "=" * 70,
        file=sys.stderr,
    )


def main(argv: list[str]) -> int:
    try:
        rawDsn, baselinePath = parseArgs(argv)
    except UsageError as exc:
        print(f"{exc}\n{USAGE}", file=sys.stderr)
        return 2

    dsn = _parseDsn(rawDsn)
    baseline: dict[str, int] | None = None
    if baselinePath is not None:
        try:
            baseline = loadBaseline(baselinePath)
        except BaselineError as exc:
            print(f"基线不可用：{exc}", file=sys.stderr)
            print("这是环境问题，不是行数偏差；先修基线文件。", file=sys.stderr)
            return 3
    else:
        _warnAppendixSnapshot()

    try:
        actual = asyncio.run(_queryAll(dsn))
    except Exception as exc:
        # 连不上库不是「行数对不上」。不区分的话，一个连错库的部署
        # 会带着退出码 1 进门禁，运维去查一个不存在的数据问题。
        print(f"查询失败（{type(exc).__name__}）：{_redact(dsn, str(exc))}", file=sys.stderr)
        print("这是连接/查询问题，不是行数偏差；请先确认 DATABASE_URL 与容器网络。", file=sys.stderr)
        return 3

    if baseline is None:
        failures = checkAll(actual)
        targetLabel = ""
    else:
        failures = checkAll(actual, expect=baseline, floor=EXPECTED)
        targetLabel = f"导出基线 {baselinePath}"
    print(renderReport(failures, targetLabel=targetLabel))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))