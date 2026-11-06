"""merge-gate 的**已知抖动白名单**（B7）CLI 层用例。

为什么单独一个文件：`test_ci_failure_set.py` 覆盖「失败集合对差」的**全部**退出码，
而白名单这条线又长（加载失败关闭 / 豁免 / 跳过 / gate 标题 / 豁免未生效 …）
⇒ 塞在一起会让两个文件都超 800 行上限 ⇒ 按职责拆开（共享替身见
`app/tests/unit/_ci_gate_support.py`）。

覆盖的行为：

- 白名单读不到 / 非法 ⇒ **失败关闭**（退出码 3），绝不退化成「空白名单」；
- 白名单命中的用例**不计入判据**（对称豁免）；
- 豁免**条件化在「该用例所在文件本次未被改动」**；
- 豁免的**可见性**：gate 标题 + 报告正文 + 「豁免未生效」提示。
"""

from __future__ import annotations

import pytest

from app.tests.unit._ci_gate_support import (
    FLAKY_FILE_IN_REPO,
    FLAKY_NODE_ID,
    STABLE_NODE_ID,
    redLogWith,
    stubCiSurface,
    stubShards,
    writeWhitelistFile,
)
from app.tests.unit._ci_gate_support import (
    greenLog as _greenLog,
)
from app.tests.unit._ci_gate_support import (
    job as _job,
)
from app.tests.unit._ci_gate_support import (
    oneRedShard as _oneRedShard,
)
from app.tests.unit._ci_gate_support import (
    stubGithub as _stubGithub,
)
from app.utils import ci_failure_set
from app.utils.ci_failure_set import collectRunFailures

# ---------------------------------------------------------------------------
# 已知抖动白名单（B7）：净丢失 / 净新增里若命中白名单 ⇒ 不计入判据
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_only_whitelisted_failure_disappeared(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """基线红 / PR 绿，且红的那条**在白名单里** ⇒ 退出码 0（不再拦下 PR）。

    这正是 PR #107 撞上的场景：唯一的差异是一条**已知抖动**的用例
    ⇒ 它的结论不是证据 ⇒ 不该挡人。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))

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

    assert exitCode == 0


def test_main_returns_five_when_removed_failure_is_not_whitelisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """净丢失的那条**不在白名单里** ⇒ 仍然退出码 5（人工确认不能被绕过）。"""
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, ("app/tests/unit/test_other.py::test_other",))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))

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

    assert exitCode == 5


def test_main_returns_zero_when_pr_side_whitelisted_failure_appears(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """基线绿 / PR 红，且红的那条在白名单里 ⇒ 退出码 0（**对称**语义）。"""
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (STABLE_NODE_ID,), (STABLE_NODE_ID, FLAKY_NODE_ID))

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

    assert exitCode == 0


def test_main_returns_three_when_whitelist_file_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """白名单文件**读不到** ⇒ 退出码 3（失败关闭，不是「当成空白名单」）。"""
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))

    exitCode = ci_failure_set.main(
        [
            "--repo",
            "sunql/QA",
            "--base-run",
            "111",
            "--pr-run",
            "222",
            "--flake-whitelist",
            str(tmp_path / "not-there.json"),
        ]
    )

    assert exitCode == 3


def test_main_returns_three_when_whitelist_is_broken(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """白名单**内容非法** ⇒ 退出码 3（失败关闭）。"""
    whitelistPath = tmp_path / "whitelist.json"
    whitelistPath.write_text("{ not json", encoding="utf-8")
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))

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

    assert exitCode == 3


def test_main_surfaces_flake_exemption_in_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    """豁免**必须出现在报告里**（不是静默放行）。"""
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))

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
    report = capsys.readouterr().out

    assert exitCode == 0
    assert "已知抖动豁免" in report
    assert FLAKY_NODE_ID in report
    assert "基线侧" in report


# ---------------------------------------------------------------------------
# 白名单**只影响对差**：不能用来掩盖「job 没跑完」「少跑了分片」「改了 conftest」
# ---------------------------------------------------------------------------


def test_main_returns_two_when_a_job_is_unusable_even_with_whitelist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """**有不可用 job** ⇒ 退出码 2 —— 哪怕差异那条已被白名单豁免。

    ⚠️ 这是「白名单不是万能钥匙」的守卫：白名单只让某个用例**不再计入对差**，
    它不能把「这个分片根本没跑完」变成「可以合并」——
    不可用 job 的失败集合是**空的** ⇒ 对差出来的「净新增 0」是假的。

    ⇒ fixture 两侧都有同一个 cancelled 的 shard 1 ⇒ **覆盖等价**成立
    ⇒ 只有「不可用 job」这条规则能拦下（不会与覆盖不等价混淆）。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))

    baseJobs = [
        _job(jobId=1, name="Backend / unit shard 0", conclusion="failure"),
        _job(jobId=2, name="Backend / unit shard 1", conclusion="cancelled"),
    ]
    baseResult = collectRunFailures(baseJobs, {1: redLogWith((FLAKY_NODE_ID, STABLE_NODE_ID)), 2: ""})
    prJobs = [
        _job(jobId=11, name="Backend / unit shard 0", conclusion="failure"),
        _job(jobId=12, name="Backend / unit shard 1", conclusion="cancelled"),
    ]
    prResult = collectRunFailures(prJobs, {11: redLogWith((STABLE_NODE_ID,)), 12: ""})
    _stubGithub(monkeypatch, baseResult, prResult)

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

    assert exitCode == 2


