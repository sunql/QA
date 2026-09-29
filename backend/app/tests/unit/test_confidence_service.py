"""M3 置信度 4 级纯函数单测（v3.1 蓝图 §12.2 / 任务 B4）。

覆盖硬约束 1：4 级全部分支（含冷启动豁免、REFUSE 三种前置原因各一条、
计分边界 0/1/2/3）+ 注入式 historySnapshot（生产 v1 传 None）。
本文件不用 DB（覆盖 conftest autouse dbSession）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from app.services.confidence_service import (
    HIGH_SCORE,
    MIN_HISTORY_USAGE,
    AccuracyThreshold,
    ConfidenceHistorySnapshot,
    ConfidenceLevel,
    calculateConfidence,
    claimRulesMatched,
    evaluateEvidenceCompleteness,
    formatConfidenceForPrompt,
    hasTripleFilled,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def dbSession() -> Any:
    """覆盖 conftest autouse dbSession —— 本文件不用 DB。"""
    return None


@pytest_asyncio.fixture()
async def seedEngine() -> Any:
    """覆盖 conftest autouse seedEngine —— 不建引擎、不 truncate。"""
    return None


@pytest_asyncio.fixture()
async def warmBusinessObjectRegistry() -> Any:
    """覆盖 conftest autouse warmBusinessObjectRegistry —— 无需 DB。"""
    yield


# ---------------------------------------------------------------------------
# REFUSE 三种前置原因（各一条）+ 计分 0
# ---------------------------------------------------------------------------


async def test_refuse_when_no_evidence() -> None:
    result = calculateConfidence(
        evidenceCount=0, isStale=False, hasOpenConflict=False,
        hasCompleteEvidence=False, rulesMatched=True,
    )
    assert result.level is ConfidenceLevel.REFUSE
    assert result.score == 0
    assert result.refuseReason is not None and "证据" in result.refuseReason


async def test_refuse_when_stale() -> None:
    result = calculateConfidence(
        evidenceCount=2, isStale=True, hasOpenConflict=False,
        hasCompleteEvidence=True, rulesMatched=True,
    )
    assert result.level is ConfidenceLevel.REFUSE
    assert result.refuseReason is not None and "数据未更新" in result.refuseReason


async def test_refuse_when_open_conflict() -> None:
    result = calculateConfidence(
        evidenceCount=2, isStale=False, hasOpenConflict=True,
        hasCompleteEvidence=True, rulesMatched=True,
    )
    assert result.level is ConfidenceLevel.REFUSE
    assert result.refuseReason is not None and "冲突" in result.refuseReason


async def test_refuse_when_score_zero() -> None:
    """有证据但证据不完整、无规则匹配、无历史 → 计分 0 也拒绝。"""
    result = calculateConfidence(
        evidenceCount=1, isStale=False, hasOpenConflict=False,
        hasCompleteEvidence=False, rulesMatched=False,
    )
    assert result.level is ConfidenceLevel.REFUSE
    assert result.score == 0
    assert result.refuseReason is not None


# ---------------------------------------------------------------------------
# 计分边界 1/2/3 → LOW / MEDIUM / HIGH
# ---------------------------------------------------------------------------


async def test_score_one_is_low() -> None:
    result = calculateConfidence(
        evidenceCount=1, isStale=False, hasOpenConflict=False,
        hasCompleteEvidence=True, rulesMatched=False,
    )
    assert result.level is ConfidenceLevel.LOW
    assert result.score == 1


async def test_score_two_is_medium() -> None:
    result = calculateConfidence(
        evidenceCount=1, isStale=False, hasOpenConflict=False,
        hasCompleteEvidence=True, rulesMatched=True,
    )
    assert result.level is ConfidenceLevel.MEDIUM
    assert result.score == 2


async def test_score_three_is_high() -> None:
    result = calculateConfidence(
        evidenceCount=3, isStale=False, hasOpenConflict=False,
        hasCompleteEvidence=True, rulesMatched=True,
        historySnapshot=ConfidenceHistorySnapshot(usageCount=20, accuracy=0.95),
    )
    assert result.level is ConfidenceLevel.HIGH
    assert result.score == 3


# ---------------------------------------------------------------------------
# 冷启动豁免 + 注入式历史 provider（生产 v1 传 None）
# ---------------------------------------------------------------------------


async def test_cold_start_history_structurally_unreachable() -> None:
    """historySnapshot=None（生产 v1）→ 历史两项结构性不可达，上限 2 分。"""
    result = calculateConfidence(
        evidenceCount=5, isStale=False, hasOpenConflict=False,
        hasCompleteEvidence=True, rulesMatched=True, historySnapshot=None,
    )
    assert result.level is ConfidenceLevel.MEDIUM
    assert result.score == 2
    breakdown = result.breakdown
    assert breakdown.hasSufficientHistory is False
    assert breakdown.hasHighHistoricalAccuracy is False


async def test_history_usage_count_boundary() -> None:
    """min=10 含边界：usageCount=10 计 1 分，9 不计。"""
    at10 = calculateConfidence(
        evidenceCount=1, isStale=False, hasOpenConflict=False,
        hasCompleteEvidence=False, rulesMatched=False,
        historySnapshot=ConfidenceHistorySnapshot(usageCount=MIN_HISTORY_USAGE, accuracy=None),
    )
    assert at10.score == 1
    assert at10.breakdown.hasSufficientHistory is True

    at9 = calculateConfidence(
        evidenceCount=1, isStale=False, hasOpenConflict=False,
        hasCompleteEvidence=False, rulesMatched=False,
        historySnapshot=ConfidenceHistorySnapshot(usageCount=MIN_HISTORY_USAGE - 1, accuracy=None),
    )
    assert at9.score == 0
    assert at9.breakdown.hasSufficientHistory is False


async def test_history_accuracy_boundary_exclusive() -> None:
    """accuracy > 0.9 严格大于：0.9 本身不计，0.91 计。"""
    exact = calculateConfidence(
        evidenceCount=1, isStale=False, hasOpenConflict=False,
        hasCompleteEvidence=False, rulesMatched=False,
        historySnapshot=ConfidenceHistorySnapshot(usageCount=0, accuracy=AccuracyThreshold),
    )
    assert exact.breakdown.hasHighHistoricalAccuracy is False

    above = calculateConfidence(
        evidenceCount=1, isStale=False, hasOpenConflict=False,
        hasCompleteEvidence=False, rulesMatched=False,
        historySnapshot=ConfidenceHistorySnapshot(usageCount=0, accuracy=0.91),
    )
    assert above.breakdown.hasHighHistoricalAccuracy is True


async def test_history_score_clamped_to_three() -> None:
    """蓝图映射只到 3：全项命中时封顶 HIGH/score=3（brief 明确 score 0-3）。"""
    result = calculateConfidence(
        evidenceCount=1, isStale=False, hasOpenConflict=False,
        hasCompleteEvidence=True, rulesMatched=True,
        historySnapshot=ConfidenceHistorySnapshot(usageCount=50, accuracy=0.99),
    )
    assert result.level is ConfidenceLevel.HIGH
    assert result.score == HIGH_SCORE


async def test_snapshot_immutable() -> None:
    snapshot = ConfidenceHistorySnapshot(usageCount=1, accuracy=0.5)
    with pytest.raises(FrozenInstanceError):
        snapshot.usageCount = 2  # type: ignore[misc]


async def test_result_immutable_and_breakdown_explainable() -> None:
    result = calculateConfidence(
        evidenceCount=1, isStale=False, hasOpenConflict=False,
        hasCompleteEvidence=True, rulesMatched=True,
    )
    with pytest.raises(FrozenInstanceError):
        result.score = 3  # type: ignore[misc]
    b = result.breakdown
    assert b.hasEvidence is True
    assert b.isStale is False
    assert b.hasOpenConflict is False
    assert b.hasCompleteEvidence is True
    assert b.rulesMatched is True
    assert b.score == 2


# ---------------------------------------------------------------------------
# rules_matched：_hasTriple 判定提取的可复用纯函数
# ---------------------------------------------------------------------------


async def test_has_triple_filled_all_empty_is_false() -> None:
    assert hasTripleFilled(subjectId=None, predicate=None, objectValue=None, objectType=None) is False


async def test_has_triple_filled_any_nonempty_is_true() -> None:
    assert hasTripleFilled(subjectId="S1", predicate=None, objectValue=None, objectType=None) is True
    assert hasTripleFilled(subjectId=None, predicate=None, objectValue="", objectType="STATISTIC") is True


async def test_claim_rules_matched_delegates() -> None:
    claim = SimpleNamespace(subject_id="S", predicate=None, object_value=None, object_type=None)
    assert claimRulesMatched(claim) is True


async def test_wiki_page_service_has_triple_delegates_to_shared_function() -> None:
    """原 WikiPageService._hasTriple 改为委托共享纯函数（A5 R1 教训：禁私有符号导入）。"""
    from app.services.wiki_page_service import WikiPageService

    svc = WikiPageService()
    claim = SimpleNamespace(subject_id=None, predicate="p", object_value=None, object_type=None)
    assert svc._hasTriple(claim) is True  # noqa: SLF001 — 测试本身就是验证该私有方法


# ---------------------------------------------------------------------------
# has_complete_evidence：≥1 条且每条 payload / source_id 至少一项非空
# ---------------------------------------------------------------------------


def _ev(payload: Any = None, sourceId: str | None = None) -> Any:
    return SimpleNamespace(payload=payload, source_id=sourceId)


async def test_evidence_completeness_empty_list_false() -> None:
    assert evaluateEvidenceCompleteness([]) is False


async def test_evidence_completeness_payload_present_true() -> None:
    assert evaluateEvidenceCompleteness([_ev(payload={"rows": 1})]) is True


async def test_evidence_completeness_source_id_present_true() -> None:
    assert evaluateEvidenceCompleteness([_ev(sourceId="DOC-1")]) is True


async def test_evidence_completeness_empty_payload_and_blank_source_false() -> None:
    assert evaluateEvidenceCompleteness([_ev(payload={}, sourceId="")]) is False


async def test_evidence_completeness_one_bad_apple_false() -> None:
    """一条不完整即整体不完整（规格：每条都要求至少一项非空）。"""
    evidences = [_ev(sourceId="DOC-1"), _ev(payload=None, sourceId=None)]
    assert evaluateEvidenceCompleteness(evidences) is False


# ---------------------------------------------------------------------------
# LLM 注入 helper（只做 helper 不接线；绝不输出数值）
# ---------------------------------------------------------------------------


async def test_format_for_prompt_outputs_level_word_without_numbers() -> None:
    # 每级一组入参，保证恰好落在该级别
    kwargsByLevel = {
        ConfidenceLevel.HIGH: dict(
            hasCompleteEvidence=True, rulesMatched=True,
            historySnapshot=ConfidenceHistorySnapshot(usageCount=30, accuracy=0.99),
        ),
        ConfidenceLevel.MEDIUM: dict(
            hasCompleteEvidence=True, rulesMatched=True, historySnapshot=None,
        ),
        ConfidenceLevel.LOW: dict(
            hasCompleteEvidence=True, rulesMatched=False, historySnapshot=None,
        ),
        ConfidenceLevel.REFUSE: dict(
            hasCompleteEvidence=False, rulesMatched=False, historySnapshot=None,
        ),
    }
    for level, kwargs in kwargsByLevel.items():
        result = calculateConfidence(
            evidenceCount=1, isStale=False, hasOpenConflict=False, **kwargs
        )
        assert result.level is level
        text = formatConfidenceForPrompt(result)
        assert level.value in text
        assert not any(ch.isdigit() for ch in text), f"prompt 不得输出数值：{text}"
