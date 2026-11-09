"""main 的 tip（= PR run 的 main 侧底座 `M_T`）的取数与筛选。

背景（2026-11-07 N6 第三版）：`ci.yml` 对 `pull_request` 触发且三个
`actions/checkout` **都不带 `with: ref:`** ⇒ PR 的 CI 检出的是
`refs/pull/N/merge` ⇒ 它跑的 commit 的**第一父**就是那次合并的 main 侧底座。

⇒ `M_T` = **PR run 事件派发那一刻 main 的 tip**（用 **`created_at`** 取上界）
⇒ 判定集合 = `A ∪ P`（`A` = 基线树 → `M_T` 的推进、`P` = PR 自己的改动）。

⚠️ **为什么锚 `created_at` 而不是 `run_started_at`**（2026-11-07 代码评审 CRITICAL-1）：
`pull_request` 事件的**合并 commit 在事件派发那一刻就被冻结**了 ⇒ 复跑（re-run）
**不会**重新计算它 ⇒ 复跑检出的仍是**同一个**合并 commit ⇒ 它的 main 侧底座是
**派发那一刻**的 main。实测（PR #105 run `41908498247`）：合并 commit `57a267a9`
的 committer.date = `20:38:18Z`（= run 的 `created_at` `20:38:19Z` 的前 1 秒）、
第一父 = `83581e10`；而 attempt-2（`run_started_at=22:24:11Z`）的 job 日志写着
`git fetch … +57a267a93f…:refs/remotes/pull/105/merge` ⇒ **同一个**合并 commit。

⚠️ 为什么不取 `pulls/{n}.merge_commit_sha`：它是 `refs/pull/N/merge` 的**当前值**
⇒ main 一推进就变 ⇒ 判定的「第二棵树」可能不是那次 run 执行的树
⇒ 假拦 + fail-open；且 PR 合并后该 ref 404 ⇒ 历史 run 无法回溯。

本文件钉「取数与筛选」的规则；判定集合的合成见 `test_ci_surface.py`。
"""

from __future__ import annotations

import json

import pytest

from app.utils import ci_gh

# --------------------------------------------------------------------------
# main 的 tip（= PR run 的 main 侧底座 `M_T`）
# --------------------------------------------------------------------------


def _mainRun(
    createdAt: str,
    headSha: str = "a" * 40,
    event: str = "push",
    headBranch: str = "main",
    headRepo: str = "sunql/QA",
) -> dict:
    """一条 main 的 push run（`workflow_runs` 里的一条）。"""
    return {
        "created_at": createdAt,
        "head_sha": headSha,
        "event": event,
        "head_branch": headBranch,
        "head_repository": {"full_name": headRepo},
    }


def _runsPayload(runs: list[dict]) -> str:
    """`gh api --paginate --slurp` 的返回形态：**页的列表**。"""
    return json.dumps([{"workflow_runs": runs}])


def test_parseIsoTimestamp_accepts_the_github_shape() -> None:
    assert ci_gh._parseIsoTimestamp("2026-11-06T19:38:13Z") is not None


def test_parseIsoTimestamp_rejects_a_malformed_value() -> None:
    """形态不符 ⇒ None（不是异常）⇒ 调用方自己决定「判定不出来」还是「跳过」。"""
    assert ci_gh._parseIsoTimestamp("") is None
    assert ci_gh._parseIsoTimestamp("2026-11-06 19:38:13") is None
    assert ci_gh._parseIsoTimestamp("not-a-time") is None


