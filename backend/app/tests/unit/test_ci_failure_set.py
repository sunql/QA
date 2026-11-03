"""CI 失败集合对差：判据必须**拒绝**「不可用的 job 贡献了一个空失败集合」。

背景：本仓 CI 基线是红的（`main` 上即有既有失败）⇒ 合并判据不是「CI 变绿」，
而是**失败集合对差**：PR run 的失败用例集合 ⊆ 基线 run 的失败用例集合。

人工做这件事已经踩过多个坑（全部实测于 2026-11-02 的 PR #98/#99 批次）：

1. **`cancelled` 的 job 失败集合是空的** ⇒ 直接对差会得出「净新增 0」的**假结论**。
2. **缺完成标记 = 没跑完** ⇒ 失败集合同样不可用。
3. **`status=1` 但没有 pytest 收尾汇总行 = pytest 中途夭折** ⇒ 失败集合只覆盖前半截。
4. **GH Actions 日志行带时间戳前缀** ⇒ 正则不能锚定 `^FAILED`（那样一条都匹配不到）。
5. **`comm` 要求两侧同序同 collation** ⇒ python `sorted()` 与 `sort -u` 不同序时
   会输出**两边都非空**的假差异。

⇒ 本模块把判据做成**可执行**的：任何不可用 job 存在时，结论一律是
「不可用，必须复跑」，**不允许**输出「净新增 0 ⇒ 可合并」。

本文件把这条性质钉死，并保证它不是恒绿（把 `cancelled` 判成可用即转红）。

脱敏 / 净化规则见 `test_ci_text.py`；gh 适配层见 `test_ci_gh.py`；
「CI 相关面」判定见 `test_ci_surface.py`。
"""

from __future__ import annotations

import pytest

from app.utils import ci_failure_set
from app.utils.ci_failure_set import (
    CiJob,
    JobAssessment,
    assessJob,
    collectRunFailures,
    diffFailureSets,
    extractFailureIds,
    parseShardDoneMarkers,
    renderReport,
    sanitizeForReport,
    verdictFor,
)
from app.utils.ci_head import HeadFreshness
from app.utils.ci_surface import CiSurfaceInfo

# GH Actions 的真实日志行形状：行首是 ISO 时间戳 + 空格
_TIMESTAMPED = "2026-11-02T07:10:13.4960640Z "


def _redLog(family: str = "UNIT", shard: int = 0) -> str:
    """一份「分片跑完但红了」的日志片段（真实形状）。

    **完成标记的族名与分片号必须与 job 名对齐**（job 名 `Backend / unit shard 0`
    ⇒ 标记 `UNIT_SHARD_DONE shard=0`）—— 实现会交叉校验两者 ⇒ fixture 不对齐时，
    用例会因为「标记与 job 不符」而变红，测不到本来要测的那件事。

    **必须带 pytest 的收尾汇总行**（`2 failed, … in …s`）—— 真红（status=1）
    而没有汇总行 ⇒ 无法确认 pytest 跑完了整个 session ⇒ 判定为不可用。
    """
    return "\n".join(
        [
            f"{_TIMESTAMPED}platform linux -- Python 3.13.0, pytest-8.3.4",
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_chat_service.py::test_reply - assert 1 == 2",
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_rate_limit.py::test_burst - AssertionError",
            f"{_TIMESTAMPED}2 failed, 118 passed, 3 skipped in 812.34s",
            f"{_TIMESTAMPED}{family}_SHARD_DONE shard={shard} status=1",
        ]
    )


def _greenLog(family: str = "UNIT", shard: int = 0) -> str:
    """一份「分片跑完且全绿」的日志片段。"""
    return "\n".join(
        [
            f"{_TIMESTAMPED}5431 passed, 1 skipped in 812.34s",
            f"{_TIMESTAMPED}{family}_SHARD_DONE shard={shard} status=0",
        ]
    )


_RED_LOG = _redLog()
_GREEN_LOG = _greenLog()


def _redLogWithExtra(
    family: str = "UNIT",
    shard: int = 0,
    extraNodeId: str = "app/tests/unit/test_new.py::test_x",
) -> str:
    """一份「比基线多一个失败用例」的日志。

    **汇总行的失败数必须同步 +1** —— 真实 run 里 pytest 自己算这个数
    （`3 failed, …`）⇒ 只加 FAILED 行而不改汇总行，会被「汇总行失败数 vs
    解析出的失败 ID 数」那条交叉核对判为不一致 ⇒ fixture 失真。
    """
    return "\n".join(
        [
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_chat_service.py::test_reply - assert 1 == 2",
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_rate_limit.py::test_burst - AssertionError",
            f"{_TIMESTAMPED}FAILED {extraNodeId} - assert 0 == 1",
            f"{_TIMESTAMPED}3 failed, 118 passed, 3 skipped in 812.34s",
            f"{_TIMESTAMPED}{family}_SHARD_DONE shard={shard} status=1",
        ]
    )


def _job(jobId: int = 1, name: str = "Backend / unit shard 0", conclusion: str = "failure") -> CiJob:
    return CiJob(jobId=jobId, name=name, conclusion=conclusion)


# --------------------------------------------------------------------------
# 失败用例抽取
# --------------------------------------------------------------------------


def test_extractFailureIds_reads_timestamped_log_lines() -> None:
    """GH Actions 日志每行带时间戳前缀 ⇒ 正则不能锚定行首。

    这条钉的是「行首有 `2026-11-02T07:10:13.4960640Z ` 前缀时照样抽得出 nodeid」。
    若实现改成 `^FAILED`，本用例立刻转红。
    """
    assert extractFailureIds(_RED_LOG) == frozenset(
        {
            "app/tests/unit/test_chat_service.py::test_reply",
            "app/tests/unit/test_rate_limit.py::test_burst",
        }
    )


def test_extractFailureIds_ignores_lines_that_merely_mention_failed() -> None:
    """`assert ... failed` 这类散文不能被当成失败用例。"""
    log = "\n".join(
        [
            f"{_TIMESTAMPED}the request failed with status 500",
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y",
        ]
    )

    assert extractFailureIds(log) == frozenset({"app/tests/unit/test_x.py::test_y"})


