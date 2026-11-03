"""CI 失败集合对差：把「PR 失败集合 ⊆ 基线失败集合」这条合并判据做成**可执行**工具。

背景
----
本仓的 CI 基线是**红的**（`main` 上即有既有失败）⇒ 合并判据不是「等 CI 变绿」，
而是**失败集合对差**：取同一个 CI 在 PR 与基线两次 run 的失败用例集合，
**净新增为空**才合并。

人工做这件事已经踩过多个坑（全部实测于 2026-11-02 的 PR #98 / #99 批次），
本模块把它们逐个封死：

1. **`cancelled` 的 job 失败集合是空的。** GH Actions 里 job 超时被标记为
   `cancelled`（**不是** `failure`），它的失败集合是**空的** ⇒ 直接拿它对差会得出
   「净新增 0」的**假结论**。⇒ `cancelled` / `skipped` / **缺完成标记**的 job
   一律判为**不可用**，必须复跑该 job 后再用。
2. **完成标记缺失 = 没跑完。** 每个分片 job 跑完必须打印
   `X_SHARD_DONE shard=N status=<pytest 退出码>`（unit 已有、integration 已补）。
   缺标记 ⇒ 该 job 在跑完前就死了 ⇒ 失败集合不可用。
3. **pytest 只有退出码 0 / 1 才代表「跑完了」。** 2=中断 / 3=内部错误 / 4=用法错误 /
   5=未收集到用例 / 128+N=被信号杀死 ⇒ 这些退出码下失败集合都是**空的或不可信的**
   ⇒ 一律判为不可用。（`status=137` 曾是实测出来的假绿通道：CI 脚本用
   `|| status=$?` 捕获退出码，pytest 被 OOM killer 杀掉时 bash 报 137，
   **标记照样打印** ⇒ 旧实现判「跑完了、真红、失败集合为空」⇒ 结论「可合并」。）
4. **真红但零失败明细 ⇒ 不可信。** `status=1` 却一条 `FAILED`/`ERROR` 都没有 ⇒
   失败集合是空的 ⇒ 同样会让对差得出假的「净新增 0」。
5. **`status=1` 但日志里没有 pytest 的收尾汇总行 ⇒ 不可信。**
   pytest 只有在**整个 session 跑完**时才会打出收尾汇总行（`2 failed, 118 passed in 812.34s`）。
   一个用例里 `os._exit(1)` 就能让 pytest **中途夭折**却留下退出码 1 ⇒
   CI 脚本照样打出 `status=1` ⇒ 但失败集合只是**前半截** ⇒ 净新增 0 是假的。
6. **日志行带时间戳前缀。** GH Actions 的日志每行形如
   `2026-11-02T07:10:13.4960640Z FAILED app/tests/x.py::test_y`
   ⇒ 正则**不能锚定** `^FAILED`（那样一条都匹配不到）。
7. **`comm` 要求两侧同序同 collation。** python `sorted()`（码位序）与
   `sort -u`（locale 序）不同序时，`comm` 会输出**两边都非空**的假差异。
   ⇒ 本模块在 Python 里直接做集合差，**不依赖 shell `comm`**。
8. **两次 run 必须跑同一批 job。** PR 少跑一个分片 ⇒ 失败集合只会**变小** ⇒
   「净新增 0」是假的（而且 GitHub 上 CI 还会是**全绿** ⇒ 连二次防线都没有）。
   ⇒ 两侧 job 名集合不等价 ⇒ 直接判「对差不成立」。
9. **两次 run 必须是不同的 run，且各自真的有 job。**
   `--base-run` 与 `--pr-run` 填成同一个 id ⇒ 自比自 ⇒ 失败集合必然相同
   ⇒ 「净新增 0」是假的；某侧 run 一个 job 都没有（id 指错 / workflow 没跑起来）
   ⇒ 失败集合是空集 ⇒ 同样假的。
10. **job 名必须符合分片协议。** job 名含 `unit` / `integration` / `shard`
    却不匹配已知协议（`Backend / unit shard N`）⇒ 说明协议被改过 ⇒ 不可信。
    日志里出现分片完成标记、但 job 名不符合协议 ⇒ 同理不可信（不能降级成
    「非分片 job」）。
11. **报告里的一切外部字符串都要净化。** 日志与 workflow 的 `name:` 都由 PR 作者
    可控 ⇒ 可能含 ANSI/OSC 转义序列（改写剪贴板、擦除已打印的结论行 ⇒ 伪造报告）。
    ⇒ 打印前统一剔除控制字符并截断。
12. **`gh` 的 stderr 可能带凭据。** job-logs 端点 302 到 Azure Blob 的 SAS 签名 URL；
    `GH_DEBUG=api` 时还会 dump 请求头 ⇒ 落进报告前统一打码。
13. **PR 改了「CI 相关面」⇒ 失败集合不再可比。** 见 `app.utils.ci_surface`：
    PR 可以通过 conftest / workflow / 分片清单让失败用例**根本不产生 FAILED 行**
    ⇒ 假「净新增 0」⇒ 而 CI 还全绿 ⇒ 没有二次防线。

用法（本地，人工执行）::

    uv run python -m app.utils.ci_failure_set \
        --repo sunql/QA --base-run 41396973529 --pr-run 41402551747

退出码：

===== ==========================
码     含义
===== ==========================
0     净新增 0 且所有 job 可用且覆盖等价 ⇒ 可合并
1     净新增失败非空 ⇒ 不可合并
2     存在不可用 job ⇒ 必须复跑后再对差
3     运行错误（gh 调用失败、参数非法、两侧 run 相同、某侧无 job 等）
4     两次 run 的 job 集合不等价 ⇒ 对差不成立 ⇒ 人工确认
5     净丢失失败用例非空 ⇒ 须人工确认不是「把失败藏起来了」
6     PR 改了 CI 相关面 ⇒ 失败集合不可比 ⇒ 须人工确认
===== ==========================
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.utils.ci_gh import (
    fetchJobLog,
    fetchJobPayloads,
    validateRepo,
    validateRunId,
)
from app.utils.ci_surface import CiSurfaceInfo, detectCiSurfaceChanges, resolveCiSurface
from app.utils.ci_text import redactSecrets, sanitizeForReport

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
_MARKER_PATTERN = re.compile(r"([A-Z][A-Z0-9_]*_SHARD_DONE)\s+shard=(\d+)\s+status=(-?\d+)")
# 从 job 名推出期望的完成标记：`Backend / unit shard 0` ⇒ `UNIT_SHARD_DONE` + shard=0。
# 非分片 job（`Frontend / typecheck + coverage`）⇒ None（**不要求标记**）。
_JOB_MARKER_PATTERN = re.compile(r"(?i)\b(unit|integration)\b\s+shard\s+(\d+)\s*$")
# job 名里出现这些词 ⇒ 它**看起来**是分片 job ⇒ 必须符合上面的协议。
# （协议被改过 / job 被换掉 ⇒ 不可信 ⇒ 不能降级成「非分片 job」。）
_SHARD_LIKE_PATTERN = re.compile(r"(?i)\b(unit|integration|shard)\b")
# pytest 的**收尾汇总行**：`2 failed, 118 passed in 812.34s`（`-q` 下无 `=` 装饰）。
# 只有 pytest 跑完整个 session 才会打这一行 ⇒ 它是「跑完了」的第二个证据。
_PYTEST_SUMMARY_PATTERN = re.compile(
    r"(?i)(?:no tests ran|\d+\s+(?:failed|passed|errors?|skipped|xfailed|xpassed))"
    r"[^\n]*\bin\s+\d+(?:\.\d+)?s\b"
)

# job 结论里，「跑完了且结果可信」的两种
_TRUSTED_CONCLUSIONS = frozenset({"success", "failure"})
# 结论与 marker 期望的对应关系
_CONCLUSION_BY_SUCCESS = {"success": True, "failure": False}

# pytest 退出码里，**代表「跑完了」**的只有这两个：
# 0 = 全部通过；1 = 有用例失败（跑完了，失败集合完整）。
# 其余（2 中断 / 3 内部错误 / 4 用法错误 / 5 未收集到用例 / 128+N 被信号杀死）
# 都意味着「没跑完」或「等于没跑」⇒ 失败集合不可信。
_COMPLETED_STATUSES = frozenset({0, 1})
# 128+N = 被信号 N 杀死（137=SIGKILL、143=SIGTERM）
_SIGNAL_EXIT_BASE = 128

# 结论码
_REASON_MERGEABLE = "MERGEABLE"
_REASON_NEW_FAILURES = "NEW_FAILURES"
_REASON_UNUSABLE = "UNUSABLE_JOBS"
_REASON_COVERAGE = "COVERAGE_MISMATCH"
_REASON_NEEDS_REVIEW = "NEEDS_REVIEW"
_REASON_CI_SURFACE = "CI_SURFACE_CHANGED"

# 拉取 CI 数据时可能被抛出的异常类型（gh 调用 + JSON 解析）。
_FETCH_ERRORS = (
    RuntimeError,
    OSError,
    ValueError,
    KeyError,
    AttributeError,
    subprocess.SubprocessError,
)


@dataclass(frozen=True)
class CiJob:
    """CI run 里的一个 job。"""

    jobId: int
    name: str
    conclusion: str


@dataclass(frozen=True)
class ShardDoneMarker:
    """分片完成标记。"""

    name: str
    shard: int
    status: int


@dataclass(frozen=True)
class JobAssessment:
    """单个 job 的可用性判定结果。"""

    job: CiJob
    marker: ShardDoneMarker | None
    failureIds: tuple[str, ...]
    isUsable: bool
    unusableReason: str | None
    hasFailureWithoutDetail: bool
    markerCount: int = 0


@dataclass(frozen=True)
class FailureDiff:
    """两次 run 的失败集合之差。"""

    baseCount: int
    prCount: int
    added: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    """合并判据的结论。"""

    canMerge: bool
    blockingReason: str | None
    reasonCode: str = _REASON_MERGEABLE


# ---------------------------------------------------------------------------
# 失败用例抽取与完成标记解析
# ---------------------------------------------------------------------------


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


def _hasPytestSummary(logText: str) -> bool:
    """日志里有没有 pytest 的收尾汇总行（= pytest 跑完了整个 session）。"""
    return _PYTEST_SUMMARY_PATTERN.search(logText) is not None


# ---------------------------------------------------------------------------
# 单个 job 的可用性判定
# ---------------------------------------------------------------------------


def _markerMismatchReason(job: CiJob, marker: ShardDoneMarker) -> str | None:
    """结论与标记互相矛盾时给出原因；一致则返回 None。"""
    expectedSuccess = _CONCLUSION_BY_SUCCESS.get(job.conclusion)
    if expectedSuccess is None:
        return None
    markerSaysSuccess = marker.status == 0
    if expectedSuccess == markerSaysSuccess:
        return None
    return (
        f"job 结论为 {job.conclusion}，但完成标记写着 status={marker.status} ⇒ 两者矛盾，不可信"
    )


def _incompleteRunReason(marker: ShardDoneMarker) -> str | None:
    """完成标记的 status 表明「pytest 没跑完」时给出原因。

    pytest 退出码：0=全绿 / 1=有失败（**这两个都代表跑完了**）/
    2=中断 / 3=内部错误 / 4=用法错误 / 5=未收集到用例；
    128+N = 被信号 N 杀死（137=SIGKILL、143=SIGTERM）。
    ⇒ 只有 0 与 1 的失败集合可信。
    """
    status = marker.status
    if status in _COMPLETED_STATUSES:
        return None
    if status >= _SIGNAL_EXIT_BASE or status < 0:
        return (
            f"完成标记 status={status} ⇒ pytest 被信号杀死（没跑完）"
            "⇒ 失败集合是空的 ⇒ 不可信"
        )
    if status == 5:
        return (
            "完成标记 status=5 ⇒ pytest 未收集到任何用例（等于没跑）"
            "⇒ 失败集合是空的 ⇒ 不可信"
        )
    return (
        f"完成标记 status={status} ⇒ pytest 未正常跑完（2=中断 / 3=内部错误 / 4=用法错误）"
        "⇒ 失败集合不可信 ⇒ 需先修掉上面的报错再重跑"
    )


def _expectedMarkerFor(jobName: str) -> tuple[str, int] | None:
    """从 job 名推出期望的完成标记（族名 + 分片号）。

    job 名形如 `Backend / unit shard 0` ⇒ 期望 `UNIT_SHARD_DONE` + shard=0。
    **非分片 job**（`Frontend / typecheck + coverage`）⇒ 返回 None ——
    它没有标记协议，不能因为「缺标记」就判它不可用
    （否则真实 run 上退出码 0 永远不可达 ⇒ 工具直接不可用）。
    """
    match = _JOB_MARKER_PATTERN.search(jobName)
    if match is None:
        return None
    return f"{match.group(1).upper()}_SHARD_DONE", int(match.group(2))


def _judgeNonShardJob(job: CiJob, markers: list[ShardDoneMarker]) -> str | None:
    """「不符合分片协议」的 job 的判定；没问题则返回 None。

    两条硬规则：

    1. 日志里**有**分片完成标记 ⇒ job 名被改过（或 job 被换成了另一个）
       ⇒ 不能按「非分片 job」降级处理，否则会拿一个**没跑完**
       （`status∉{0,1}`）的失败集合去对差。
    2. job 名**看起来**是分片 job（含 `unit` / `integration` / `shard`）
       却不匹配协议 ⇒ 协议被改过 ⇒ 不可信。
    """
    if markers:
        return (
            f"日志里有分片完成标记（{markers[-1].name} shard={markers[-1].shard}），"
            f"但 job「{sanitizeForReport(job.name)}」不符合分片协议 ⇒ 不可信"
        )
    if _SHARD_LIKE_PATTERN.search(job.name):
        return (
            "job 名含 unit/integration/shard 但不符合已知分片协议"
            f"（job「{sanitizeForReport(job.name)}」）⇒ 不可信"
        )
    return None


def _judgeShardJob(
    job: CiJob,
    markers: list[ShardDoneMarker],
    extractedIds: tuple[str, ...],
    logText: str,
    expectedMarker: tuple[str, int],
) -> tuple[ShardDoneMarker | None, str | None]:
    """「符合分片协议」的 job 的判定 ⇒ 返回 (标记, 原因)；原因 None ⇒ 可用。"""
    if len(markers) > 1:
        # pytest 的 stdout 由 PR 作者可控（测试里可以 print）⇒ 可以伪造标记。
        # 真标记只会在最后打一次 ⇒ 出现多条 ⇒ 必须人工确认。
        return None, (
            f"日志里出现 {len(markers)} 条分片完成标记 ⇒ 疑似有伪造标记"
            "（pytest 输出由 PR 作者可控）⇒ 不可信"
        )
    if not markers:
        return None, (
            "日志里缺少分片完成标记（形如 `X_SHARD_DONE shard=N status=<退出码>`）"
            "⇒ 该 job 没跑完，失败集合不可用"
        )
    marker = markers[-1]
    reason = _markerMismatchReason(job, marker)
    if reason is None and (marker.name != expectedMarker[0] or marker.shard != expectedMarker[1]):
        reason = (
            f"完成标记 {marker.name} shard={marker.shard} 与 job「{sanitizeForReport(job.name)}」"
            f"期望的 {expectedMarker[0]} shard={expectedMarker[1]} 不符 ⇒ 不可信"
        )
    if reason is None:
        reason = _incompleteRunReason(marker)
    if reason is None and marker.status == 1 and not _hasPytestSummary(logText):
        reason = (
            "完成标记显示 pytest 真红（status=1），但日志里没有 pytest 的收尾汇总行"
            "（形如 `2 failed, 118 passed in 812.34s`）⇒ 无法确认 pytest 跑完了整个 session"
            "⇒ 失败集合可能只覆盖前半截 ⇒ 不可信"
        )
    if reason is None and marker.status != 0 and not extractedIds:
        reason = (
            "完成标记显示 pytest 真红（status=1），但日志里一条 FAILED/ERROR 都没有"
            "⇒ 失败集合是空的 ⇒ 不可信"
        )
    return marker, reason


def assessJob(
    job: CiJob,
    logText: str,
    logFetchError: str | None = None,
) -> JobAssessment:
    """判定单个 job 的失败集合**是否可用**。

    不可用 ⇒ 它的失败集合是空的（`cancelled` / `skipped` / 没跑完 / 被信号杀死），
    拿它对差会得出假的「净新增 0」⇒ 必须复跑该 job。
    """
    markers = parseShardDoneMarkers(logText)
    extractedIds = tuple(sorted(extractFailureIds(logText)))
    expectedMarker = _expectedMarkerFor(job.name)

    reason: str | None = None
    marker: ShardDoneMarker | None = None
    failureIds = extractedIds

    if job.conclusion not in _TRUSTED_CONCLUSIONS:
        reason = f"job 结论为 {job.conclusion}（失败集合是空的，不可用于对差 ⇒ 必须复跑该 job）"
    elif expectedMarker is None:
        reason = _judgeNonShardJob(job, markers)
        if reason is None and job.conclusion == "failure" and not extractedIds:
            # 真正的非分片 job（如 frontend）：真红但抽不出失败明细
            # （vitest 的失败格式本工具不解析）⇒ 用一个**跨 run 稳定的不透明 ID**
            # 顶替 ⇒ 两次 run 可比，且「PR 把它弄红了」会作为净新增失败被看见。
            failureIds = (f"job:{job.name}",)
    else:
        marker, reason = _judgeShardJob(job, markers, extractedIds, logText, expectedMarker)

    if reason is not None and logFetchError is not None:
        reason = f"{reason}（日志拉取失败：{logFetchError}）"

    isUsable = reason is None
    hasFailureWithoutDetail = bool(marker is not None and marker.status == 1 and not extractedIds)

    return JobAssessment(
        job=job,
        marker=marker,
        failureIds=failureIds,
        isUsable=isUsable,
        unusableReason=reason,
        hasFailureWithoutDetail=hasFailureWithoutDetail,
        markerCount=len(markers),
    )


# ---------------------------------------------------------------------------
# 整轮 run 的汇总与对差
# ---------------------------------------------------------------------------


def _unionUsableFailures(assessments: Iterable[JobAssessment]) -> frozenset[str]:
    """可用 job 的失败集合取并集（不可用 job 的失败集合**不计入**）。"""
    return frozenset(
        failureId
        for item in assessments
        if item.isUsable
        for failureId in item.failureIds
    )


def collectRunFailures(
    jobs: Iterable[CiJob],
    logs: Mapping[int, str],
) -> tuple[frozenset[str], tuple[JobAssessment, ...]]:
    """汇总一轮 run 的失败集合（**只取可用 job** 的）与逐 job 判定。"""
    assessments = tuple(assessJob(job, logs.get(job.jobId, "")) for job in jobs)
    return _unionUsableFailures(assessments), assessments


def diffFailureSets(base: frozenset[str], pr: frozenset[str]) -> FailureDiff:
    """两个失败集合之差。

    在 Python 里做集合差，**不依赖 shell `comm`** —— `comm` 要求两侧同序同 collation，
    python `sorted()`（码位序）与 `sort -u`（locale 序）不同序时会输出两边都非空的假差异。
    """
    return FailureDiff(
        baseCount=len(base),
        prCount=len(pr),
        added=tuple(sorted(pr - base)),
        removed=tuple(sorted(base - pr)),
    )


def _describeUnusable(assessments: Iterable[JobAssessment]) -> str:
    return "; ".join(
        f"{sanitizeForReport(item.job.name)}(job={item.job.jobId}): {item.unusableReason}"
        for item in assessments
        if not item.isUsable and item.unusableReason is not None
    )


def _coverageMismatchReason(
    baseAssessments: Iterable[JobAssessment],
    prAssessments: Iterable[JobAssessment],
) -> str | None:
    """两次 run 的 job 名集合不等价时给出原因；等价则返回 None。

    **job 名**（不是 job id）才是可比维度 —— 两次 run 的 job id 必然不同，
    而 job 名里带着分片号（`Backend / integration shard 2`）。
    """
    baseNames = frozenset(item.job.name for item in baseAssessments)
    prNames = frozenset(item.job.name for item in prAssessments)
    missing = sorted(baseNames - prNames)
    extra = sorted(prNames - baseNames)
    if not missing and not extra:
        return None
    return (
        "两次 run 跑的 job 集合不等价 ⇒ 失败集合不可比："
        f"基线有而 PR 没有：{missing or '无'}；PR 有而基线没有：{extra or '无'}"
        "（PR 少跑 job ⇒ 失败集合只会变小 ⇒ 「净新增 0」是假的，且 CI 可能全绿）"
    )


def verdictFor(
    diff: FailureDiff,
    baseAssessments: Iterable[JobAssessment],
    prAssessments: Iterable[JobAssessment],
) -> Verdict:
    """给出合并判据的结论。

    **只要有任何一个 job 不可用，或两次 run 覆盖不等价，就不能输出「可合并」** ——
    即使净新增为空。这正是「假净新增 0」的根因所在。
    """
    baseTuple = tuple(baseAssessments)
    prTuple = tuple(prAssessments)
    unusable = [item for item in (*baseTuple, *prTuple) if not item.isUsable]
    if unusable:
        return Verdict(
            canMerge=False,
            blockingReason=(
                f"存在不可用 job（失败集合不可信 ⇒ 必须复跑后重新对差）：{_describeUnusable(unusable)}"
            ),
            reasonCode=_REASON_UNUSABLE,
        )
    coverageReason = _coverageMismatchReason(baseTuple, prTuple)
    if coverageReason is not None:
        return Verdict(
            canMerge=False,
            blockingReason=coverageReason,
            reasonCode=_REASON_COVERAGE,
        )
    if diff.added:
        return Verdict(
            canMerge=False,
            blockingReason=f"净新增失败 {len(diff.added)} 条：{', '.join(diff.added)}",
            reasonCode=_REASON_NEW_FAILURES,
        )
    if diff.removed:
        # 净丢失**不等于**回归（可能是「顺手修好了」），但规则要求人工确认
        # 不是「把失败藏起来了」⇒ 单列成「需人工确认」，不与净新增混为一谈。
        return Verdict(
            canMerge=False,
            blockingReason=(
                f"净丢失失败用例 {len(diff.removed)} 条 ⇒ 须人工确认不是「把失败藏起来了」"
            ),
            reasonCode=_REASON_NEEDS_REVIEW,
        )
    return Verdict(canMerge=True, blockingReason=None, reasonCode=_REASON_MERGEABLE)


# ---------------------------------------------------------------------------
# gh 适配（把 run 的 job 拉下来逐个判定）
# ---------------------------------------------------------------------------


def _toCiJobs(rawItems: Iterable[dict]) -> list[CiJob]:
    """把 gh 返回的 job dict 转成 `CiJob`。"""
    jobs: list[CiJob] = []
    for item in rawItems:
        if "id" not in item:
            raise ValueError(f"gh 返回的 job 缺少 id 字段：{item}")
        jobs.append(
            CiJob(
                jobId=int(item["id"]),
                name=str(item.get("name", "")),
                conclusion=str(item.get("conclusion") or ""),
            )
        )
    return jobs


def collectRunFromGithub(repo: str, runId: str) -> tuple[frozenset[str], tuple[JobAssessment, ...]]:
    """从 GitHub 拉一轮 run 的失败集合与逐 job 判定。

    **逐个 job 拉日志、拉完立刻判定并丢弃原文** ⇒ 内存里最多留一份日志，
    而不是把一轮 run 的所有日志同时留在内存里（integration 日志可达数十 MB/份）。
    """
    jobs = _toCiJobs(fetchJobPayloads(repo, runId))
    assessments: list[JobAssessment] = []
    for job in jobs:
        logFetchError: str | None = None
        try:
            logText = fetchJobLog(repo, job.jobId)
        except RuntimeError as error:
            # 日志拉不到（如尚未归档 / token 失效）⇒ 视为空日志 ⇒ 判定为
            # 「缺完成标记 ⇒ 不可用」。**失败原因必须带进判定**，否则操作者
            # 分不清「还在跑」与「gh 挂了」。
            logText = ""
            logFetchError = sanitizeForReport(redactSecrets(str(error)))
        assessments.append(assessJob(job, logText, logFetchError))
    return _unionUsableFailures(assessments), tuple(assessments)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _reportFailureId(failureId: str) -> str:
    """失败用例 ID 的报告形态：净化后的可读 ID + 原始 ID 的指纹。

    指纹用于在被净化后仍能唯一定位同一个用例（净化会抹掉控制字符，
    两个不同的原始 ID 可能净化成同一个可读串）。
    """
    cleaned = sanitizeForReport(failureId)
    if cleaned == failureId:
        return cleaned
    digest = hashlib.sha256(failureId.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned} [sha256:{digest}]"


def renderReport(
    baseRunId: str,
    prRunId: str,
    baseFailures: frozenset[str],
    prFailures: frozenset[str],
    diff: FailureDiff,
    baseAssessments: Iterable[JobAssessment],
    prAssessments: Iterable[JobAssessment],
    verdict: Verdict,
    baseSha: str | None = None,
    prSha: str | None = None,
) -> str:
    """人读的结论文本。

    **一切外部字符串都过 `sanitizeForReport`** —— 否则 PR 作者可以在测试里 print
    一条带 ANSI 的「失败用例 ID」，用 CSI 序列擦掉已打印的结论行 ⇒ 伪造报告。
    ⚠️ 净化必须覆盖**每一个**插入点：漏掉一处（实测漏在 `status` 字段的
    `unusableReason` 上）就等于留了一条 OSC 52 注入通道。
    ⇒ 收尾再整体过一遍 `redactSecrets`（凭据可能出现在失败用例 ID 里，
    例如有人把 token 写进了测试名）。
    """
    baseTuple = tuple(baseAssessments)
    prTuple = tuple(prAssessments)
    baseShaText = sanitizeForReport(baseSha) if baseSha else "未知"
    prShaText = sanitizeForReport(prSha) if prSha else "未知"
    lines = [
        f"结论：{'可合并' if verdict.canMerge else '不可合并'}（{verdict.reasonCode}）",
        f"基线 run {sanitizeForReport(baseRunId)}（head {baseShaText}）：失败 {len(baseFailures)} 条",
        f"PR    run {sanitizeForReport(prRunId)}（head {prShaText}）：失败 {len(prFailures)} 条",
        f"净新增 {len(diff.added)} 条 / 净丢失 {len(diff.removed)} 条",
    ]
    if verdict.blockingReason is not None:
        lines.append(f"原因：{sanitizeForReport(verdict.blockingReason)}")
    if diff.removed:
        lines.append("⚠️ 净丢失失败用例 ⇒ 须人工确认不是「把失败藏起来了」：")
        lines.extend(f"  - {_reportFailureId(failureId)}" for failureId in diff.removed)
    for runLabel, assessments in (("基线", baseTuple), ("PR", prTuple)):
        for item in assessments:
            reason = item.unusableReason if item.unusableReason is not None else ""
            status = "可用" if item.isUsable else f"不可用（{sanitizeForReport(reason)}）"
            lines.append(
                f"  - [{runLabel}] {sanitizeForReport(item.job.name)} "
                f"[{sanitizeForReport(item.job.conclusion)}]：{status}，失败 {len(item.failureIds)} 条"
            )
            if item.hasFailureWithoutDetail:
                lines.append("      ⚠️ 该 job 真红但没有 FAILED 行 ⇒ 失败集合为空 ⇒ 不可信")
            if item.markerCount > 1:
                lines.append(
                    f"      ⚠️ 日志里出现 {item.markerCount} 条完成标记 ⇒ 疑似有伪造标记 ⇒ 不可信"
                )
    if diff.added:
        lines.append("净新增失败用例：")
        lines.extend(f"  + {_reportFailureId(failureId)}" for failureId in diff.added)
    return redactSecrets("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _invalidArgumentReason(repo: str, baseRunId: str, prRunId: str) -> str | None:
    """参数非法时给出原因；合法则返回 None。"""
    repoReason = validateRepo(repo)
    if repoReason is not None:
        return repoReason
    for label, runId in (("--base-run", baseRunId), ("--pr-run", prRunId)):
        runIdReason = validateRunId(runId)
        if runIdReason is not None:
            return f"{label} 非法（应为纯数字）：{sanitizeForReport(runId)}"
    if baseRunId == prRunId:
        return (
            "--base-run 与 --pr-run 不能是同一个 run id"
            "（自比自 ⇒ 失败集合必然相同 ⇒ 「净新增 0」是假的）"
        )
    return None


def _applyCiSurfaceGuard(
    verdict: Verdict,
    surface: CiSurfaceInfo,
    allowCiChanges: bool,
) -> Verdict:
    """CI 相关面守卫：PR 改了 CI 相关文件 ⇒ 失败集合不可比 ⇒ 不可判「可合并」。

    只在**本该判「可合并」**时才起作用（其他结论已经拦住了）。
    判定不出来（`surface.error` 非空）时**保守拦下** —— 「不知道」不等于「没有」。
    """
    if not verdict.canMerge or allowCiChanges:
        return verdict
    if surface.error is not None:
        return Verdict(
            canMerge=False,
            blockingReason=(
                "无法判定 PR 是否改动了 CI 相关面（改动文件清单拉取失败）"
                f" ⇒ 失败集合可比性未知 ⇒ 不判可合并：{surface.error}"
            ),
            reasonCode=_REASON_CI_SURFACE,
        )
    ciChanges = detectCiSurfaceChanges(surface.changedFiles)
    if not ciChanges:
        return verdict
    return Verdict(
        canMerge=False,
        blockingReason=(
            "PR 改动了 CI 相关面 ⇒ 两次 run 的失败集合不可比 ⇒ 对差不成立："
            f"{list(ciChanges)}"
            "（PR 可以通过 conftest / workflow / 分片清单让失败用例不产生 FAILED 行）"
            "⇒ 须人工确认；确知无影响时用 --allow-ci-changes 显式豁免"
        ),
        reasonCode=_REASON_CI_SURFACE,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    退出码：0 可合并 / 1 净新增失败 / 2 存在不可用 job / 3 运行错误 /
    4 覆盖不等价 / 5 净丢失失败待人工确认 / 6 CI 相关面被改动（集合不可比）。
    """
    parser = argparse.ArgumentParser(
        prog="app.utils.ci_failure_set",
        description="对差两次 CI run 的失败用例集合，给出合并判据结论",
    )
    parser.add_argument("--repo", required=True, help="仓库 slug，例如 sunql/QA")
    parser.add_argument("--base-run", required=True, help="基线 run id（main 上的那次）")
    parser.add_argument("--pr-run", required=True, help="PR run id")
    parser.add_argument(
        "--allow-ci-changes",
        action="store_true",
        help="PR 确实改动了 CI 相关文件（workflow / conftest / pytest 配置 / 分片清单）"
        "⇒ 失败集合不可比，此参数表示已人工确认无影响",
    )
    args = parser.parse_args(argv)

    invalidReason = _invalidArgumentReason(args.repo, args.base_run, args.pr_run)
    if invalidReason is not None:
        print(f"错误：{invalidReason}", file=sys.stderr)
        return 3

    try:
        baseFailures, baseAssessments = collectRunFromGithub(args.repo, args.base_run)
        prFailures, prAssessments = collectRunFromGithub(args.repo, args.pr_run)
    except _FETCH_ERRORS as error:
        # 注意：不要用 `::error::` 前缀 —— 那是 GH Actions 的 workflow command，
        # 本工具是本地 CLI；一旦输出被贴进 Actions 日志就会变成一条注解。
        print(f"错误：拉取 CI 日志失败：{redactSecrets(str(error))}", file=sys.stderr)
        return 3

    if not baseAssessments or not prAssessments:
        print(
            "错误：有一侧的 run 一个 job 都没有（run id 指错 / workflow 没跑起来 / "
            "job 列表尚未归档）⇒ 失败集合是空集 ⇒ 对差不成立",
            file=sys.stderr,
        )
        return 3

    surface = resolveCiSurface(args.repo, args.base_run, args.pr_run)
    diff = diffFailureSets(baseFailures, prFailures)
    verdict = verdictFor(diff, baseAssessments, prAssessments)
    verdict = _applyCiSurfaceGuard(verdict, surface, args.allow_ci_changes)

    try:
        report = renderReport(
            args.base_run,
            args.pr_run,
            baseFailures,
            prFailures,
            diff,
            baseAssessments,
            prAssessments,
            verdict,
            surface.baseSha,
            surface.prSha,
        )
    except Exception as error:  # noqa: BLE001 —— 报告渲染失败不能当成「可合并」
        # 渲染异常 ⇒ 结论不可见 ⇒ 绝不能落到 `return 0`（假绿）。
        print(f"错误：渲染报告失败：{redactSecrets(str(error))}", file=sys.stderr)
        return 3
    print(report)

    if verdict.canMerge:
        return 0
    if verdict.reasonCode == _REASON_COVERAGE:
        return 4
    if verdict.reasonCode == _REASON_NEEDS_REVIEW:
        return 5
    if verdict.reasonCode == _REASON_CI_SURFACE:
        return 6
    # 「不可用」优先于「净新增失败」：基线侧或 PR 侧只要有一个 job 不可用，
    # 结论就必须是「必须复跑」⇒ 退出码 2。
    unusable = [item for item in (*baseAssessments, *prAssessments) if not item.isUsable]
    return 2 if unusable else 1


if __name__ == "__main__":
    sys.exit(main())