def test_main_returns_four_when_job_coverage_differs_even_with_whitelist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """**覆盖不等价**（PR 少跑一个分片）⇒ 退出码 4 —— 白名单救不了。

    攻击路径：PR 改 `ci.yml` 让某个分片不跑 ⇒ 命中失败用例的分片根本不跑
    ⇒ PR 失败集合变小 ⇒ 「净新增 0」是假的，而 GitHub 上 CI 还全绿。

    ⇒ fixture 里两侧的 job 都**可用** ⇒ 只有「覆盖等价」这条规则能拦下。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))

    baseJobs = [
        _job(jobId=1, name="Backend / unit shard 0", conclusion="failure"),
        _job(jobId=2, name="Backend / unit shard 1", conclusion="success"),
    ]
    baseResult = collectRunFailures(
        baseJobs,
        {1: redLogWith((FLAKY_NODE_ID, STABLE_NODE_ID), shard=0),
         2: _greenLog("UNIT", 1)},
    )
    prJobs = [_job(jobId=11, name="Backend / unit shard 0", conclusion="failure")]
    prResult = collectRunFailures(prJobs, {11: redLogWith((STABLE_NODE_ID,), shard=0)})
    _stubGithub(monkeypatch, baseResult, prResult)

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

    assert exitCode == 4


def test_main_returns_six_when_ci_surface_changed_even_with_whitelist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """**PR 改了 CI 相关面** ⇒ 退出码 6 —— 白名单救不了。

    攻击路径：PR 改 `conftest.py` / workflow / 分片清单 ⇒ 失败用例根本不产生
    FAILED 行 ⇒ 失败集合与基线完全相同 ⇒ 假「净新增 0」⇒ 而 CI 还全绿。

    ⇒ 本例用 `.github/CODEOWNERS`（改授权面 = 换复核人）作为触发的 CI 相关面。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))
    stubCiSurface(monkeypatch, (".github/CODEOWNERS",))

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

    assert exitCode == 6


def test_main_notes_unmatched_whitelist_entries(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    """白名单里有条目**这次没命中** ⇒ 报告里必须提示（否则拼错的 ID 静默失效）。

    没有这条提示的话：node ID 写错一个字 ⇒ 白名单看起来在管事，实际那条抖动
    照样随机挡人，而报告里看不出「有条目一直没生效」。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, ("app/tests/unit/test_typo.py::test_typo",))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))

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
    report = capsys.readouterr().out

    assert exitCode == 5  # 未命中的那条不影响判据；此处差异仍是净丢失
    assert "未命中" in report


def test_main_prints_gate_title_when_exemptions_happened(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    """退出码 0 且发生豁免 ⇒ stdout 里有 `gate-title:` 一行（**仅供人读**）。

    ⚠️ 这一行是 **display-only**：check 标题的**机器通道**是 `--gate-title-file`
    指明的那个**独立文件**（SR-L-2：闸门不得从人读的报告正文里 grep 标题 ⇒
    报告渲染一改就静默失效）。这一行只让**直接看 stdout** 的人也能看到豁免条数。

    ⇒ 所以它**可以**留在 stdout，但**不得**被当成机器通道；
    workflow 侧的守卫 `test_merge_gate_reads_the_gate_title_from_the_tool_file`
    会断言闸门**不 grep** 这一行。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))

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
    report = capsys.readouterr().out

    assert exitCode == 0
    assert "gate-title: " in report
    assert "已知抖动豁免" in report.split("gate-title: ", 1)[1]