def test_mainTipCutoff_prefers_created_at() -> None:
    """上界必须锚在 **`created_at`**（事件派发时刻），不是 `run_started_at`。

    ⚠️ 2026-11-07 代码评审 CRITICAL-1：`pull_request` 事件的**合并 commit
    在事件派发那一刻就被冻结**了 ⇒ 复跑（re-run）**不会**重新计算它
    ⇒ 复跑检出的仍是**同一个**合并 commit ⇒ 它的 main 侧底座是
    **派发那一刻**的 main。

    实测（PR #105 run `41908498247`）：`created_at=2026-11-04T20:38:19Z`、
    `run_started_at=2026-11-04T22:24:11Z`（attempt=2）；合并 commit `57a267a9`
    的 committer.date = `20:38:18Z`、第一父 = `83581e10`（= 派发那一刻的 main）；
    attempt-2 的 job 日志写着
    `git fetch … +57a267a93f…:refs/remotes/pull/105/merge`
    ⇒ 复跑跑的仍是**同一个**合并 commit。

    ⇒ 用 `run_started_at` ⇒ `M_T` 取到**派发之后**的 main ⇒ `A` 腿把
    「派发之后 main 才推进的 CI 改动」也算进来 ⇒ 判定集偏大 ⇒ 假拦；
    而闸门侧若同样用它取基线 ⇒ 基线树与执行树都落到那个更晚的 main
    ⇒ `A` 变成空集 ⇒ 两次 run 实际跑的 CI 定义不同却判「可合并」⇒ **fail-open**。
    """
    cutoff = ci_gh.mainTipCutoff(createdAt="2026-11-04T20:38:19Z")

    assert cutoff.isoformat().startswith("2026-11-04T20:38:19")


def test_mainTipCutoff_applies_no_grace_window() -> None:
    """上界 = 锚点**原值**，**不加宽限**。

    ⚠️ 2026-11-07 代码评审 MEDIUM-1：闸门侧（`merge-gate.yml`）取基线用的
    上界是 `PR_RUN_CREATED_AT`（**无宽限**）⇒ 工具侧若多给 2 分钟
    ⇒ 工具侧的 `M_T` 比闸门侧的基线**更新**
    ⇒ `A` 腿把「派发之后 main 才推进的 CI 改动」也算进来
    ⇒ **判定集偏大 ⇒ 假拦**（main 在 PR 派发后 2 分钟内推进过就中招）。
    ⇒ 两边必须**同一个上界、同一个比较符** ⇒ 工具侧也用 `created_at` 原值。
    """
    anchor = ci_gh._parseIsoTimestamp("2026-11-04T20:38:19Z")
    assert anchor is not None

    cutoff = ci_gh.mainTipCutoff(createdAt="2026-11-04T20:38:19Z")

    assert cutoff == anchor, "上界多加了宽限 ⇒ 与闸门侧不同 ⇒ 假拦"


def test_mainTipCutoff_does_not_fall_back_to_run_started_at() -> None:
    """`created_at` 缺失 ⇒ **不**回退 ⇒ 判定不出来（fail-closed）。

    ⚠️ 2026-11-07 代码评审 MEDIUM-3（fail-closed 对称）：
    闸门侧（`merge-gate.yml`）没有回退 —— `PR_RUN_CREATED_AT` 为空
    ⇒ 直接 fail-closed（退出码 3）⇒ 工具侧若回退 `run_started_at`
    ⇒ 两边的上界不同 ⇒ 基线树 ≠ 执行树 ⇒ **fail-open**。
    ⇒ **两边必须同时 fail-closed**。
    """
    with pytest.raises(ValueError):
        ci_gh.mainTipCutoff(createdAt="")


def test_mainTipCutoff_rejects_the_removed_run_started_at_parameter() -> None:
    """`runStartedAt` 这个参数**已被删除** ⇒ 传它必须报错（而不是被静默忽略）。

    ⚠️ 静默忽略 = 调用方以为自己传了兜底 ⇒ 实际没有 ⇒ 两侧上界不同
    ⇒ 又是「基线树 ≠ 执行树」⇒ fail-open。
    """
    with pytest.raises(TypeError):
        ci_gh.mainTipCutoff(  # type: ignore[call-arg]
            createdAt="2026-11-04T20:38:19Z", runStartedAt="2026-11-04T22:24:11Z"
        )