def test_extractFailureIds_includes_collection_errors() -> None:
    """收集期错误（`ERROR app/tests/x.py`）也是失败，必须进集合。

    否则「pytest 挂了但失败集合为空」⇒ 对差得出假的「净新增 0」。
    """
    log = "\n".join(
        [
            f"{_TIMESTAMPED}ERROR app/tests/unit/test_broken.py",
            f"{_TIMESTAMPED}ERROR app/tests/unit/test_broken.py - ImportError: no module x",
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_ok.py::test_a",
        ]
    )

    assert extractFailureIds(log) == frozenset(
        {
            "app/tests/unit/test_broken.py",
            "app/tests/unit/test_ok.py::test_a",
        }
    )


def test_extractFailureIds_ignores_uppercase_prose_failed() -> None:
    """散文里的**大写** `FAILED` 不能被当成失败用例。

    宽正则 `FAILED\\s+(\\S+)` 会把 `FAILED to reach the Milvus host` 里的 `to`
    当成失败用例 ID ⇒ 报出无意义的「净新增失败：to」⇒ 误判回归、白白卡住合并。
    """
    log = "\n".join(
        [
            f"{_TIMESTAMPED}FAILED to reach the Milvus host, retrying",
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y",
        ]
    )

    assert extractFailureIds(log) == frozenset({"app/tests/unit/test_x.py::test_y"})


def test_extractFailureIds_keeps_full_nodeid_for_error_lines() -> None:
    """`ERROR <nodeid>` 必须保留**完整 nodeid**，不能截断成文件路径。

    截断会让同一文件里的不同用例塌成同一个集合元素 ⇒ 对差精度丢失。
    """
    log = f"{_TIMESTAMPED}ERROR app/tests/unit/test_broken.py::test_x - fixture error"

    assert extractFailureIds(log) == frozenset({"app/tests/unit/test_broken.py::test_x"})


# --------------------------------------------------------------------------
# 完成标记
# --------------------------------------------------------------------------


def test_parseShardDoneMarkers_reads_marker_with_status() -> None:
    """标记形如 `X_SHARD_DONE shard=N status=<退出码>`。"""
    markers = parseShardDoneMarkers(_RED_LOG)

    assert len(markers) == 1
    assert markers[0].name == "UNIT_SHARD_DONE"
    assert markers[0].shard == 0
    assert markers[0].status == 1


def test_parseShardDoneMarkers_returns_empty_when_missing() -> None:
    """缺标记 ⇒ job 是在跑完前死的 ⇒ 失败集合不可用。"""
    log = f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y"

    assert parseShardDoneMarkers(log) == []


def test_parseShardDoneMarkers_accepts_integration_marker() -> None:
    """integration 分片用的是 `INTEGRATION_SHARD_DONE`，同一套解析。"""
    log = f"{_TIMESTAMPED}INTEGRATION_SHARD_DONE shard=2 status=1"

    markers = parseShardDoneMarkers(log)

    assert len(markers) == 1
    assert markers[0].name == "INTEGRATION_SHARD_DONE"
    assert markers[0].shard == 2


# --------------------------------------------------------------------------
# 单个 job 的可用性判定
# --------------------------------------------------------------------------


def test_assessJob_accepts_red_job_with_marker() -> None:
    """有标记 + status!=0 ⇒ 真红 ⇒ 失败集合**可用**。"""
    assessment = assessJob(_job(), _RED_LOG)

    assert assessment.isUsable is True
    assert assessment.unusableReason is None
    assert assessment.failureIds == (
        "app/tests/unit/test_chat_service.py::test_reply",
        "app/tests/unit/test_rate_limit.py::test_burst",
    )


def test_assessJob_accepts_green_job_with_marker() -> None:
    """有标记 + status=0 ⇒ 真绿 ⇒ 失败集合可用且为空。"""
    assessment = assessJob(_job(conclusion="success"), _GREEN_LOG)

    assert assessment.isUsable is True
    assert assessment.failureIds == ()


def test_assessJob_treats_cancelled_as_unusable() -> None:
    """`cancelled`（超时/被抢占）的失败集合是**空的** ⇒ 必须判为不可用。

    这是整套判据的核心：若把它判成可用，对差就会输出「净新增 0 ⇒ 可合并」。
    """
    assessment = assessJob(_job(conclusion="cancelled"), _RED_LOG)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None
    assert "cancelled" in assessment.unusableReason


def test_assessJob_treats_skipped_as_unusable() -> None:
    """`skipped` 的 job 从没跑过 ⇒ 失败集合同样是空的 ⇒ 不可用。"""
    assessment = assessJob(_job(conclusion="skipped"), "")

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None


def test_assessJob_treats_missing_marker_as_unusable() -> None:
    """缺完成标记 ⇒ 没跑完 ⇒ 失败集合不可用（哪怕日志里明明有 FAILED 行）。"""
    log = f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y"

    assessment = assessJob(_job(), log)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None
    assert "标记" in assessment.unusableReason


def test_assessJob_rejects_contradictory_marker_and_conclusion() -> None:
    """结论与标记互相矛盾（success 但 status=1）⇒ 不可信 ⇒ 不可用。"""
    assessment = assessJob(_job(conclusion="success"), _RED_LOG)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None


def test_assessJob_rejects_red_run_without_pytest_summary() -> None:
    """**status=1 但日志里没有 pytest 收尾汇总行 ⇒ 不可信 ⇒ 不可用。**

    pytest 只有在**整个 session 跑完**时才会打出收尾汇总行。一个用例里
    `os._exit(1)` 就能让 pytest **中途夭折**却留下退出码 1 ⇒ CI 脚本照样打出
    `status=1` ⇒ 但失败集合只是**前半截** ⇒ 净新增 0 是假的。

    这条是 2026-11-02 review 实测出来的假绿通道（上一版只认退出码）。
    """
    log = "\n".join(
        [
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y - assert 1 == 2",
            f"{_TIMESTAMPED}UNIT_SHARD_DONE shard=0 status=1",
        ]
    )

    assessment = assessJob(_job(), log)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None
    assert "汇总行" in assessment.unusableReason