def test_main_gate_title_flags_pr_side_hit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    """**PR 侧命中**白名单 ⇒ 标题里必须点明「请人工确认不是确定性回归」。

    这是对称豁免唯一的兜底：结论不拦人，但**必须让人一眼看见**。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (STABLE_NODE_ID,), (STABLE_NODE_ID, FLAKY_NODE_ID))

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
    report = capsys.readouterr().out

    assert exitCode == 0
    titleLine = next(line for line in report.splitlines() if line.startswith("gate-title: "))
    assert "PR 侧命中" in titleLine
    assert "人工确认" in titleLine


def test_main_prints_no_gate_title_without_exemptions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    """没有豁免 ⇒ **不打印** `gate-title:`（标题沿用闸门按退出码生成的默认文案）。"""
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, ())
    stubShards(monkeypatch, (STABLE_NODE_ID,), (STABLE_NODE_ID,))

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
    report = capsys.readouterr().out

    assert exitCode == 0
    assert "gate-title: " not in report


# ---------------------------------------------------------------------------
# 白名单读取失败必须是**任何**异常形态（2026-11-06 安全审查 L-1）
# ---------------------------------------------------------------------------


def test_main_returns_three_when_the_whitelist_cannot_be_read_at_all(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """白名单读取抛出**非** `FlakeWhitelistError` 的异常 ⇒ 仍然退出码 3。

    ⚠️ 2026-11-06 安全审查 L-1：`_loadFlakeEntries` 只捕 `FlakeWhitelistError`
    ⇒ 别的异常（`OSError` / `KeyError` / `MemoryError` …）会**逃出**加载器
    ⇒ CLI 裸 traceback + 退出码 **1**（1 的语义是「净新增失败」⇒ 操作者查错方向）。

    ⇒ 白名单是判据的一部分 ⇒ 读不出来 ⇒ 一律**失败关闭**（退出码 3）。

    ⚠️ fixture 刻意 stub 掉 gh：否则「退出码 3」也可能来自 gh 调用失败
    ⇒ 本用例就变成**恒绿**（断言的失败原因不对）。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)

    def boom(path):
        raise RuntimeError("模拟非白名单异常")

    monkeypatch.setattr(ci_failure_set, "loadFlakeWhitelist", boom)

    exitCode = ci_failure_set.main(
        ["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"]
    )

    assert exitCode == 3


# ---------------------------------------------------------------------------
# gate 标题必须走**独立文件**（2026-11-06 安全审查 L-2）
# ---------------------------------------------------------------------------


def test_main_writes_the_gate_title_into_the_title_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """`--gate-title-file` ⇒ 标题写进**独立文件**（不靠从人读报告里 grep）。

    ⚠️ 2026-11-06 安全审查 L-2：旧实现让闸门 `grep '^gate-title: '` 报告正文
    ⇒ 报告是**给人读**的文本 ⇒ 改一下渲染（加个缩进、换个前缀、把标题挪到
    被截断的尾部）就静默失效 ⇒ **豁免提示从标题里消失** ⇒ 对称豁免的代价
    重新被「绿色对勾」盖住。

    ⇒ 标题走**专用文件** ⇒ 人读报告怎么改都不影响它。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))
    titlePath = tmp_path / "gate_title.txt"

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
            "--gate-title-file",
            str(titlePath),
        ]
    )

    assert exitCode == 0
    assert titlePath.is_file()
    title = titlePath.read_text(encoding="utf-8")
    assert "已知抖动豁免" in title
    assert "净新增 0" in title


def test_main_writes_no_title_file_without_exemptions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """没有豁免 ⇒ **不写**标题文件 ⇒ 闸门沿用按退出码生成的默认标题。"""
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, ())
    stubShards(monkeypatch, (STABLE_NODE_ID,), (STABLE_NODE_ID,))
    titlePath = tmp_path / "gate_title.txt"

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
            "--gate-title-file",
            str(titlePath),
        ]
    )

    assert exitCode == 0
    assert not titlePath.exists()


def test_main_returns_three_when_the_title_file_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """标题文件写不出来 ⇒ 退出码 3（失败关闭）。

    标题是「豁免可见性」的**唯一**载体 ⇒ 写不出来 = 豁免会被「绿色对勾」盖住
    ⇒ 与其静默放行，不如保守拦下（与「报告渲染失败 ⇒ 3」同一纪律）。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))
    titlePath = tmp_path / "no_such_dir" / "gate_title.txt"

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
            "--gate-title-file",
            str(titlePath),
        ]
    )

    assert exitCode == 3


# ---------------------------------------------------------------------------
# 豁免必须条件化在「该用例所在文件本次未被改动」（2026-11-06 安全审查 H-2）
# ---------------------------------------------------------------------------