def test_mainTipCutoff_requires_explicitly_named_timestamps() -> None:
    """上界的两个时间戳必须**具名传** ⇒ 杜绝「位置传参写反」。

    ⚠️ 这次的真实缺陷正是「位置传参 + 锚点写反」：早期实现把
    `run_started_at` 放在第一个位置 ⇒ 调用方很容易把两个值传反
    ⇒ 而**传反了不会报错**（两个都是合法时间戳）⇒ 判定静默用错锚点
    ⇒ fail-open。
    ⇒ 改成 keyword-only ⇒ 传参顺序再也没法搞错。
    """
    with pytest.raises(TypeError):
        ci_gh.mainTipCutoff("2026-11-04T20:38:19Z")  # type: ignore[misc]


def test_mainTipCutoff_raises_when_created_at_is_missing() -> None:
    """`created_at` 没有 ⇒ 算不出上界 ⇒ 判定不出来（不能当成「无上界」）。

    ⚠️ 空上界会让「不晚于上界」恒真 ⇒ `M_T` 退化成「main 上最新的 run」
    ⇒ 两次 run 的代码差异被放大 ⇒ **系统性假拦**。
    """
    with pytest.raises(ValueError):
        ci_gh.mainTipCutoff(createdAt="")


def test_the_pr105_rerun_anchor_picks_the_dispatch_time_main_tip() -> None:
    """PR #105 复跑的**实证回归**锚点：上界锚 `created_at` ⇒ `M_T` = 派发那一刻的 main。

    ⚠️ 2026-11-07 代码评审 CRITICAL-1 的实证材料：
    - PR #105 run `41908498247`：`created_at=2026-11-04T20:38:19Z`、
      `run_started_at=2026-11-04T22:24:11Z`（attempt=2）；
    - 合并 commit `57a267a9` 的第一父 = `83581e10`（main 在**派发那一刻**的 tip）；
    - main 的 push run：`83581e10` 的 run 创建于 `19:49:02Z`（run `41900595777`）；
      `8411697e` 的 run 创建于 `22:23:53Z`（run `41924916158`，那次 main 只改了
      `.github/workflows/merge-gate.yml`）。

    ⇒ 上界锚 `created_at`（`20:38:19Z`）⇒ 只命中 `19:49:02Z`
      那次 run ⇒ `M_T` = `83581e10` = 合并 commit 的第一父 ⇒ **一致**。
    ⇒ 锚 `run_started_at`（⇒ `22:24:11Z`）⇒ 会命中 `22:23:53Z` 那次
      ⇒ `M_T` = `8411697e` ≠ 实际执行树的底座 ⇒ `A` 被算空 ⇒ fail-open。
    """
    payload = _runsPayload(
        [
            _mainRun("2026-11-04T19:49:02Z", headSha="8" * 40),
            _mainRun("2026-11-04T22:23:53Z", headSha="4" * 40),
        ]
    )

    cutoff = ci_gh.mainTipCutoff(createdAt="2026-11-04T20:38:19Z")
    sha = ci_gh._parseMainTipSha(payload, cutoff.isoformat(), "sunql/QA")

    assert sha == "8" * 40, (
        "锚点用错 ⇒ M_T 取到派发之后的 main ⇒ A 腿被算空 ⇒ 两次 run 的 CI 定义"
        "可能其实不同却判「可合并」⇒ fail-open"
    )

    # 对照：锚点若换成复跑的开跑时刻 ⇒ 取到的是**另一个** commit
    # ⇒ 这条 fixture 确实有区分力（否则上面那条断言是恒真的）。
    wrongCutoff = ci_gh.mainTipCutoff(createdAt="2026-11-04T22:24:11Z")
    wrongSha = ci_gh._parseMainTipSha(payload, wrongCutoff.isoformat(), "sunql/QA")

    assert wrongSha == "4" * 40, "锚点换错时结果没变 ⇒ 这条 fixture 没有区分力"