def test_assessJob_rejects_green_run_without_pytest_summary() -> None:
    """**status=0（真绿）也必须有 pytest 收尾汇总行 ⇒ 否则不可信 ⇒ 不可用。**

    旧实现只在 `status=1` 时要求汇总行 ⇒ 「真绿」这一侧是空的：可一个用例里
    `os._exit(0)` 就让 pytest **中途夭折**却留下退出码 0 ⇒ CI 脚本照样打出
    `status=0` ⇒ 该分片**一条用例都没跑完**却贡献一个空失败集合 ⇒
    对差得出假的「净新增 0」⇒ 而 GitHub 上 CI 还**全绿** ⇒ 没有二次防线。

    这条是 2026-11-03 两路 review（安全审查 C-1 / 代码审查 HIGH-1）共同指出的
    假绿通道：汇总行是「pytest 跑完了整个 session」的**唯一**旁证，
    只在红的那侧要求 ⇒ 绿的那侧完全没有证据。
    """
    log = "\n".join(
        [
            f"{_TIMESTAMPED}platform linux -- Python 3.13.0, pytest-8.3.4",
            f"{_TIMESTAMPED}UNIT_SHARD_DONE shard=0 status=0",
        ]
    )

    assessment = assessJob(_job(conclusion="success"), log)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None
    assert "汇总行" in assessment.unusableReason


def test_assessJob_rejects_summary_failure_count_mismatch() -> None:
    """汇总行说的失败数与日志里解析出的失败用例 ID 数**对不上** ⇒ 不可信。

    汇总行（`2 failed, 118 passed in 812.34s`）里的失败数是 pytest **自己**算的；
    我们解析出的 FAILED 行数是**另算**的。两者必须一致，否则说明
    「日志被截断」或「失败行格式不认识」⇒ 失败集合不完整 ⇒ 对差出的
    「净新增 0」是假的。

    方向一（汇总行说 2 个、日志里只有 1 个）：日志被截断。
    方向二（汇总行说 1 个、日志里有 2 个）：有失败行没被认出来。
    """
    log = "\n".join(
        [
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y - assert 1 == 2",
            f"{_TIMESTAMPED}2 failed, 118 passed in 812.34s",
            f"{_TIMESTAMPED}UNIT_SHARD_DONE shard=0 status=1",
        ]
    )

    assessment = assessJob(_job(), log)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None
    assert "不一致" in assessment.unusableReason


def test_assessJob_rejects_summary_failure_count_overflow() -> None:
    """反向不一致（汇总行说 1 个、日志里有 2 个 FAILED 行）同样要拦下。"""
    log = "\n".join(
        [
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y - assert 1 == 2",
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_z.py::test_w - assert 3 == 4",
            f"{_TIMESTAMPED}1 failed, 118 passed in 812.34s",
            f"{_TIMESTAMPED}UNIT_SHARD_DONE shard=0 status=1",
        ]
    )

    assessment = assessJob(_job(), log)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None
    assert "不一致" in assessment.unusableReason


def test_assessJob_accepts_summary_failure_count_match() -> None:
    """反向守卫：汇总行与失败明细**一致**时不得误拦（否则会白卡合并）。"""
    log = "\n".join(
        [
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y - assert 1 == 2",
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_z.py::test_w - assert 3 == 4",
            f"{_TIMESTAMPED}2 failed, 118 passed in 812.34s",
            f"{_TIMESTAMPED}UNIT_SHARD_DONE shard=0 status=1",
        ]
    )

    assessment = assessJob(_job(), log)

    assert assessment.isUsable is True
    assert assessment.unusableReason is None


def test_assessJob_skips_count_check_when_log_has_errors() -> None:
    """汇总行里带 error 数时**不做**失败数交叉核对（error 不计入 failed）。

    pytest 的「1 failed, 1 error」里 error 是收集期/拆解期错误，它的用例 ID
    会出现在 `ERROR` 行而非 `FAILED` 行 ⇒ 拿 failed 数去比「FAILED+ERROR 的总数」
    必然对不上 ⇒ 会误拦真实 run ⇒ 只在**没有 error** 时才做这项核对。
    """
    log = "\n".join(
        [
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y - assert 1 == 2",
            f"{_TIMESTAMPED}ERROR app/tests/unit/test_broken.py - ImportError",
            f"{_TIMESTAMPED}1 failed, 1 error, 118 passed in 812.34s",
            f"{_TIMESTAMPED}UNIT_SHARD_DONE shard=0 status=1",
        ]
    )

    assessment = assessJob(_job(), log)

    assert assessment.isUsable is True
    assert assessment.unusableReason is None


def test_assessJob_flags_failure_without_detail() -> None:
    """真红但一条 FAILED 行都没有 ⇒ 失败集合是**空的** ⇒ 必须判为不可用。

    「job 是红的」与「失败集合为空」同时成立，只有两种解释：① 日志没抽到失败行
    （收集期错误被截断、格式不认识）；② pytest 根本没跑到判失败那一步。
    两种情况下失败集合都**不可信** ⇒ 拿它对差会得出「净新增 0」的假结论。

    ⇒ 早期版本只把它记成一个 ⚠️ 提醒、仍输出「可合并」，那是同一族假绿。

    ⚠️ **fixture 不得被别的规则遮蔽**（变异取证 M6 实测）：汇总行若写
    `1 failed, 0 passed in 1.00s` ⇒「汇总行失败数 vs 解析出的失败 ID 数」那条
    交叉核对（1 ≠ 0）会**先**把 job 判成不可用 ⇒ 关掉本条规则照样绿 ⇒
    **恒绿守卫**。⇒ 汇总行改成 `1 error in 1.00s`：error 数 > 0 ⇒ 交叉核对按
    规则**跳过**（error 的 ID 出现在 `ERROR` 行而非 `FAILED` 行）⇒ 只剩本条规则
    能把 job 判成不可用 ⇒ 关掉它就转红。
    """
    log = "\n".join(
        [
            f"{_TIMESTAMPED}1 error in 1.00s",
            f"{_TIMESTAMPED}INTEGRATION_SHARD_DONE shard=0 status=1",
        ]
    )

    assessment = assessJob(_job(name="Backend / integration shard 0"), log)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None
    assert assessment.failureIds == ()
    assert assessment.hasFailureWithoutDetail is True


def test_assessJob_treats_signal_killed_status_as_unusable() -> None:
    """**被信号杀死**的 pytest（`status=137` = SIGKILL）⇒ 没跑完 ⇒ 不可用。

    这条是 2026-11-02 review 实测出来的假绿通道：CI 脚本用 `|| status=$?`
    捕获退出码，pytest 被 OOM killer 杀掉时 bash 报 137，**标记照样打印**
    ⇒ 旧实现把它当成「跑完了、真红、失败集合为空」⇒ 结论「可合并」。
    """
    log = "\n".join(
        [
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y - assert 1 == 2",
            f"{_TIMESTAMPED}INTEGRATION_SHARD_DONE shard=0 status=137",
        ]
    )

    assessment = assessJob(_job(name="Backend / integration shard 0"), log)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None
    assert "137" in assessment.unusableReason