def test_main_does_not_exempt_a_test_whose_file_the_pr_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """PR 改了白名单用例**所在文件** ⇒ **不豁免** ⇒ 该用例照常计入判据。

    对称豁免的代价是「PR 侧确定性弄红白名单用例也不拦」⇒ 若 PR 直接改了那个
    文件，它的红就**可能不是抖动**了 ⇒ 豁免必须条件化在「该文件未被改动」上。

    ⚠️ 这条**不引入随机阻塞**：判据只依赖「改了哪些文件」（服务端事实），
    不依赖用例这一次红没红 ⇒ 结论稳定可复现。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))
    stubCiSurface(monkeypatch, (FLAKY_FILE_IN_REPO,))

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


def test_main_notes_an_exemption_that_did_not_apply(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    """豁免因「所在文件被改动」而未生效 ⇒ 报告里必须**明示**（不静默）。"""
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))
    stubCiSurface(monkeypatch, (FLAKY_FILE_IN_REPO,))

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
    report = capsys.readouterr().out

    assert exitCode == 5
    assert "豁免未生效" in report
    assert FLAKY_NODE_ID in report


def test_main_does_not_advise_cleaning_up_a_skipped_whitelist_entry(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    """因「所在文件被改动」而未生效的条目 ⇒ **不得**被算成「未命中 ⇒ 建议清理」。

    ⚠️ 2026-11-06 代码评审 M-6：旧实现按 `len(entries) - len(exemptions)`
    算「未命中」⇒ **命中了失败集合、只是没生效豁免**的条目被算成「未命中」
    ⇒ 报告说「本次命中 0 条，未命中 1 条（可能已失效或拼写错误 ⇒ 建议清理）」
    ⇒ 两句话都是**假的** ⇒ 操作者据此删掉一条**仍然活着**的抖动条目
    ⇒ 随机挡人的老毛病**静默复活**（B7 存在的目的被推翻）。

    ⇒ 「命中失败集合」= 生效豁免 + 因文件被改动而未生效。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))
    stubCiSurface(monkeypatch, (FLAKY_FILE_IN_REPO,))

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
    report = capsys.readouterr().out

    assert exitCode == 5
    # 该条目**命中了**失败集合 ⇒ 不得出现在「未命中」计数里，更不得建议清理。
    assert "建议清理" not in report
    assert "未命中" not in report


def test_main_counts_a_skipped_entry_as_matched_in_the_unmatched_note(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    """「命中失败集合」的计数必须把**因文件改动而未生效**的条目算进去。

    ⇒ 白名单里 2 条：一条命中了失败集合（但未生效豁免）、一条是拼错的
    ⇒ 报告必须说「命中失败集合 1 条 / 未命中 1 条」
    ⇒ 若把未生效的那条算成未命中 ⇒ 计数变成「命中 0 条 / 未命中 2 条」
    ⇒ 「未命中」被灌水 ⇒ 「建议清理」指向两条，其中一条是**活的**。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID, "app/tests/unit/test_typo.py::test_typo"))
    stubShards(monkeypatch, (FLAKY_NODE_ID, STABLE_NODE_ID), (STABLE_NODE_ID,))
    stubCiSurface(monkeypatch, (FLAKY_FILE_IN_REPO,))

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
    report = capsys.readouterr().out

    assert exitCode == 5
    assert "命中失败集合 1 条" in report
    assert "未命中 1 条" in report


def test_main_gate_title_does_not_flag_a_both_sides_hit_as_a_pr_side_hit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    """**两侧都命中**（基线红、PR 也红）不得报「PR 侧命中」。

    ⚠️ 2026-11-06 代码评审 L-1：两侧都命中是**最典型**的抖动形态
    （抖动就是「同代码不同结论」，两次 run 都红完全正常）
    ⇒ 却也被算成「PR 侧命中」⇒ 每次抖动都报「请人工确认不是确定性回归」
    ⇒ 告警变噪音 ⇒ 真告警被淹没。

    ⇒ 真正危险的只有「基线不红、PR 红了」——那才可能是被掩盖的确定性回归。
    """
    whitelistPath = tmp_path / "whitelist.json"
    writeWhitelistFile(whitelistPath, (FLAKY_NODE_ID,))
    stubShards(monkeypatch, (STABLE_NODE_ID, FLAKY_NODE_ID), (STABLE_NODE_ID, FLAKY_NODE_ID))

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
    report = capsys.readouterr().out

    assert exitCode == 0
    titleLine = next(line for line in report.splitlines() if line.startswith("gate-title: "))
    assert "PR 侧命中" not in titleLine, titleLine
    assert "人工确认" not in titleLine, titleLine