# --------------------------------------------------------------------------
# 第二条腿：沿 main 的**第一父链**回溯（2026-11-07 代码评审 MEDIUM-2）
# --------------------------------------------------------------------------


def _commitEntry(
    sha: str,
    committedAt: str,
    parents: list[str] | None = None,
) -> dict:
    """`GET /repos/{o}/{r}/commits` 响应里的一条 commit。"""
    return {
        "sha": sha,
        "commit": {"committer": {"date": committedAt}},
        "parents": [{"sha": parent} for parent in (parents or [])],
    }


def _stubMainHistory(
    monkeypatch: pytest.MonkeyPatch,
    tipSha: str,
    history: list[dict],
) -> None:
    """把 `_runGh` 换成走「git ref + commits 列表」的假实现。"""
    historyPayload = json.dumps(history)

    def fakeRunGh(args: list[str]) -> str:
        path = args[1]
        if path.endswith("/git/ref/heads/main"):
            return json.dumps({"object": {"sha": tipSha}})
        if "/commits?" in path:
            return historyPayload
        raise AssertionError(f"测试里没准备这个 API 路径：{path}")

    monkeypatch.setattr(ci_gh, "_runGh", fakeRunGh)


def test_fetchMainFirstParentTipAt_walks_back_to_the_first_commit_before_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """沿第一父链回溯 ⇒ 取到「日期 ≤ 上界」的**主线** commit。

    ⚠️ 为什么不能直接用 `commits?sha=main&until=<上界>`：它返回的是
    「从 main **可达**、且日期 ≤ 上界的最新提交」⇒ 那个提交**可能是侧提交**
    （后来被 merge 进 main 的分支上的提交，作者日期早于上界、但它进 main 的
    时刻在上界之后）⇒ 它的 diff **不是** main 在那一刻的推进 ⇒ **假拦**。
    """
    tip = "f" * 40
    middle = "e" * 40
    base = "d" * 40
    _stubMainHistory(
        monkeypatch,
        tip,
        [
            _commitEntry(tip, "2026-11-06T19:39:00Z", [middle]),
            _commitEntry(middle, "2026-11-06T19:37:00Z", [base]),
            _commitEntry(base, "2026-11-06T19:30:00Z"),
        ],
    )

    sha = ci_gh.fetchMainFirstParentTipAt("sunql/QA", "2026-11-06T19:38:13+00:00")

    assert sha == middle


def test_fetchMainFirstParentTipAt_never_returns_a_side_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """侧提交（第二父）**永远不在主线**上 ⇒ 不得被回溯链选中。

    ⚠️ 这是「侧提交危害」的**正面回归**：旧实现（`commits?until=`）会返回
    侧提交 ⇒ 它的 diff 不是 main 的推进 ⇒ 假拦 + 归因错误。
    """
    tip = "a" * 40
    mainLine = "b" * 40
    sideCommit = "c" * 40
    _stubMainHistory(
        monkeypatch,
        tip,
        [
            # tip 的**第一父**是主线 commit，**第二父**是日期更早的侧提交
            _commitEntry(tip, "2026-11-06T19:39:00Z", [mainLine, sideCommit]),
            _commitEntry(mainLine, "2026-11-06T19:37:00Z"),
            _commitEntry(sideCommit, "2026-11-06T19:35:00Z"),
        ],
    )

    sha = ci_gh.fetchMainFirstParentTipAt("sunql/QA", "2026-11-06T19:38:13+00:00")

    assert sha == mainLine, "回溯链走到了侧提交 ⇒ 那是 main 的旁支，不是主线"


