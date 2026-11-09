"""PR run 的**新鲜度**判定：这次 CI 到底跑的是不是 PR 当前那版代码？

背景
----
「失败集合对差」的前提是 **PR run 跑的就是 PR 当前那版代码**。
若 PR 又推了新 commit，而操作者拿的是**上一次** push 的 run id
⇒ 对差的是**旧代码**的失败集合 ⇒ 「净新增 0」是假的
（而 GitHub 上最新那次 CI 可能已经红了，而且没人看）。

⇒ 本模块把 PR run 的 head_sha 与**该分支当前的 head sha**对比：
不一致 ⇒ run 已过期 ⇒ 对差不成立 ⇒ 必须用最新一次的 run 重跑对差。

⚠️ 威胁模型的边界（与 `ci_failure_set` 一致）：
本模块核对的是「run 记录的 head sha」与「GitHub 上分支当前 head」两个
**服务端事实** ⇒ 不受 PR 作者对日志内容的控制影响。
但它**不能**证明 run 本身是真的跑完了 —— 那由 `ci_failure_set` 的
完成标记 / 汇总行判据负责。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from app.utils.ci_gh import fetchBranchHeadSha, fetchRunHeadInfo
from app.utils.ci_text import sanitizeForReport

# 拉取失败时可能被抛出的异常类型（`gh` 调用 + JSON 解析）。
_FETCH_ERRORS = (
    RuntimeError,
    OSError,
    ValueError,
    KeyError,
    AttributeError,
    subprocess.SubprocessError,
)


@dataclass(frozen=True)
class HeadFreshness:
    """PR run 新鲜度判定结果。

    `reason` 非空 ⇒ **不可用**（必须用最新一次的 run 重跑对差）。
    `note` 非空 ⇒ 不拦，但必须在报告里明示（当前只有 fork PR 这一种情况）。
    """

    isFresh: bool
    reason: str | None
    note: str | None


def resolveHeadFreshness(repo: str, runId: str, runSha: str) -> HeadFreshness:
    """判断 `runId` 这轮 run 是否跑在分支的**当前** head 上。

    - run 的 head sha == 分支当前 head ⇒ 新鲜；
    - 不等 ⇒ **已过期**（PR 推了新 commit）⇒ 拦下；
    - PR 来自 fork ⇒ 分支不在本仓库 ⇒ 判不出来 ⇒ **不拦但明示**
      （拦下来会让工具对 fork PR 完全不可用 ⇒ 反而没人用）；
    - 其余拉取失败 ⇒ 判不出来 ⇒ **保守拦下**（「不知道」不等于「没有」）。
    """
    try:
        headInfo = fetchRunHeadInfo(repo, runId)
    except _FETCH_ERRORS as error:
        return HeadFreshness(
            isFresh=False,
            reason=f"拉取 run {runId} 的 head 信息失败 ⇒ 无法确认 run 是否最新：{sanitizeForReport(str(error))}",
            note=None,
        )
    # ⚠️ **大小写不敏感**比较（2026-11-07 安全评审 MEDIUM-1）：
    # GitHub 的仓库名大小写不敏感，API 侧会原样返回 `Sunql/QA` 这类形态
    # ⇒ 严格相等会把**同一个仓库**当成 fork ⇒ 直接跳过新鲜度核对
    # ⇒ 「run 已过期」这条守卫**静默失效**（fail-open）。
    # 与 `ci_surface._baseTrustError` 的比较方式保持一致。
    if headInfo.headRepo.lower() != repo.lower():
        return HeadFreshness(
            isFresh=True,
            reason=None,
            note=(
                f"PR 来自 fork 仓库（{sanitizeForReport(headInfo.headRepo)}）"
                "⇒ 分支不在本仓库，无法自动核对 run 是否最新 ⇒ 请人工确认用的是最新一次 run"
            ),
        )
    try:
        currentSha = fetchBranchHeadSha(repo, headInfo.headBranch)
    except _FETCH_ERRORS as error:
        return HeadFreshness(
            isFresh=False,
            reason=(
                f"拉取分支 {sanitizeForReport(headInfo.headBranch)} 的当前 head 失败"
                f" ⇒ 无法确认 run 是否最新：{sanitizeForReport(str(error))}"
            ),
            note=None,
        )
    if currentSha != runSha:
        return HeadFreshness(
            isFresh=False,
            reason=(
                f"PR run {runId} 跑的是 {sanitizeForReport(runSha)}，"
                f"而分支 {sanitizeForReport(headInfo.headBranch)} 当前 head 已是 "
                f"{sanitizeForReport(currentSha)} ⇒ run 已过期（PR 推了新 commit）"
                "⇒ 对差的是旧代码 ⇒ 须用最新一次的 run 重跑对差"
            ),
            note=None,
        )
    return HeadFreshness(isFresh=True, reason=None, note=None)
