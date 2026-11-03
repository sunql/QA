"""PR run 的**新鲜度**：这次 CI 到底跑的是不是 PR 当前那版代码？

背景
----
「失败集合对差」的前提是 **PR run 跑的就是 PR 当前那版代码**。
若 PR 又推了新 commit，而操作者拿的是**上一次** push 的 run id
⇒ 对差的是**旧代码**的失败集合 ⇒ 「净新增 0」是假的
（而 GitHub 上最新那次 CI 可能已经红了）。

⇒ 本模块把 PR run 的 head_sha 与**该分支当前的 head sha**对比：
不一致 ⇒ run 已过期 ⇒ 对差不成立 ⇒ 必须用最新一次的 run 重跑对差。
"""

from __future__ import annotations

from app.utils import ci_head
from app.utils.ci_gh import RunHeadInfo


def _headInfo(
    headSha: str = "a" * 40,
    headBranch: str = "feat/ci-integration-sharding",
    headRepo: str = "sunql/QA",
) -> RunHeadInfo:
    return RunHeadInfo(headSha=headSha, headBranch=headBranch, headRepo=headRepo)


def test_resolveHeadFreshness_rejects_stale_run(monkeypatch) -> None:
    """run 跑的 sha 与分支当前 head 不一致 ⇒ **run 已过期** ⇒ 必须拦下。

    这是 2026-11-03 代码审查 M-1 指出的漏洞：工具拿 `surface.prSha` 印在报告里
    却**从不**与 PR 当前 head 比较 ⇒ 拿旧 run 对差会得出「净新增 0」。
    """
    monkeypatch.setattr(ci_head, "fetchRunHeadInfo", lambda repo, runId: _headInfo())
    monkeypatch.setattr(ci_head, "fetchBranchHeadSha", lambda repo, branch: "b" * 40)

    result = ci_head.resolveHeadFreshness("sunql/QA", "222", "a" * 40)

    assert result.isFresh is False
    assert result.reason is not None
    assert "过期" in result.reason


def test_resolveHeadFreshness_accepts_current_run(monkeypatch) -> None:
    """run 的 sha 就是分支当前 head ⇒ 新鲜 ⇒ 放行（反向守卫，别误拦）。"""
    monkeypatch.setattr(ci_head, "fetchRunHeadInfo", lambda repo, runId: _headInfo())
    monkeypatch.setattr(ci_head, "fetchBranchHeadSha", lambda repo, branch: "a" * 40)

    result = ci_head.resolveHeadFreshness("sunql/QA", "222", "a" * 40)

    assert result.isFresh is True
    assert result.reason is None


def test_resolveHeadFreshness_notes_fork_pr_without_blocking(monkeypatch) -> None:
    """PR 来自 fork ⇒ 分支不在本仓库 ⇒ **判不出来** ⇒ 不拦，但必须留下人工确认提示。

    拦下来会让工具对 fork PR 完全不可用 ⇒ 反而没人用 ⇒ 判为「不拦 + 明示」。
    （这是本工具唯一一处「判不出来却不拦」的地方，必须在报告里显式提示。）
    """
    monkeypatch.setattr(
        ci_head, "fetchRunHeadInfo", lambda repo, runId: _headInfo(headRepo="someone/QA")
    )

    def mustNotBeCalled(repo: str, branch: str) -> str:
        raise AssertionError("fork 的分支不在本仓库，不该去查它的 head")

    monkeypatch.setattr(ci_head, "fetchBranchHeadSha", mustNotBeCalled)

    result = ci_head.resolveHeadFreshness("sunql/QA", "222", "a" * 40)

    assert result.isFresh is True
    assert result.reason is None
    assert result.note is not None
    assert "人工确认" in result.note


def test_resolveHeadFreshness_blocks_when_head_cannot_be_fetched(monkeypatch) -> None:
    """分支 head 拉不到 ⇒ **判定不出来** ⇒ 保守拦下（「不知道」不等于「没有」）。"""

    def boom(repo: str, branch: str) -> str:
        raise RuntimeError("gh 挂了")

    monkeypatch.setattr(ci_head, "fetchRunHeadInfo", lambda repo, runId: _headInfo())
    monkeypatch.setattr(ci_head, "fetchBranchHeadSha", boom)

    result = ci_head.resolveHeadFreshness("sunql/QA", "222", "a" * 40)

    assert result.isFresh is False
    assert result.reason is not None
    assert "gh 挂了" in result.reason


def test_resolveHeadFreshness_blocks_when_run_head_cannot_be_fetched(monkeypatch) -> None:
    """run 自己的 head 信息拉不到 ⇒ 同样判定不出来 ⇒ 保守拦下。"""

    def boom(repo: str, runId: str) -> RunHeadInfo:
        raise RuntimeError("gh 挂了")

    monkeypatch.setattr(ci_head, "fetchRunHeadInfo", boom)

    result = ci_head.resolveHeadFreshness("sunql/QA", "222", "a" * 40)

    assert result.isFresh is False
    assert result.reason is not None
