"""pytest job 日志的**解析原语**：失败用例 ID、完成标记、收尾汇总行。

为什么单独一个模块：这三样东西都是「从 PR 作者可控的文本里抽事实」，
规则多、耦合紧、且**每一处放宽都会变成一条假绿通道** ⇒ 集中一处、
单独一个文件，便于逐条加守卫与做变异取证。

⚠️ 威胁模型的边界（务必读）：
本模块解析的是 **pytest 的 stdout**，而 stdout 由 PR 作者**完全可控**
（测试里可以 `print` 任何东西）。⇒ 这里的所有判据都只能拦住
**「意外」的假绿**（分片被取消、pytest 中途夭折、日志被截断），
**拦不住**「蓄意」伪造（作者打印一条假的汇总行 / 假的完成标记）。
⇒ 真正的合并授权仍必须落在：**受保护分支 + 必需状态检查 +
`.github/workflows/` 的 CODEOWNERS 审查**（见 `Harness/rules/开发流程规范.md`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 失败用例 ID 的形状：**nodeid**（含 `::`）或**测试目录下的 .py 文件**。
# 刻意收紧：日志里常有 `FAILED to reach the Milvus host, retrying` 这类散文，
# 宽正则 `FAILED\s+(\S+)` 会把 `to` 当成失败用例 ID ⇒ 报出无意义的「净新增失败」
# ⇒ 误判回归、白白卡住合并。
_TEST_ID = r"(?:\S+::\S+|[\w./-]*tests?/[\w./-]*\.py)"
# pytest 失败行：`FAILED <nodeid> [- reason]`
_FAILURE_PATTERN = re.compile(rf"FAILED\s+({_TEST_ID})")
# pytest 收集期错误行：`ERROR <nodeid|file.py> [- reason]`
_ERROR_PATTERN = re.compile(rf"ERROR\s+({_TEST_ID})")
# 分片完成标记：`UNIT_SHARD_DONE shard=1 status=1` / `INTEGRATION_SHARD_DONE shard=2 status=0`
# ⚠️ `shard` 允许负数：CI 脚本在**分片号校验失败**时打哨兵值 `-1`
# （旧实现打 0 ⇒ 工具报「标记与 job 不符」，真实原因「分片号非法」被掩盖）。
_MARKER_PATTERN = re.compile(r"([A-Z][A-Z0-9_]*_SHARD_DONE)\s+shard=(-?\d+)\s+status=(-?\d+)")
# pytest 的**收尾汇总行**：`2 failed, 118 passed in 812.34s`（`-q` 下无 `=` 装饰）。
# 只有 pytest 跑完整个 session 才会打这一行 ⇒ 它是「跑完了」的**唯一旁证**。
_PYTEST_SUMMARY_PATTERN = re.compile(
    r"(?i)(?:no tests ran|\d+\s+(?:failed|passed|errors?|skipped|xfailed|xpassed))"
    r"[^\n]*\bin\s+\d+(?:\.\d+)?s\b"
)
# 汇总行里的失败数 / 错误数（pytest **自己**算的 ⇒ 与我们解析出的失败行数可交叉核对）
_FAILED_COUNT_PATTERN = re.compile(r"(?i)(\d+)\s+failed")
_ERROR_COUNT_PATTERN = re.compile(r"(?i)(\d+)\s+errors?\b")

# 分片号哨兵：CI 脚本在分片号**非法**时用它打完成标记。
INVALID_SHARD = -1


@dataclass(frozen=True)
class ShardDoneMarker:
    """分片完成标记。"""

    name: str
    shard: int
    status: int


def extractFailureIds(logText: str) -> frozenset[str]:
    """从 job 日志里抽出失败用例 ID。

    正则**非锚定**：GH Actions 日志每行带 `2026-11-02T07:10:13.4960640Z ` 前缀，
    锚定 `^FAILED` 会一条都匹配不到。
    """
    found = {match.group(1) for match in _FAILURE_PATTERN.finditer(logText)}
    found.update(match.group(1) for match in _ERROR_PATTERN.finditer(logText))
    return frozenset(found)


def parseShardDoneMarkers(logText: str) -> list[ShardDoneMarker]:
    """抽取日志里的分片完成标记（按出现顺序，通常 0 或 1 个）。"""
    return [
        ShardDoneMarker(name=match.group(1), shard=int(match.group(2)), status=int(match.group(3)))
        for match in _MARKER_PATTERN.finditer(logText)
    ]


def hasPytestSummary(logText: str) -> bool:
    """日志里有没有 pytest 的收尾汇总行（= pytest 跑完了整个 session）。"""
    return _PYTEST_SUMMARY_PATTERN.search(logText) is not None


def lastPytestSummaryLine(logText: str) -> str | None:
    """日志里**最后**一条 pytest 收尾汇总行；没有则返回 None。

    取**最后**一条而不是第一条：pytest 的收尾汇总行是它自己打的最后一行，
    任何测试里 `print` 出来的仿冒汇总行都在它**之前** ⇒ 不会被仿冒行顶掉。
    """
    last: str | None = None
    for match in _PYTEST_SUMMARY_PATTERN.finditer(logText):
        last = match.group(0)
    return last


def failedIdCount(logText: str) -> int:
    """日志里**失败**用例 ID 的个数（不含收集期错误）。

    与 `extractFailureIds` 的区别：那个是 FAILED ∪ ERROR 的并集；
    这里的计数只算 `FAILED` 行 —— 因为 pytest 汇总行里的「N failed」
    **只统计失败**，收集期错误另计在「N error」里。
    """
    return len({match.group(1) for match in _FAILURE_PATTERN.finditer(logText)})


def summaryCountMismatchReason(logText: str) -> str | None:
    """汇总行里的失败数与日志里解析出的失败用例 ID 数对不上时给出原因。

    汇总行的失败数是 pytest **自己**算的；我们解析出的失败行数是**另算**的。
    两者必须一致 —— 不一致说明「日志被截断」或「失败行格式不认识」
    ⇒ 失败集合不完整 ⇒ 对差出的「净新增 0」是假的。

    两种情况**不做**这项核对（否则会误拦真实 run）：
    - 汇总行里有 error 数：error 用例的 ID 出现在 `ERROR` 行而非 `FAILED` 行，
      拿 failed 数去比「FAILED+ERROR 的总数」必然对不上；
    - 汇总行里没有 failed 数（如 `no tests ran` / 全绿）。
    """
    summaryLine = lastPytestSummaryLine(logText)
    if summaryLine is None:
        return None
    errorMatch = _ERROR_COUNT_PATTERN.search(summaryLine)
    if errorMatch is not None and int(errorMatch.group(1)) > 0:
        return None
    failedMatch = _FAILED_COUNT_PATTERN.search(summaryLine)
    if failedMatch is None:
        return None
    expected = int(failedMatch.group(1))
    actual = failedIdCount(logText)
    if expected == actual:
        return None
    return (
        f"pytest 收尾汇总行说有 {expected} 个失败用例，"
        f"但日志里解析出 {actual} 个失败用例 ID ⇒ 两者不一致"
        "（日志被截断 / 失败行格式不认识）⇒ 失败集合不完整 ⇒ 不可信"
    )
