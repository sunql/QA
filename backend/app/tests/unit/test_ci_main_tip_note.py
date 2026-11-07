"""「基线 run ≠ PR run 的 main 侧底座」必须在报告里说（2026-11-07 N6）。

背景：`ci.yml` 对 `pull_request` 触发且 `actions/checkout` 不带 `with: ref:`
⇒ PR 的 CI run 检出的是 `refs/pull/N/merge` ⇒ 它跑的 commit 的**第一父**
就是那次合并的 **main 侧底座**（本批记作 `M_T`）。

⇒ 基线 run 若是**别的** main commit ⇒ main 在基线之后推进过
⇒ 两次 run 的 CI 定义可能不同 ⇒ 失败集合原则上不可比
⇒ 判出「CI 相关面变更」时的正确处置是**推一个新 commit 到 PR 分支**
（或 rebase 到最新 main / 重开 PR）⇒ 产生一次**新的 CI 事件派发**
⇒ 新的合并 commit ⇒ 新的 main 侧底座。

⚠️⚠️ **不能叫操作者「重新运行 PR 的 CI」**（2026-11-07 代码评审 HIGH-1）：
`pull_request` 事件的**合并 commit 在事件派发那一刻就被冻结**了
⇒ 复跑（re-run）**不会**重新计算它 ⇒ 复跑检出的仍是**同一个**合并 commit
⇒ 判定依旧不成立 ⇒ 照着这句话做会**白跑一轮**还拿不到新结论。
实测（PR #105 run `41908498247`，attempt=2）：复跑仍执行 `57a267a93f…`
（= attempt-1 的那个合并 commit），其第一父仍是派发那一刻的 main。

⇒ 这条提示必须出现在报告里：人要先看到它，才知道该做什么
（否则只会看到一句无处置办法的「CI 相关面变了 ⇒ 拦」）。

⚠️ 2026-11-07 N6 第三版：判定集合 = `A ∪ P`（main 的推进 ∪ PR 的改动），
`M_T` 是「PR run **事件派发**那一刻 main 的 tip」⇒ 与基线 head sha 不同**是常态**
（main 一直在推进）⇒ 这条提示必须**只在与判据相关时**出现，否则就是噪音。

本文件只钉这条提示；CI 相关面判定本身见 `test_ci_surface.py`。
"""

from __future__ import annotations

import pytest

from app.tests.unit._ci_gate_support import oneRedShard as _oneRedShard
from app.tests.unit._ci_gate_support import stubCiSurface as _stubCiSurface
from app.tests.unit._ci_gate_support import stubGithub as _stubGithub
from app.utils import ci_failure_set


def test_main_reports_when_the_baseline_is_not_the_pr_runs_main_side(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """基线 commit ≠ main 侧底座 ⇒ 报告里必须出现提示**和处置办法**。

    ⚠️ 只说「拦」不说「怎么办」= 把判断成本转嫁给操作者
    ⇒ 断言里同时钉住「提示」与处置办法两个片段。

    ⚠️⚠️ 处置办法**不能**是「重新运行 PR 的 CI」（2026-11-07 代码评审 HIGH-1）：
    `pull_request` 的合并 commit 在派发那一刻就冻结 ⇒ 复跑仍执行同一棵树
    ⇒ 判定依旧不成立 ⇒ 照做会白跑一轮。
    ⇒ 正确处置 = **推一个新 commit**（产生新的事件派发 ⇒ 新的合并 commit）。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    _stubCiSurface(
        monkeypatch,
        changedFiles=(".github/workflows/ci.yml",),
        mainTipSha="z" * 40,
    )

    exitCode = ci_failure_set.main(
        ["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"]
    )
    report = capsys.readouterr().out

    assert exitCode == 6
    assert "不是 PR run 的 main 侧底座" in report
    assert "推一个新 commit 到 PR 分支" in report


def test_main_does_not_advise_a_pointless_rerun(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """处置办法**不得**是「重新运行 PR 的 CI」—— 那是**无用**的（代码评审 HIGH-1）。

    ⚠️ `pull_request` 事件的合并 commit 在**事件派发那一刻**就被冻结
    ⇒ 复跑（re-run）不会重新计算它 ⇒ 复跑检出的仍是同一个合并 commit
    ⇒ 它的 main 侧底座仍是派发那一刻的 main ⇒ 判定依旧不成立。
    实测（PR #105 run `41908498247`，attempt=2）：复跑仍执行 `57a267a93f…`。
    ⇒ 照着「重新运行 PR 的 CI」做 ⇒ **白跑一轮**还拿不到新结论。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    _stubCiSurface(
        monkeypatch,
        changedFiles=(".github/workflows/ci.yml",),
        mainTipSha="z" * 40,
    )

    ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])
    report = capsys.readouterr().out

    assert "重新运行 PR 的 CI" not in report
    assert "单纯复跑 run 无效" in report