def test_assessJob_treats_incomplete_pytest_exit_codes_as_unusable() -> None:
    """pytest 只有退出码 0（跑完全绿）与 1（跑完有失败）才代表「跑完了」。

    2=中断 / 3=内部错误 / 4=用法错误 / 5=未收集到用例 —— 这四种都意味着
    **没跑完**或**等于没跑** ⇒ 失败集合不可信 ⇒ 不可用。
    """
    for status in (2, 3, 4, 5):
        # 日志里**有** FAILED 行 ⇒ 「真红但零失败明细」这条规则不会先触发
        # ⇒ 用例真正检验的是「退出码不在 {0,1} ⇒ 没跑完」这条判据。
        log = "\n".join(
            [
                f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y - assert 1 == 2",
                f"{_TIMESTAMPED}INTEGRATION_SHARD_DONE shard=0 status={status}",
            ]
        )

        assessment = assessJob(_job(name="Backend / integration shard 0"), log)

        assert assessment.isUsable is False, f"status={status} 应当判为不可用"
        assert assessment.unusableReason is not None


# --------------------------------------------------------------------------
# 整轮 run 的汇总与对差
# --------------------------------------------------------------------------


def test_collectRunFailures_unions_failures_of_usable_jobs() -> None:
    """可用 job 的失败集合取并集。"""
    jobs = [
        _job(jobId=1, name="Backend / unit shard 0", conclusion="failure"),
        _job(jobId=2, name="Backend / unit shard 1", conclusion="success"),
    ]
    logs = {
        1: _RED_LOG,
        2: _greenLog("UNIT", 1),
    }

    failures, assessments = collectRunFailures(jobs, logs)

    assert failures == frozenset(
        {
            "app/tests/unit/test_chat_service.py::test_reply",
            "app/tests/unit/test_rate_limit.py::test_burst",
        }
    )
    assert [item.isUsable for item in assessments] == [True, True]


def test_collectRunFailures_excludes_unusable_jobs_from_failure_set() -> None:
    """不可用 job 的失败集合**不计入**并集（它本来也不可信）。"""
    jobs = [
        _job(jobId=1, name="Backend / unit shard 0", conclusion="failure"),
        _job(jobId=2, name="Backend / unit shard 1", conclusion="cancelled"),
    ]
    logs = {1: _RED_LOG, 2: _RED_LOG}

    failures, assessments = collectRunFailures(jobs, logs)

    assert failures == frozenset(
        {
            "app/tests/unit/test_chat_service.py::test_reply",
            "app/tests/unit/test_rate_limit.py::test_burst",
        }
    )
    assert assessments[1].isUsable is False


def test_diffFailureSets_reports_added_and_removed() -> None:
    """对差必须同时给出净新增与净丢失（后者用来发现「失败被偷偷藏起来」）。"""
    base = frozenset({"a::1", "b::2"})
    pr = frozenset({"b::2", "c::3"})

    diff = diffFailureSets(base, pr)

    assert diff.added == ("c::3",)
    assert diff.removed == ("a::1",)
    assert diff.baseCount == 2
    assert diff.prCount == 2


def test_diffFailureSets_is_order_independent() -> None:
    """输入顺序不影响结果（集合语义，不是列表语义）。"""
    base = frozenset({"z::1", "a::1"})
    pr = frozenset({"a::1", "m::1"})

    diff = diffFailureSets(base, pr)

    assert diff.added == ("m::1",)
    assert diff.removed == ("z::1",)


# --------------------------------------------------------------------------
# 结论判定
# --------------------------------------------------------------------------


def test_verdict_is_mergeable_when_no_new_failures() -> None:
    """全部 job 可用且净新增为空 ⇒ 可合并。"""
    jobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    logs = {1: _RED_LOG}
    baseFailures, baseAssessments = collectRunFailures(jobs, logs)
    prFailures, prAssessments = collectRunFailures(jobs, logs)

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is True
    assert verdict.blockingReason is None


def test_verdict_is_blocked_when_new_failures_appear() -> None:
    """净新增非空 ⇒ 不可合并。"""
    jobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    baseFailures, baseAssessments = collectRunFailures(jobs, {1: _RED_LOG})
    prFailures, prAssessments = collectRunFailures(
        jobs, {1: _redLogWithExtra()}
    )

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is False
    assert verdict.blockingReason is not None
    assert "app/tests/unit/test_new.py::test_x" in verdict.blockingReason


def test_verdict_is_blocked_when_any_job_is_unusable() -> None:
    """**核心守卫**：哪怕净新增为空，只要有不可用 job 就**不能**说「可合并」。

    若实现把 `cancelled` 判成可用（或忽略不可用 job 的存在），本用例立刻转红
    —— 这正是 PR #98 首轮「假净新增 0」的成因。
    """
    baseJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    baseFailures, baseAssessments = collectRunFailures(baseJobs, {1: _RED_LOG})

    prJobs = [
        _job(jobId=1, name="Backend / unit shard 0", conclusion="failure"),
        _job(jobId=2, name="Backend / unit shard 1", conclusion="cancelled"),
    ]
    prFailures, prAssessments = collectRunFailures(prJobs, {1: _RED_LOG, 2: _RED_LOG})

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is False
    assert verdict.blockingReason is not None
    assert "cancelled" in verdict.blockingReason


