"""CI 失败集合的**判定**：哪些 job 的失败集合可用、两次 run 的差是什么、结论是什么。

为什么单独一个模块：判定是本工具的**核心语义**，规则密、耦合紧
（标记协议 / pytest 退出码 / 日志旁证 / 覆盖等价 / 结论优先级）⇒ 集中一处，
便于逐条加守卫与做变异取证。CLI（参数、退出码、报告打印）在
`app.utils.ci_failure_set`；报告渲染在 `app.utils.ci_report`。

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
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.utils.ci_gh import fetchJobLog, fetchJobPayloads
from app.utils.ci_pytest_log import (
    INVALID_SHARD,
    ShardDoneMarker,
    extractFailureIds,
    hasPytestSummary,
    parseShardDoneMarkers,
    summaryCountMismatchReason,
)
from app.utils.ci_text import sanitizeForReport

# 从 job 名推出期望的完成标记：`Backend / unit shard 0` ⇒ `UNIT_SHARD_DONE` + shard=0。
# 非分片 job（`Frontend / typecheck + coverage`）⇒ None（**不要求标记**）。
_JOB_MARKER_PATTERN = re.compile(r"(?i)\b(unit|integration)\b\s+shard\s+(\d+)\s*$")
# job 名里出现这些词 ⇒ 它**看起来**是分片 job ⇒ 必须符合上面的协议。
# （协议被改过 / job 被换掉 ⇒ 不可信 ⇒ 不能降级成「非分片 job」。）
_SHARD_LIKE_PATTERN = re.compile(r"(?i)\b(unit|integration|shard)\b")

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


def _logEvidenceReason(
    logText: str,
    marker: ShardDoneMarker,
    extractedIds: tuple[str, ...],
) -> str | None:
    """「pytest 真的跑完了整个 session」的三项旁证；都通过则返回 None。

    1. **收尾汇总行**（对所有 status 都要求，不只 status=1）：
       一个用例里 `os._exit(0)` 就能让 pytest 中途夭折却留下退出码 0
       ⇒ CI 脚本照样打出「真绿」⇒ 该分片一条用例都没跑完却贡献一个**空**失败集合
       ⇒ 对差得出假的「净新增 0」⇒ 而 GitHub 上 CI 还全绿 ⇒ 没有二次防线。
       旧实现只在 status=1 时要求 ⇒ 绿的那侧完全没有「跑完了」的证据。
    2. **汇总行的失败数 == 解析出的失败 ID 数** ⇒ 失败集合是完整的。
    3. **真红必有明细** ⇒ 否则失败集合是空的。
    """
    if not hasPytestSummary(logText):
        return (
            "日志里没有 pytest 的收尾汇总行"
            "（形如 `2 failed, 118 passed in 812.34s`）"
            "⇒ 无法确认 pytest 跑完了整个 session"
            "⇒ 失败集合可能只覆盖前半截 ⇒ 不可信"
        )
    countReason = summaryCountMismatchReason(logText)
    if countReason is not None:
        return countReason
    if marker.status != 0 and not extractedIds:
        return (
            "完成标记显示 pytest 真红（status=1），但日志里一条 FAILED/ERROR 都没有"
            "⇒ 失败集合是空的 ⇒ 不可信"
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
    if marker.shard == INVALID_SHARD:
        # CI 脚本在**分片号非法**时打哨兵值 -1 ⇒ 该 job 一个用例都没跑
        # ⇒ 它的失败集合是空的 ⇒ 参与对差只会掩盖真实失败 ⇒ 必须拦下。
        # （旧实现打 0 ⇒ 这里会被误判成「标记与 job 不符」，真实原因被掩盖。）
        return None, (
            "完成标记 shard=-1 ⇒ CI 脚本判定分片号非法 ⇒ 该 job 一个用例都没跑"
            "⇒ 失败集合是空的 ⇒ 不可信"
        )
    reason = _markerMismatchReason(job, marker)
    if reason is None and (marker.name != expectedMarker[0] or marker.shard != expectedMarker[1]):
        reason = (
            f"完成标记 {marker.name} shard={marker.shard} 与 job「{sanitizeForReport(job.name)}」"
            f"期望的 {expectedMarker[0]} shard={expectedMarker[1]} 不符 ⇒ 不可信"
        )
    if reason is None:
        reason = _incompleteRunReason(marker)
    if reason is None:
        reason = _logEvidenceReason(logText, marker, extractedIds)
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
            logFetchError = sanitizeForReport(str(error))
        assessments.append(assessJob(job, logText, logFetchError))
    return _unionUsableFailures(assessments), tuple(assessments)