def test_main_does_not_report_a_mismatch_when_the_base_is_the_same_commit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """基线 commit == main 侧底座 ⇒ **不**出现该提示（否则就成了噪音）。

    反向守卫：提示一旦变成「每次都出现」，操作者就会无视它 ⇒ 提示就失效了。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    # `mainTipSha` 的默认值 = 基线 head sha ⇒ 同底座。
    _stubCiSurface(monkeypatch, changedFiles=(".github/workflows/ci.yml",))

    exitCode = ci_failure_set.main(
        ["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"]
    )
    report = capsys.readouterr().out

    assert exitCode == 6
    assert "不是 PR run 的 main 侧底座" not in report


def test_main_does_not_report_a_mismatch_when_no_ci_surface_changed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """底座不同但**没判出 CI 相关面变更** ⇒ 同样**不**出现提示（第三轮评审 M-1）。

    `M_base ≠ M_T` 是**常态**（main 一直在推进）——规则文档写的是
    「提示只在判出 CI 相关面变更时出现」⇒ 没判出时出现就是噪音
    ⇒ 提示变噪音就会被无视 ⇒ 提示失效（恰好毁掉处置办法的可信度）。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    _stubCiSurface(
        monkeypatch,
        changedFiles=("docs/readme.md",),
        netChangedFiles=(),  # 判定集合不含任何 CI 文件 ⇒ 不判 CI 相关面变更
        mainTipSha="z" * 40,  # 底座不同（常态）
    )

    exitCode = ci_failure_set.main(
        ["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"]
    )
    report = capsys.readouterr().out

    assert exitCode == 0
    assert "不是 PR run 的 main 侧底座" not in report
    assert "推一个新 commit 到 PR 分支" not in report


def test_main_reports_the_main_side_base_in_the_header(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """报告**头部**必须印出 `M_T` ⇒ 人能自己核对「PR 的 CI 跑的到底是什么」。

    ⚠️ 这条不能省：`M_T` 是整套判定的支点（`A` = 基线树 → `M_T` 的推进）
    ⇒ 报告里看不到它 ⇒ 操作者只能相信工具的一句话。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    _stubCiSurface(
        monkeypatch,
        changedFiles=(".github/workflows/ci.yml",),
        mainTipSha="z" * 40,
    )

    ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])
    report = capsys.readouterr().out

    assert "main 侧底座" in report
    assert "z" * 40 in report


def test_main_says_the_ci_change_is_not_attributable_to_this_pr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """「归因不到本 PR」必须**如实说**（判定集合含 main 的推进、PR 没改它）。

    ⚠️ 第三版判定集合 = `A ∪ P` ⇒ 「main 改了 CI 文件、PR 没跟上」会命中
    ⇒ 但措辞不能写成「PR 改了」⇒ 否则操作者会去审 PR 的改动（找不到），
    而真正的变更方在 main 上。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    _stubCiSurface(
        monkeypatch,
        changedFiles=(),
        mainAdvanceFiles=(".github/workflows/ci.yml",),
        netChangedFiles=(".github/workflows/ci.yml",),
        unattributedCiFiles=(".github/workflows/ci.yml",),
    )

    exitCode = ci_failure_set.main(
        ["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"]
    )
    report = capsys.readouterr().out

    assert exitCode == 6
    assert "归因不到本 PR" in report