def test_verdict_is_blocked_when_marker_missing() -> None:
    """缺完成标记的 job 同样让结论变成「不可用」。"""
    baseJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    baseFailures, baseAssessments = collectRunFailures(baseJobs, {1: _RED_LOG})

    prJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    prFailures, prAssessments = collectRunFailures(
        prJobs, {1: f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y"}
    )

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is False
    assert verdict.blockingReason is not None
    assert "标记" in verdict.blockingReason


def test_verdict_is_blocked_when_base_run_has_unusable_job() -> None:
    """**基线侧**有不可用 job 时也必须拒绝合并。

    基线不可信 ⇒ 「PR ⊆ 基线」这个判断本身就没有意义。
    """
    baseJobs = [
        _job(jobId=1, name="Backend / unit shard 0", conclusion="failure"),
        _job(jobId=2, name="Backend / unit shard 1", conclusion="cancelled"),
    ]
    baseFailures, baseAssessments = collectRunFailures(baseJobs, {1: _RED_LOG, 2: _RED_LOG})

    prJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    prFailures, prAssessments = collectRunFailures(prJobs, {1: _RED_LOG})

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is False
    assert verdict.blockingReason is not None


def test_verdict_is_blocked_when_failures_were_removed() -> None:
    """**净丢失**（PR 让某个基线失败消失了）⇒ 不能只报一句就放行。

    规则要求「净丢失要人工确认」（确认不是「把失败藏起来了」）。
    ⇒ 工具必须把它单列成一个**需要人工确认**的结论，而不是可合并。

    注意：这条与「净新增」分开 —— 净新增是硬回归，净丢失可能是「顺手修好了」，
    所以结论是「需人工确认」而不是「有回归」。
    """
    baseJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    baseFailures, baseAssessments = collectRunFailures(baseJobs, {1: _RED_LOG})

    prJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="success")]
    prFailures, prAssessments = collectRunFailures(prJobs, {1: _GREEN_LOG})

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is False
    assert verdict.reasonCode == "NEEDS_REVIEW"


# --------------------------------------------------------------------------
# 非分片 job（frontend 等）：没有标记协议
# --------------------------------------------------------------------------


def test_assessJob_accepts_non_shard_job_without_marker() -> None:
    """**非分片 job 没有完成标记协议** ⇒ 不能因为缺标记就判它不可用。

    真实 run 里除分片 job 外还有 `Frontend / typecheck + coverage` —— 它按设计
    永远不会打印 `*_SHARD_DONE`。若对它也要求标记 ⇒ 不可用列表永不为空 ⇒
    **退出码 0（可合并）永远不可达** ⇒ 工具在真实 run 上直接不可用。
    """
    assessment = assessJob(_job(name="Frontend / typecheck + coverage", conclusion="success"), "")

    assert assessment.isUsable is True
    assert assessment.failureIds == ()


def test_assessJob_gives_non_shard_red_job_an_opaque_failure_id() -> None:
    """非分片 job 真红但本工具解析不出它的失败明细 ⇒ 用**稳定的不透明 ID** 顶替。

    否则它会以**空失败集合**参与对差 ⇒ 「PR 把它弄红了」看不见 ⇒ 假绿。
    用 `job:<job 名>` 这种跨 run 稳定的 ID ⇒ 两次 run 可比。
    """
    assessment = assessJob(_job(name="Frontend / typecheck + coverage", conclusion="failure"), "")

    assert assessment.isUsable is True
    assert assessment.failureIds == ("job:Frontend / typecheck + coverage",)


def test_verdict_is_mergeable_when_run_contains_frontend_job() -> None:
    """**含 frontend job 的真实 run 形态**下，工具仍必须能输出「可合并」。

    这条是 CRITICAL-1 的守卫：没有它，「每个 job 都要标记」的假绿/假红判定
    会在真实 run 上把退出码 0 卡死。
    """
    baseJobs = [
        _job(jobId=1, name="Frontend / typecheck + coverage", conclusion="success"),
        _job(jobId=2, name="Backend / unit shard 0", conclusion="failure"),
        _job(jobId=3, name="Backend / unit shard 1", conclusion="success"),
    ]
    baseFailures, baseAssessments = collectRunFailures(
        baseJobs, {1: "", 2: _RED_LOG, 3: _greenLog("UNIT", 1)}
    )

    prJobs = [
        _job(jobId=11, name="Frontend / typecheck + coverage", conclusion="success"),
        _job(jobId=12, name="Backend / unit shard 0", conclusion="failure"),
        _job(jobId=13, name="Backend / unit shard 1", conclusion="success"),
    ]
    prFailures, prAssessments = collectRunFailures(
        prJobs, {11: "", 12: _RED_LOG, 13: _greenLog("UNIT", 1)}
    )

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is True
    assert verdict.reasonCode == "MERGEABLE"


# --------------------------------------------------------------------------
# 完成标记与 job 名必须对得上
# --------------------------------------------------------------------------


def test_assessJob_rejects_invalid_shard_sentinel() -> None:
    """完成标记 `shard=-1` ⇒ CI 脚本判定**分片号非法** ⇒ 该 job 一个用例都没跑。

    ⇒ 它的失败集合是空的 ⇒ 参与对差只会掩盖真实失败 ⇒ 必须判为不可用。
    （哨兵值是 CI 脚本与工具之间的约定：见 `.github/workflows/ci.yml` 的
    `shard_index=-1`；旧实现打 0 ⇒ 这里会被误判成「标记与 job 不符」，
    真实原因「分片号非法」被掩盖 ⇒ 操作者会去查错方向。）
    """
    logText = "\n".join(
        [
            f"{_TIMESTAMPED}UNIT_SHARD_DONE shard=-1 status=4",
        ]
    )

    assessment = assessJob(_job(), logText)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None
    assert "分片号非法" in assessment.unusableReason


def test_assessJob_rejects_marker_with_mismatched_family() -> None:
    """unit 分片的日志里出现 `INTEGRATION_SHARD_DONE` ⇒ 标记不可信 ⇒ 不可用。

    ⚠️ **fixture 必须带 pytest 收尾汇总行**（变异取证 M5 实测）：没有汇总行时
    「日志里没有 pytest 的收尾汇总行」那条规则会**先**把 job 判成不可用
    ⇒ 关掉「标记族名/分片号交叉校验」照样绿 ⇒ **恒绿守卫**。
    ⇒ 带上一行汇总行后，**只剩**交叉校验能把 job 判成不可用 ⇒ 关掉它就转红。
    """
    log = "\n".join(
        [
            f"{_TIMESTAMPED}5431 passed, 1 skipped in 812.34s",
            f"{_TIMESTAMPED}INTEGRATION_SHARD_DONE shard=0 status=0",
        ]
    )

    assessment = assessJob(_job(name="Backend / unit shard 0", conclusion="success"), log)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None


