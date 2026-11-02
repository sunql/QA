"""CI 失败集合对差：把「PR 失败集合 ⊆ 基线失败集合」这条合并判据做成**可执行**工具。

背景
----
本仓的 CI 基线是**红的**（`main` 上即有既有失败）⇒ 合并判据不是「等 CI 变绿」，
而是**失败集合对差**：取同一个 CI 在 PR 与基线两次 run 的失败用例集合，
**净新增为空**才合并。

人工做这件事已经踩过四个坑（全部实测于 2026-11-02 的 PR #98 / #99 批次），
本模块把它们逐个封死：

1. **`cancelled` 的 job 失败集合是空的。** GH Actions 里 job 超时被标记为
   `cancelled`（**不是** `failure`），它的失败集合是**空的** ⇒ 直接拿它对差会得出
   「净新增 0」的**假结论**。⇒ `cancelled` / `skipped` / **缺完成标记**的 job
   一律判为**不可用**，必须复跑该 job 后再用。
2. **完成标记缺失 = 没跑完。** 每个分片 job 跑完必须打印
   `X_SHARD_DONE shard=N status=<pytest 退出码>`（unit 已有、integration 本批补上）。
   缺标记 ⇒ 该 job 在跑完前就死了 ⇒ 失败集合不可用。
3. **日志行带时间戳前缀。** GH Actions 的日志每行形如
   `2026-11-02T07:10:13.4960640Z FAILED app/tests/x.py::test_y`
   ⇒ 正则**不能锚定** `^FAILED`（那样一条都匹配不到）。
4. **`comm` 要求两侧同序同 collation。** python `sorted()`（码位序）与
   `sort -u`（locale 序）不同序时，`comm` 会输出**两边都非空**的假差异。
   ⇒ 本模块在 Python 里直接做集合差，**不依赖 shell `comm`**。

用法（本地，人工执行）::

    uv run python -m app.utils.ci_failure_set \
        --repo sunql/QA --base-run 41396973529 --pr-run 41402551747

退出码：

===== ==========================
码     含义
===== ==========================
0     净新增 0 且所有 job 可用 ⇒ 可合并
1     净新增失败非空 ⇒ 不可合并
2     存在不可用 job ⇒ 必须复跑后再对差
3     运行错误（gh 调用失败等）
===== ==========================
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# pytest 失败行：`FAILED <nodeid> [- reason]`
_FAILURE_PATTERN = re.compile(r"FAILED\s+(\S+)")
# pytest 收集期错误行：`ERROR <file>.py [- reason]`
# 只收 .py —— 避免把日志里其它 `ERROR xxx` 散文误当成失败用例。
_ERROR_PATTERN = re.compile(r"ERROR\s+(\S+\.py)")
# 分片完成标记：`UNIT_SHARD_DONE shard=1 status=1` / `INTEGRATION_SHARD_DONE shard=2 status=0`
_MARKER_PATTERN = re.compile(r"([A-Z][A-Z0-9_]*_SHARD_DONE)\s+shard=(\d+)\s+status=(-?\d+)")

# job 结论里，「跑完了且结果可信」的两种
_TRUSTED_CONCLUSIONS = frozenset({"success", "failure"})
# 结论与 marker 期望的对应关系
_CONCLUSION_BY_SUCCESS = {"success": True, "failure": False}

_GH_TIMEOUT_SEC = 300


@dataclass(frozen=True)
class CiJob:
    """CI run 里的一个 job。"""

    jobId: int
    name: str
    conclusion: str


@dataclass(frozen=True)
class ShardDoneMarker:
    """分片 job 的完成标记。"""

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


def extractFailureIds(logText: str) -> frozenset[str]:
    """从 job 日志里抽出失败用例 ID。

    正则**非锚定**：GH Actions 日志每行带 `2026-11-02T07:10:13.4960640Z ` 前缀，
    锚定 `^FAILED` 会一条都匹配不到。
    """
    found = {match.group(1) for match in _FAILURE_PATTERN.finditer(logText)}
    found.update(match.group(1) for match in _ERROR_PATTERN.finditer(logText))
    return frozenset(found)


def parseShardDoneMarkers(logText: str) -> list[ShardDoneMarker]:
    """抽取日志里的分片完成标记（通常 0 或 1 个）。"""
    markers = [
        ShardDoneMarker(name=match.group(1), shard=int(match.group(2)), status=int(match.group(3)))
        for match in _MARKER_PATTERN.finditer(logText)
    ]
    return markers


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


def assessJob(job: CiJob, logText: str) -> JobAssessment:
    """判定单个 job 的失败集合**是否可用**。

    不可用 ⇒ 它的失败集合是空的（`cancelled` / `skipped` / 没跑完），
    拿它对差会得出假的「净新增 0」⇒ 必须复跑该 job。
    """
    markers = parseShardDoneMarkers(logText)
    failureIds = tuple(sorted(extractFailureIds(logText)))

    reason: str | None = None
    marker: ShardDoneMarker | None = None

    if job.conclusion not in _TRUSTED_CONCLUSIONS:
        reason = f"job 结论为 {job.conclusion}（失败集合是空的，不可用于对差 ⇒ 必须复跑该 job）"
    elif not markers:
        reason = (
            "日志里缺少分片完成标记（形如 `X_SHARD_DONE shard=N status=<退出码>`）"
            "⇒ 该 job 没跑完，失败集合不可用"
        )
    elif len(markers) > 1:
        reason = f"日志里出现 {len(markers)} 个分片完成标记 ⇒ 无法确定该 job 是否跑完"
    else:
        marker = markers[0]
        reason = _markerMismatchReason(job, marker)

    isUsable = reason is None
    hasFailureWithoutDetail = bool(
        isUsable and marker is not None and marker.status != 0 and not failureIds
    )

    return JobAssessment(
        job=job,
        marker=marker,
        failureIds=failureIds,
        isUsable=isUsable,
        unusableReason=reason,
        hasFailureWithoutDetail=hasFailureWithoutDetail,
    )


def collectRunFailures(
    jobs: Iterable[CiJob],
    logs: Mapping[int, str],
) -> tuple[frozenset[str], tuple[JobAssessment, ...]]:
    """汇总一轮 run 的失败集合（**只取可用 job** 的）与逐 job 判定。"""
    assessments = tuple(assessJob(job, logs.get(job.jobId, "")) for job in jobs)
    failures: frozenset[str] = frozenset()
    for assessment in assessments:
        if assessment.isUsable:
            failures = failures | frozenset(assessment.failureIds)
    return failures, assessments


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
        f"{item.job.name}(job={item.job.jobId}): {item.unusableReason}"
        for item in assessments
        if not item.isUsable and item.unusableReason is not None
    )


def verdictFor(
    diff: FailureDiff,
    baseAssessments: Iterable[JobAssessment],
    prAssessments: Iterable[JobAssessment],
) -> Verdict:
    """给出合并判据的结论。

    **只要有任何一个 job 不可用，就不能输出「可合并」** —— 即使净新增为空。
    这正是「假净新增 0」的根因所在。
    """
    unusable = [
        item for item in (*tuple(baseAssessments), *tuple(prAssessments)) if not item.isUsable
    ]
    if unusable:
        return Verdict(
            canMerge=False,
            blockingReason=f"存在不可用 job（失败集合不可信 ⇒ 必须复跑后重新对差）：{_describeUnusable(unusable)}",
        )
    if diff.added:
        return Verdict(
            canMerge=False,
            blockingReason=f"净新增失败 {len(diff.added)} 条：{', '.join(diff.added)}",
        )
    return Verdict(canMerge=True, blockingReason=None)


# ---------------------------------------------------------------------------
# gh 适配层（CLI 用；纯函数部分不依赖网络）
# ---------------------------------------------------------------------------


def _runGh(args: list[str]) -> str:
    completed = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=_GH_TIMEOUT_SEC,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} 失败（退出码 {completed.returncode}）：{completed.stderr.strip()}"
        )
    return completed.stdout


def fetchJobs(repo: str, runId: str) -> list[CiJob]:
    """取一轮 run 的全部 job。"""
    payload = _runGh(["api", f"repos/{repo}/actions/runs/{runId}/jobs", "--paginate"])
    parsed = json.loads(payload)
    jobs = parsed.get("jobs", [])
    return [
        CiJob(
            jobId=int(item["id"]),
            name=str(item.get("name", "")),
            conclusion=str(item.get("conclusion") or ""),
        )
        for item in jobs
    ]


def fetchJobLog(repo: str, jobId: int) -> str:
    """取单个 job 的日志文本。

    `--allow-escape-sequences` 是必需的：GH Actions 的日志含终端转义序列，
    不加这个 flag `gh api` 会直接报错拒绝输出。
    """
    return _runGh(["api", f"repos/{repo}/actions/jobs/{jobId}/logs", "--allow-escape-sequences"])


def collectRunFromGithub(repo: str, runId: str) -> tuple[frozenset[str], tuple[JobAssessment, ...]]:
    """从 GitHub 拉一轮 run 的失败集合与逐 job 判定。"""
    jobs = fetchJobs(repo, runId)
    logs: dict[int, str] = {}
    for job in jobs:
        try:
            logs[job.jobId] = fetchJobLog(repo, job.jobId)
        except RuntimeError:
            # 日志拉不到（如尚未归档）⇒ 视为空日志 ⇒ 判定为「缺完成标记 ⇒ 不可用」
            logs[job.jobId] = ""
    return collectRunFailures(jobs, logs)


def renderReport(
    baseRunId: str,
    prRunId: str,
    baseFailures: frozenset[str],
    prFailures: frozenset[str],
    diff: FailureDiff,
    assessments: Iterable[JobAssessment],
    verdict: Verdict,
) -> str:
    """人读的结论文本。"""
    lines = [
        f"基线 run {baseRunId}：失败 {len(baseFailures)} 条",
        f"PR    run {prRunId}：失败 {len(prFailures)} 条",
        f"净新增 {len(diff.added)} 条 / 净丢失 {len(diff.removed)} 条",
    ]
    for item in assessments:
        status = "可用" if item.isUsable else f"不可用（{item.unusableReason}）"
        lines.append(f"  - {item.job.name} [{item.job.conclusion}]：{status}，失败 {len(item.failureIds)} 条")
        if item.hasFailureWithoutDetail:
            lines.append("      ⚠️ 该 job 真红但没有 FAILED 行（可能是收集期错误）⇒ 需人工核对")
    if diff.added:
        lines.append("净新增失败用例：")
        lines.extend(f"  + {failureId}" for failureId in diff.added)
    if diff.removed:
        lines.append("净丢失失败用例（确认是否被偷偷藏起来）：")
        lines.extend(f"  - {failureId}" for failureId in diff.removed)
    lines.append("结论：" + ("可合并" if verdict.canMerge else f"不可合并 —— {verdict.blockingReason}"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    退出码：0 可合并 / 1 净新增失败 / 2 存在不可用 job / 3 运行错误。
    """
    parser = argparse.ArgumentParser(
        prog="app.utils.ci_failure_set",
        description="对差两次 CI run 的失败用例集合，给出合并判据结论",
    )
    parser.add_argument("--repo", required=True, help="仓库 slug，例如 sunql/QA")
    parser.add_argument("--base-run", required=True, help="基线 run id（main 上的那次）")
    parser.add_argument("--pr-run", required=True, help="PR run id")
    args = parser.parse_args(argv)

    try:
        baseFailures, baseAssessments = collectRunFromGithub(args.repo, args.base_run)
        prFailures, prAssessments = collectRunFromGithub(args.repo, args.pr_run)
    except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        print(f"::error::拉取 CI 日志失败：{error}", file=sys.stderr)
        return 3

    diff = diffFailureSets(baseFailures, prFailures)
    verdict = verdictFor(diff, baseAssessments, prAssessments)
    print(renderReport(args.base_run, args.pr_run, baseFailures, prFailures, diff, prAssessments, verdict))

    if not verdict.canMerge:
        unusable = [item for item in prAssessments if not item.isUsable]
        return 2 if unusable else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
