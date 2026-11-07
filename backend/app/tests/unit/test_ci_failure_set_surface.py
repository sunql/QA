"""闸门对「**CI 相关面判定**」的退出码（第三版判定集合 = `A ∪ P`）。

为什么单独一个文件：`test_ci_failure_set.py` 覆盖「失败集合对差」的**全部**退出码，
`test_ci_failure_set_flakes.py` 覆盖**白名单**这条线；本文件只钉「CI 相关面判定」
这一条线（2026-11-07 N6 第三版新增的用例）⇒ 三个文件都不超 800 行上限。

覆盖的行为：

- 判定集合里**归因不到本 PR** 的 CI 文件（main 改了、PR 没跟上）⇒ 照样拦（退出码 6）；
- 基线 run **不是本仓库 main 上的 push run** ⇒ 对差前提不成立 ⇒ 退出码 3；
- 抖动豁免的前置条件用**判定集合** ⇒ main 侧改过的测试文件**不豁免**。

共享替身见 `app/tests/unit/_ci_gate_support.py`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tests.unit._ci_gate_support import (
    FLAKY_FILE_IN_REPO,
    FLAKY_NODE_ID,
    STABLE_NODE_ID,
    stubCiSurface,
    stubShards,
    writeWhitelistFile,
)
from app.tests.unit._ci_gate_support import (
    oneRedShard as _oneRedShard,
)
from app.tests.unit._ci_gate_support import (
    stubGithub as _stubGithub,
)
from app.utils import ci_failure_set


def test_main_returns_six_when_ci_surface_differs_but_is_not_attributable_to_the_pr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CI 相关面不同、但**归因不到本 PR** ⇒ 照样拦（退出码 6）。

    这一类是「main 在 PR 分叉之后改了 CI 文件、PR 分支没跟上」⇒ 那两次 run
    跑的 CI 定义**本来就不同** ⇒ 失败集合不可比。
    ⇒ 归因集里没有它**不等于**没问题 ⇒ 不能放过去。

    ⚠️ 措辞必须如实说「归因不到本 PR」（而不是谎称「PR 改了」）⇒
    操作者才知道该去查 main 还是查 PR。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    stubCiSurface(
        monkeypatch,
        changedFiles=(),
        netChangedFiles=(".github/workflows/ci.yml",),
        unattributedCiFiles=(".github/workflows/ci.yml",),
    )

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])
    report = capsys.readouterr().out

    assert exitCode == 6
    assert "归因不到本 PR" in report


def test_main_returns_three_when_the_base_run_is_untrusted(monkeypatch: pytest.MonkeyPatch) -> None:
    """基线 run **不是 main 上的 push run** ⇒ 对差的前提不成立 ⇒ 退出码 3。

    2026-11-07 安全评审 HIGH-1：GitHub 的 `runs?branch=main` **不区分事件**
    ⇒ 一个**名叫 main 的 fork 分支**发起的 PR 也会命中 ⇒ 它的 run 可能被
    当成「main 的基线」⇒ 基线 = 攻击者自己的代码 ⇒ 失败集合对差被中和。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    stubCiSurface(
        monkeypatch,
        (),
        baseTrustError="基线 run 的触发事件是 'pull_request' 而不是 'push' ⇒ 失败集合不可比",
    )

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 3


def test_main_does_not_exempt_a_test_whose_file_changed_on_either_side(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """豁免的前置条件必须用**判定集合** ⇒ **main 侧改的**也算「改过了」。

    ⚠️ 2026-11-07 安全评审 MEDIUM-3：前置条件若只用「PR 相对分叉点改了什么」
    ⇒ main 在分叉点之后改了那个测试文件、PR 分支没跟上 ⇒ 两次 run 跑的
    **代码内容其实不同** ⇒ 那个用例的红绿可能已经不是抖动
    ⇒ 却照样被豁免（对称豁免 ⇒ 两侧都剔除）⇒ 真回归被吃掉。

    ⇒ 这里：归因集为空（PR 没改），净变更里有 ⇒ 必须**不豁免** ⇒ 退出码 5。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))
    stubCiSurface(
        monkeypatch,
        changedFiles=(),
        netChangedFiles=(FLAKY_FILE_IN_REPO,),
    )

    exitCode = ci_failure_set.main(
        [
            "--repo",
            "sunql/QA",
            "--base-run",
            "111",
            "--pr-run",
            "222",
            "--flake-whitelist",
            str(whitelistPath),
        ]
    )

    # 豁免不生效 ⇒ 基线侧多出那条抖动失败 ⇒ 净丢失 ⇒ 退出码 5（须人工确认）
    assert exitCode == 5