def test_assessJob_rejects_marker_with_mismatched_shard_index() -> None:
    """分片 0 的日志里出现 `UNIT_SHARD_DONE shard=1` ⇒ 标记不可信 ⇒ 不可用。

    ⚠️ 同上：fixture 必须带 pytest 收尾汇总行，否则会被「缺汇总行」那条规则遮蔽
    ⇒ 交叉校验就不再是唯一原因（变异取证 M5 实测）。
    """
    log = "\n".join(
        [
            f"{_TIMESTAMPED}5431 passed, 1 skipped in 812.34s",
            f"{_TIMESTAMPED}UNIT_SHARD_DONE shard=1 status=0",
        ]
    )

    assessment = assessJob(_job(name="Backend / unit shard 0", conclusion="success"), log)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None


def test_assessJob_rejects_marker_for_unknown_job_name() -> None:
    """日志里有分片标记但 job 名不符合协议 ⇒ 不可用（不可降级成「非分片 job」）。

    否则一个 status=137（被信号杀死）的分片会被当成普通 job ⇒ 失败集合为空
    ⇒ 参与对差 ⇒ 假「净新增 0」。
    """
    log = f"{_TIMESTAMPED}UNIT_SHARD_DONE shard=0 status=137"

    assessment = assessJob(_job(name="Backend / unknown job", conclusion="failure"), log)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None
    assert "分片完成标记" in assessment.unusableReason


def test_assessJob_rejects_shard_named_job_with_unknown_protocol() -> None:
    """job 名**看起来**是分片 job 却不匹配协议 ⇒ 不可用（协议被改过）。

    覆盖两种形态：协议词被换掉（`unit group 0`）与分片号不是数字
    （`unit shard X`）。两者都会让「这个 job 到底是哪个分片」变得不确定
    ⇒ 它贡献的失败集合不能参与对差。
    """
    for name in ("Backend / unit group 0", "Backend / unit shard X"):
        assessment = assessJob(_job(name=name, conclusion="success"), "")

        assert assessment.isUsable is False, name
        assert assessment.unusableReason is not None, name


def test_assessJob_rejects_log_with_multiple_markers() -> None:
    """日志里出现**多条**完成标记 ⇒ 疑似有伪造标记 ⇒ 不可用。

    pytest 的 stdout 由 PR 作者可控（测试里可以 print）⇒ 可以伪造一条
    `INTEGRATION_SHARD_DONE shard=0 status=0`。真标记只会打一次 ⇒ 出现多条
    必须人工确认，不能只取最后一条就放行。

    日志带 pytest 收尾汇总行：这是**唯一**拒绝它的规则 —— 否则「status=1
    无汇总行」那条规则会替它挡枪，本用例就测不到多标记判据本身。
    """
    log = "\n".join(
        [
            f"{_TIMESTAMPED}INTEGRATION_SHARD_DONE shard=0 status=0",
            f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y - assert 1 == 2",
            f"{_TIMESTAMPED}1 failed, 1 passed in 12.34s",
            f"{_TIMESTAMPED}INTEGRATION_SHARD_DONE shard=0 status=1",
        ]
    )

    assessment = assessJob(_job(name="Backend / integration shard 0"), log)

    assert assessment.isUsable is False
    assert assessment.unusableReason is not None
    assert assessment.markerCount == 2


# --------------------------------------------------------------------------
# 覆盖等价性（两次 run 必须跑同一批 job）
# --------------------------------------------------------------------------


def test_verdict_is_blocked_when_job_coverage_differs() -> None:
    """**PR 少跑 job ⇒ 失败集合只会变小 ⇒ 「净新增 0」是假的** ⇒ 必须阻断。

    攻击路径（本分支自己就在改 `ci.yml`，所以现实可达）：把
    `matrix.shard: [0,1,2,3]` 改成 `[0]`，或给某个 shard 加 `if: false`
    ⇒ 原本命中失败用例的分片根本不跑 ⇒ PR 失败集合为空 ⇒ 旧实现判「可合并」，
    而 GitHub 上 CI 还是**全绿**（少跑的分片没人失败）—— 连二次防线都没有。
    """
    baseJobs = [
        _job(jobId=1, name="Backend / integration shard 0", conclusion="failure"),
        _job(jobId=2, name="Backend / integration shard 1", conclusion="failure"),
    ]
    baseFailures, baseAssessments = collectRunFailures(
        baseJobs, {1: _redLog("INTEGRATION", 0), 2: _redLog("INTEGRATION", 1)}
    )

    prJobs = [_job(jobId=11, name="Backend / integration shard 0", conclusion="success")]
    prFailures, prAssessments = collectRunFailures(prJobs, {11: _greenLog("INTEGRATION", 0)})

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is False
    assert verdict.blockingReason is not None
    assert verdict.reasonCode == "COVERAGE_MISMATCH"


def test_verdict_is_mergeable_when_job_coverage_matches() -> None:
    """两侧 job 名集合一致 ⇒ 覆盖等价 ⇒ 正常对差。"""
    baseJobs = [_job(jobId=1, name="Backend / integration shard 0", conclusion="failure")]
    baseFailures, baseAssessments = collectRunFailures(baseJobs, {1: _redLog("INTEGRATION", 0)})
    prJobs = [_job(jobId=99, name="Backend / integration shard 0", conclusion="failure")]
    prFailures, prAssessments = collectRunFailures(prJobs, {99: _redLog("INTEGRATION", 0)})

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is True
    assert verdict.reasonCode == "MERGEABLE"


# --------------------------------------------------------------------------
# 报告输出（净化：终端转义注入）
# --------------------------------------------------------------------------


def test_sanitizeForReport_strips_control_sequences() -> None:
    """C0/C1 控制字符与 ESC 序列必须被剔除。

    否则 PR 作者可以在测试里 print 一条带 ANSI 的「失败用例 ID」：
    - OSC 52 改写维护者终端的剪贴板；
    - CSI 序列（`\x1b[2K\r`）擦除报告已打印的行 ⇒ **伪造出「结论：可合并」**。
    """
    dirty = "\x1b]52;c;Zm9v\r\x07 app/tests/x.py::t \x1b[2K"

    cleaned = sanitizeForReport(dirty)

    assert "\x1b" not in cleaned
    assert "\r" not in cleaned
    assert "\x07" not in cleaned
    assert "app/tests/x.py::t" in cleaned