def test_fetchMainFirstParentTipAt_raises_on_a_malformed_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史条目畸形 ⇒ **报错**（fail-closed），不静默跳过。

    ⚠️ 静默跳过 = 那条提交从历史里消失 ⇒ 回溯链断掉 ⇒ 拿到的 tip 是错的
    ⇒ `A` 腿算错 ⇒ **fail-open**。
    """
    _stubMainHistory(monkeypatch, "a" * 40, [_commitEntry("a" * 40, "not-a-time")])

    with pytest.raises(ValueError):
        ci_gh.fetchMainFirstParentTipAt("sunql/QA", "2026-11-06T19:38:13+00:00")


def test_fetchMainFirstParentTipAt_raises_on_an_empty_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史为空 ⇒ 算不出底座 ⇒ 报错（fail-closed）。"""
    _stubMainHistory(monkeypatch, "a" * 40, [])

    with pytest.raises(ValueError):
        ci_gh.fetchMainFirstParentTipAt("sunql/QA", "2026-11-06T19:38:13+00:00")


def test_fetchMainFirstParentTipAt_raises_when_the_chain_is_too_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回溯跳数超上限 ⇒ 报错（fail-closed）。

    ⚠️ 不能「回溯到跳不动为止然后拿个错的 sha 当底座」⇒ 那会让 `A` 腿算错
    ⇒ fail-open。
    """
    chainLength = ci_gh._MAX_FIRST_PARENT_HOPS + 2
    shas = [f"{index:040x}" for index in range(chainLength)]
    history = [
        _commitEntry(shas[index], "2026-11-06T19:39:00Z", [shas[index + 1]])
        for index in range(chainLength - 1)
    ]
    history.append(_commitEntry(shas[-1], "2026-11-06T19:39:00Z", []))
    _stubMainHistory(monkeypatch, shas[0], history)

    with pytest.raises(ValueError):
        ci_gh.fetchMainFirstParentTipAt("sunql/QA", "2026-11-06T19:38:13+00:00")


def test_parseMainTipSha_takes_the_latest_run_not_later_than_the_cutoff() -> None:
    payload = _runsPayload(
        [
            _mainRun("2026-11-06T19:37:16Z", headSha="e" * 40),
            _mainRun("2026-11-06T19:30:00Z", headSha="d" * 40),
        ]
    )

    sha = ci_gh._parseMainTipSha(payload, "2026-11-06T19:38:13+00:00", "sunql/QA")

    assert sha == "e" * 40


def test_parseMainTipSha_ignores_a_run_after_the_cutoff() -> None:
    """上界之后才创建的 run **不算**（它是 PR 开跑之后才推的 main）。"""
    payload = _runsPayload(
        [
            _mainRun("2026-11-06T19:37:16Z", headSha="e" * 40),
            _mainRun("2026-11-06T19:39:00Z", headSha="f" * 40),
        ]
    )

    sha = ci_gh._parseMainTipSha(payload, "2026-11-06T19:38:13+00:00", "sunql/QA")

    assert sha == "e" * 40


def test_parseMainTipSha_ignores_a_pull_request_run() -> None:
    """`branch=main` 的过滤**不区分事件** ⇒ PR run 也会混进来 ⇒ 必须自己筛。"""
    payload = _runsPayload(
        [
            _mainRun("2026-11-06T19:37:16Z", headSha="e" * 40),
            _mainRun("2026-11-06T19:38:00Z", headSha="f" * 40, event="pull_request"),
        ]
    )

    sha = ci_gh._parseMainTipSha(payload, "2026-11-06T19:38:13+00:00", "sunql/QA")

    assert sha == "e" * 40


def test_parseMainTipSha_ignores_a_run_on_another_branch() -> None:
    payload = _runsPayload(
        [
            _mainRun("2026-11-06T19:37:16Z", headSha="e" * 40),
            _mainRun("2026-11-06T19:38:00Z", headSha="f" * 40, headBranch="feat/other"),
        ]
    )

    sha = ci_gh._parseMainTipSha(payload, "2026-11-06T19:38:13+00:00", "sunql/QA")

    assert sha == "e" * 40


def test_parseMainTipSha_ignores_a_run_from_another_repository() -> None:
    """fork 上一个**也叫 main** 的分支 ⇒ 必须排除（否则基线 = 攻击者的代码）。"""
    payload = _runsPayload(
        [
            _mainRun("2026-11-06T19:37:16Z", headSha="e" * 40),
            _mainRun("2026-11-06T19:38:00Z", headSha="f" * 40, headRepo="someone/QA"),
        ]
    )

    sha = ci_gh._parseMainTipSha(payload, "2026-11-06T19:38:13+00:00", "sunql/QA")

    assert sha == "e" * 40


def test_parseMainTipSha_matches_the_repo_name_case_insensitively() -> None:
    """仓库名大小写不同也算同一个仓库（否则合法的基线会被判成「别人的」）。"""
    payload = _runsPayload(
        [_mainRun("2026-11-06T19:37:16Z", headSha="e" * 40, headRepo="Sunql/QA")]
    )

    sha = ci_gh._parseMainTipSha(payload, "2026-11-06T19:38:13+00:00", "sunql/QA")

    assert sha == "e" * 40


def test_parseMainTipSha_raises_when_no_run_qualifies() -> None:
    """一条都不合格 ⇒ 判定不出来 ⇒ 报错（不能返回「随便一个」）。"""
    payload = _runsPayload([_mainRun("2026-11-06T19:39:00Z", headSha="f" * 40)])

    with pytest.raises(ValueError):
        ci_gh._parseMainTipSha(payload, "2026-11-06T19:38:13+00:00", "sunql/QA")


def test_parseMainTipSha_rejects_a_malformed_cutoff() -> None:
    payload = _runsPayload([_mainRun("2026-11-06T19:37:16Z", headSha="e" * 40)])

    with pytest.raises(ValueError):
        ci_gh._parseMainTipSha(payload, "not-a-time", "sunql/QA")


def test_parseMainTipSha_rejects_an_unexpected_payload_shape() -> None:
    """响应不是「页的列表」⇒ 报错（fail-closed），不静默当成「没有 run」。"""
    with pytest.raises(ValueError):
        ci_gh._parseMainTipSha(
            json.dumps({"foo": 1}), "2026-11-06T19:38:13+00:00", "sunql/QA"
        )


def test_parseMainTipSha_reads_every_page() -> None:
    """`--paginate --slurp` 的返回是**页的列表** ⇒ 目标可能在第二页。

    ⚠️ 只读第一页 ⇒ 目标不在页内 ⇒ 判定不出来 ⇒ 白拦。
    """
    payload = json.dumps(
        [
            {"workflow_runs": [_mainRun("2026-11-06T19:30:00Z", headSha="d" * 40)]},
            {"workflow_runs": [_mainRun("2026-11-06T19:37:16Z", headSha="e" * 40)]},
        ]
    )

    sha = ci_gh._parseMainTipSha(payload, "2026-11-06T19:38:13+00:00", "sunql/QA")

    assert sha == "e" * 40


def test_fetchMainTipShaAt_queries_main_push_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """取数必须走「main 的 push run 列表」⇒ 且**翻遍所有页**。"""
    captured: dict[str, list[str]] = {}
    payload = _runsPayload([_mainRun("2026-11-06T19:37:16Z", headSha="e" * 40)])

    def fakeRunGh(args: list[str]) -> str:
        captured["args"] = args
        return payload

    monkeypatch.setattr(ci_gh, "_runGh", fakeRunGh)

    sha = ci_gh.fetchMainTipShaAt("sunql/QA", "2026-11-06T19:38:13+00:00")

    assert sha == "e" * 40
    assert (
        "repos/sunql/QA/actions/runs?branch=main&event=push&per_page=100" in captured["args"]
    )
    assert "--paginate" in captured["args"]
    assert "--slurp" in captured["args"]


def test_fetchMainTipShaAt_rejects_a_malformed_repo() -> None:
    """仓库名会被拼进 API 路径 ⇒ 自己校验。"""
    with pytest.raises(ValueError):
        ci_gh.fetchMainTipShaAt(
            "../../other-owner/other-repo", "2026-11-06T19:38:13+00:00"
        )
