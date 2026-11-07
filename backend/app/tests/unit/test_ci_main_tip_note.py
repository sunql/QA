"""「基线 run ≠ PR run 的 main 侧底座」必须在报告里说（2026-11-07 N6）。

背景：`ci.yml` 对 `pull_request` 触发且 `actions/checkout` 不带 `with: ref:`
⇒ PR 的 CI run 检出的是 `refs/pull/N/merge` ⇒ 它跑的 commit 的**第一父**
就是那次合并的 **main 侧底座**（本批记作 `M_T`）。

⇒ 基线 run 若是**别的** main commit ⇒ main 在基线之后推进过
⇒ 两次 run 的 CI 定义可能不同 ⇒ 失败集合原则上不可比
⇒ 判出「CI 相关面变更」时的正确处置是**重新运行 PR 的 CI**（让它合上最新的
main），而不是直接放行。

⇒ 这条提示必须出现在报告里：人要先看到它，才知道该做什么
（否则只会看到一句无处置办法的「CI 相关面变了 ⇒ 拦」）。

⚠️ 2026-11-07 N6 第三版：判定集合 = `A ∪ P`（main 的推进 ∪ PR 的改动），
`M_T` 是「PR run 开跑那一刻 main 的 tip」⇒ 与基线 head sha 不同**是常态**
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
    ⇒ 断言里同时钉住「提示」与「重新运行 PR 的 CI」两个片段。
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
    assert "重新运行 PR 的 CI" in report


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