def test_renderReport_never_prints_control_sequences() -> None:
    """报告里的一切外部字符串都要过净化 —— 包括 job 名与失败用例 ID。"""
    injected = "\x1b]52;c;Zm9v\r\x07"
    log = f"{_TIMESTAMPED}FAILED {injected} app/tests/unit/test_x.py::test_y"
    jobs = [_job(jobId=1, name=f"Backend / unit shard 0{injected}", conclusion="failure")]
    failures, assessments = collectRunFailures(jobs, {1: log})

    report = renderReport(
        "111",
        "222",
        failures,
        failures,
        diffFailureSets(failures, failures),
        assessments,
        assessments,
        verdictFor(diffFailureSets(failures, failures), assessments, assessments),
    )

    assert "\x1b" not in report
    assert "\x07" not in report


def test_renderReport_never_prints_control_sequences_in_unusable_reason() -> None:
    """**不可用原因**也必须过净化 —— 它来自日志内容（PR 作者可控）。

    实测漏在这里：`status` 字段直接插 `unusableReason` ⇒ 攻击者用 CSI 序列
    （`\x1b[2K\r`）就能擦掉报告里已打印的「结论：不可合并」行。
    """
    injected = "\x1b]52;c;Zm9v\r\x07"
    log = f"{_TIMESTAMPED}UNIT_SHARD_DONE shard=0 status=137 {injected}"
    jobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="cancelled")]
    failures, assessments = collectRunFailures(jobs, {1: log})

    report = renderReport(
        "111",
        "222",
        failures,
        failures,
        diffFailureSets(failures, failures),
        assessments,
        assessments,
        verdictFor(diffFailureSets(failures, failures), assessments, assessments),
    )

    assert "\x1b" not in report
    assert "\x07" not in report


def test_renderReport_shows_both_runs() -> None:
    """报告要同时列出基线与 PR 的逐 job 判定（否则看不到基线侧的问题）。"""
    baseJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    baseFailures, baseAssessments = collectRunFailures(baseJobs, {1: _RED_LOG})
    prJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    prFailures, prAssessments = collectRunFailures(prJobs, {1: _RED_LOG})

    diff = diffFailureSets(baseFailures, prFailures)
    verdict = verdictFor(diff, baseAssessments, prAssessments)
    report = renderReport("111", "222", baseFailures, prFailures, diff, baseAssessments, prAssessments, verdict)

    assert "[基线]" in report
    assert "[PR]" in report
    assert "基线 run 111" in report
    assert "PR    run 222" in report


def test_renderReport_shows_run_head_shas() -> None:
    """报告必须印出两次 run 的 head sha（否则无法确认「比的是哪两个点」）。"""
    baseJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    baseFailures, baseAssessments = collectRunFailures(baseJobs, {1: _RED_LOG})
    diff = diffFailureSets(baseFailures, baseFailures)
    verdict = verdictFor(diff, baseAssessments, baseAssessments)

    report = renderReport(
        "111",
        "222",
        baseFailures,
        baseFailures,
        diff,
        baseAssessments,
        baseAssessments,
        verdict,
        "a" * 40,
        "b" * 40,
    )

    assert "a" * 40 in report
    assert "b" * 40 in report


# --------------------------------------------------------------------------
# CLI 退出码契约
# --------------------------------------------------------------------------


def _stubCiSurface(
    monkeypatch: pytest.MonkeyPatch,
    changedFiles: tuple[str, ...],
    error: str | None = None,
) -> None:
    """把「CI 相关面判定」换成固定结果（避免真调 gh 的 compare API）。

    默认给空清单 ⇒ CI 相关面守卫不触发 ⇒ CLI 用例只需关注失败集合对差本身。
    """
    monkeypatch.setattr(
        ci_failure_set,
        "resolveCiSurface",
        lambda repo, baseRunId, prRunId: CiSurfaceInfo(
            baseSha="a" * 40,
            prSha="b" * 40,
            changedFiles=changedFiles,
            error=error,
        ),
    )


def _stubHeadFreshness(
    monkeypatch: pytest.MonkeyPatch,
    isFresh: bool = True,
    reason: str | None = None,
    note: str | None = None,
) -> None:
    """把「PR run 新鲜度判定」换成固定结果（避免真调 gh 查分支 head）。

    默认新鲜 ⇒ CLI 用例只需关注失败集合对差本身。
    """
    monkeypatch.setattr(
        ci_failure_set,
        "resolveHeadFreshness",
        lambda repo, runId, runSha: HeadFreshness(isFresh=isFresh, reason=reason, note=note),
    )


def _stubGithub(
    monkeypatch: pytest.MonkeyPatch,
    baseResult: tuple[frozenset[str], tuple[JobAssessment, ...]],
    prResult: tuple[frozenset[str], tuple[JobAssessment, ...]],
) -> None:
    """把 `collectRunFromGithub` 换成固定返回值（避免真调 gh）。"""
    _stubCiSurface(monkeypatch, ())
    _stubHeadFreshness(monkeypatch)

    def fake(repo: str, runId: str):
        return baseResult if runId == "111" else prResult

    monkeypatch.setattr(ci_failure_set, "collectRunFromGithub", fake)


def _oneRedShard() -> tuple[frozenset[str], tuple[JobAssessment, ...]]:
    jobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    return collectRunFailures(jobs, {1: _RED_LOG})


def test_main_returns_zero_when_mergeable(monkeypatch: pytest.MonkeyPatch) -> None:
    """净新增 0 且全部 job 可用 ⇒ 退出码 0。"""
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 0


def test_main_returns_one_when_new_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """净新增失败 ⇒ 退出码 1。"""
    baseJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    baseResult = collectRunFailures(baseJobs, {1: _RED_LOG})
    prJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    prResult = collectRunFailures(
        prJobs, {1: _redLogWithExtra()}
    )
    _stubGithub(monkeypatch, baseResult, prResult)

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 1


def test_main_returns_two_when_any_job_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    """存在不可用 job ⇒ 退出码 2（必须复跑）。"""
    baseJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    baseResult = collectRunFailures(baseJobs, {1: _RED_LOG})
    prJobs = [
        _job(jobId=1, name="Backend / unit shard 0", conclusion="failure"),
        _job(jobId=2, name="Backend / unit shard 1", conclusion="cancelled"),
    ]
    prResult = collectRunFailures(prJobs, {1: _RED_LOG, 2: _RED_LOG})
    _stubGithub(monkeypatch, baseResult, prResult)

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 2


def test_main_returns_five_when_failures_were_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """净丢失失败 ⇒ 退出码 5（须人工确认）。"""
    baseJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    baseResult = collectRunFailures(baseJobs, {1: _RED_LOG})
    prJobs = [_job(jobId=1, name="Backend / unit shard 0", conclusion="success")]
    prResult = collectRunFailures(prJobs, {1: _GREEN_LOG})
    _stubGithub(monkeypatch, baseResult, prResult)

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 5


