"""CI 失败集合对差：判据必须**拒绝**「不可用的 job 贡献了一个空失败集合」。

背景：本仓 CI 基线是红的（`main` 上即有既有失败）⇒ 合并判据不是「CI 变绿」，
而是**失败集合对差**：PR run 的失败用例集合 ⊆ 基线 run 的失败用例集合。

人工做这件事已经踩过四个坑（全部实测于 2026-11-02 的 PR #98/#99 批次）：

1. **`cancelled` 的 job 失败集合是空的** ⇒ 直接对差会得出「净新增 0」的**假结论**。
2. **缺完成标记 = 没跑完** ⇒ 失败集合同样不可用。
3. **GH Actions 日志行带时间戳前缀** ⇒ 正则不能锚定 `^FAILED`（那样一条都匹配不到）。
4. **`comm` 要求两侧同序同 collation** ⇒ python `sorted()` 与 `sort -u` 不同序时
   会输出**两边都非空**的假差异。

⇒ 本模块把判据做成**可执行**的：任何不可用 job 存在时，结论一律是
「不可用，必须复跑」，**不允许**输出「净新增 0 ⇒ 可合并」。

本文件把这条性质钉死，并保证它不是恒绿（把 `cancelled` 判成可用即转红）。
"""

from __future__ import annotations

from app.utils.ci_failure_set import (
    CiJob,
    assessJob,
    collectRunFailures,
    diffFailureSets,
    extractFailureIds,
    parseShardDoneMarkers,
    verdictFor,
)

# GH Actions 的真实日志行形状：行首是 ISO 时间戳 + 空格
_TIMESTAMPED = "2026-11-02T07:10:13.4960640Z "

# 一份「unit 分片跑完但红了」的日志片段（真实形状）
_RED_LOG = "\n".join(
    [
        f"{_TIMESTAMPED}platform linux -- Python 3.13.0, pytest-8.3.4",
        f"{_TIMESTAMPED}FAILED app/tests/unit/test_chat_service.py::test_reply - assert 1 == 2",
        f"{_TIMESTAMPED}FAILED app/tests/unit/test_rate_limit.py::test_burst - AssertionError",
        f"{_TIMESTAMPED}UNIT_SHARD_DONE shard=1 status=1",
    ]
)

# 一份「分片跑完且全绿」的日志片段
_GREEN_LOG = "\n".join(
    [
        f"{_TIMESTAMPED}5431 passed, 1 skipped in 812.34s",
        f"{_TIMESTAMPED}UNIT_SHARD_DONE shard=0 status=0",
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


# --------------------------------------------------------------------------
# 完成标记
# --------------------------------------------------------------------------


def test_parseShardDoneMarkers_reads_marker_with_status() -> None:
    """标记形如 `X_SHARD_DONE shard=N status=<退出码>`。"""
    markers = parseShardDoneMarkers(_RED_LOG)

    assert len(markers) == 1
    assert markers[0].name == "UNIT_SHARD_DONE"
    assert markers[0].shard == 1
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


def test_assessJob_flags_failure_without_detail() -> None:
    """真红但一条 FAILED 行都没有 ⇒ 可能是收集期错误或基础设施失败 ⇒ 要提醒人工核对。"""
    log = f"{_TIMESTAMPED}INTEGRATION_SHARD_DONE shard=0 status=1"

    assessment = assessJob(_job(), log)

    assert assessment.isUsable is True
    assert assessment.failureIds == ()
    assert assessment.hasFailureWithoutDetail is True


# --------------------------------------------------------------------------
# 整轮 run 的汇总与对差
# --------------------------------------------------------------------------


def test_collectRunFailures_unions_failures_of_usable_jobs() -> None:
    """可用 job 的失败集合取并集。"""
    jobs = [
        _job(jobId=1, name="shard 0", conclusion="failure"),
        _job(jobId=2, name="shard 1", conclusion="success"),
    ]
    logs = {
        1: _RED_LOG,
        2: _GREEN_LOG,
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
        _job(jobId=1, name="shard 0", conclusion="failure"),
        _job(jobId=2, name="shard 1", conclusion="cancelled"),
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
    jobs = [_job(jobId=1, name="shard 0", conclusion="failure")]
    logs = {1: _RED_LOG}
    baseFailures, baseAssessments = collectRunFailures(jobs, logs)
    prFailures, prAssessments = collectRunFailures(jobs, logs)

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is True
    assert verdict.blockingReason is None


def test_verdict_is_blocked_when_new_failures_appear() -> None:
    """净新增非空 ⇒ 不可合并。"""
    jobs = [_job(jobId=1, name="shard 0", conclusion="failure")]
    baseFailures, baseAssessments = collectRunFailures(jobs, {1: _RED_LOG})
    prFailures, prAssessments = collectRunFailures(jobs, {1: _RED_LOG + "\nFAILED app/tests/unit/test_new.py::test_x"})

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is False
    assert verdict.blockingReason is not None
    assert "app/tests/unit/test_new.py::test_x" in verdict.blockingReason


def test_verdict_is_blocked_when_any_job_is_unusable() -> None:
    """**核心守卫**：哪怕净新增为空，只要有不可用 job 就**不能**说「可合并」。

    若实现把 `cancelled` 判成可用（或忽略不可用 job 的存在），本用例立刻转红
    —— 这正是 PR #98 首轮「假净新增 0」的成因。
    """
    baseJobs = [_job(jobId=1, name="shard 0", conclusion="failure")]
    baseFailures, baseAssessments = collectRunFailures(baseJobs, {1: _RED_LOG})

    prJobs = [
        _job(jobId=1, name="shard 0", conclusion="failure"),
        _job(jobId=2, name="shard 1", conclusion="cancelled"),
    ]
    prFailures, prAssessments = collectRunFailures(prJobs, {1: _RED_LOG, 2: _RED_LOG})

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is False
    assert verdict.blockingReason is not None
    assert "cancelled" in verdict.blockingReason


def test_verdict_is_blocked_when_marker_missing() -> None:
    """缺完成标记的 job 同样让结论变成「不可用」。"""
    baseJobs = [_job(jobId=1, name="shard 0", conclusion="failure")]
    baseFailures, baseAssessments = collectRunFailures(baseJobs, {1: _RED_LOG})

    prJobs = [_job(jobId=1, name="shard 0", conclusion="failure")]
    prFailures, prAssessments = collectRunFailures(prJobs, {1: f"{_TIMESTAMPED}FAILED app/tests/unit/test_x.py::test_y"})

    verdict = verdictFor(diffFailureSets(baseFailures, prFailures), baseAssessments, prAssessments)

    assert verdict.canMerge is False
    assert verdict.blockingReason is not None