def test_main_returns_four_when_job_coverage_differs(monkeypatch: pytest.MonkeyPatch) -> None:
    """两次 run 的 job 集合不等价 ⇒ 退出码 4。"""
    baseJobs = [
        _job(jobId=1, name="Backend / integration shard 0", conclusion="failure"),
        _job(jobId=2, name="Backend / integration shard 1", conclusion="failure"),
    ]
    baseResult = collectRunFailures(
        baseJobs, {1: _redLog("INTEGRATION", 0), 2: _redLog("INTEGRATION", 1)}
    )
    prJobs = [_job(jobId=11, name="Backend / integration shard 0", conclusion="success")]
    prResult = collectRunFailures(prJobs, {11: _greenLog("INTEGRATION", 0)})
    _stubGithub(monkeypatch, baseResult, prResult)

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 4


def test_main_returns_three_when_repo_argument_is_malformed() -> None:
    exitCode = ci_failure_set.main(["--repo", "../evil", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 3


def test_main_returns_three_when_run_id_is_not_numeric() -> None:
    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "abc", "--pr-run", "222"])

    assert exitCode == 3


def test_main_returns_three_when_both_runs_are_the_same(monkeypatch: pytest.MonkeyPatch) -> None:
    """**自比自**（同一个 run id 填两次）⇒ 失败集合必然相同 ⇒ 假「净新增 0」。

    必须拒绝：否则报告会打印一份「基线 111 / PR 111 / 净新增 0 ⇒ 可合并」
    的**看起来完全正常**的绿灯报告，而实际上从未比较过任何东西。

    stub 掉 github：变异掉「同 id 拒绝」后，main 会继续走下去拿到两侧相同
    的失败集合 ⇒ 净新增 0 ⇒ 退出 0 ⇒ 本用例转红（不 stub 的话真调 gh 失败
    也会退 3，变异就被掩盖了）。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "111"])

    assert exitCode == 3


def test_main_returns_three_when_a_run_has_no_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """某侧 run **一个 job 都没有** ⇒ 失败集合是空集 ⇒ 对差不成立。

    现实场景：run id 指错 / workflow 没跑起来 / job 列表尚未归档。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, (frozenset(), ()))

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 3


def test_main_returns_three_when_github_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """gh 调用失败 ⇒ 退出码 3（不是 0）。"""

    def boom(repo: str, runId: str):
        raise RuntimeError("gh 挂了")

    monkeypatch.setattr(ci_failure_set, "collectRunFromGithub", boom)

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 3


def test_main_returns_three_when_report_rendering_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """报告渲染异常 ⇒ 退出码 3（**绝不能**落到 0 —— 结论不可见即不可合并）。"""
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)

    def boom(*args, **kwargs):
        raise RuntimeError("渲染炸了")

    monkeypatch.setattr(ci_failure_set, "renderReport", boom)

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 3


# --------------------------------------------------------------------------
# CI 相关面守卫（PR 改了 CI 文件 ⇒ 失败集合不可比）
# --------------------------------------------------------------------------


def test_main_returns_six_when_ci_surface_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR 改了 CI 相关文件 ⇒ 失败集合**不可比** ⇒ 退出码 6（不是 0）。

    攻击路径：PR 用 `pytest_collection_modifyitems` 把失败用例跳过 ⇒
    失败用例**根本不产生 FAILED 行** ⇒ 失败集合与基线完全相同 ⇒ 假「净新增 0」。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    _stubCiSurface(monkeypatch, (".github/workflows/ci.yml",))

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 6


def test_main_returns_zero_when_ci_changes_explicitly_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--allow-ci-changes` = 已人工确认无影响 ⇒ 退出码 0 恢复可达。"""
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    _stubCiSurface(monkeypatch, (".github/workflows/ci.yml",))

    exitCode = ci_failure_set.main(
        ["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222", "--allow-ci-changes"]
    )

    assert exitCode == 0


def test_main_returns_six_when_changed_files_cannot_be_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    """判定不出「PR 改了哪些文件」⇒ **保守拦下**（不知道 ≠ 没有）。"""
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    _stubCiSurface(monkeypatch, (), error="compare 挂了")

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 6


# --------------------------------------------------------------------------
# PR run 新鲜度（run 跑的必须就是 PR 当前那版代码）
# --------------------------------------------------------------------------


def test_main_returns_three_when_pr_run_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR run **已过期**（PR 又推了新 commit）⇒ 对差的是旧代码 ⇒ 退出码 3。

    这是 2026-11-03 代码审查 M-1 指出的漏洞：工具把 `surface.prSha` 印在报告里
    却**从不**与 PR 当前 head 比较 ⇒ 拿上一次 push 的 run 对差会得出「净新增 0」，
    而 GitHub 上最新那次 CI 可能已经红了、没人看。
    """
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    _stubHeadFreshness(
        monkeypatch,
        isFresh=False,
        reason="PR run 222 跑的是 aaaa…，而分支当前 head 已是 bbbb… ⇒ run 已过期",
    )

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])

    assert exitCode == 3


def test_main_surfaces_fork_note_in_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """fork PR 判不出新鲜度 ⇒ 不拦，但**必须在报告里明示**（唯一一处「判不出来不拦」）。"""
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    _stubHeadFreshness(
        monkeypatch,
        isFresh=True,
        note="PR 来自 fork 仓库 ⇒ 无法自动核对 run 是否最新 ⇒ 请人工确认",
    )

    exitCode = ci_failure_set.main(["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222"])
    report = capsys.readouterr().out

    assert exitCode == 0
    assert "请人工确认" in report


def test_main_surfaces_allow_ci_changes_note(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """用了 `--allow-ci-changes` ⇒ 报告里必须留痕（豁免不是静默的）。"""
    result = _oneRedShard()
    _stubGithub(monkeypatch, result, result)
    _stubCiSurface(monkeypatch, (".github/workflows/ci.yml",))

    exitCode = ci_failure_set.main(
        ["--repo", "sunql/QA", "--base-run", "111", "--pr-run", "222", "--allow-ci-changes"]
    )
    report = capsys.readouterr().out

    assert exitCode == 0
    assert "allow-ci-changes" in report
