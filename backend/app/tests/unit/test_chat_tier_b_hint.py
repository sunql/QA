"""Tier B 哨兵拆包 + SSE meta 字段形状（纯函数，无 IO）。"""

import pytest

from app.domain.enums import IntentType
from app.services.chat_stream_output import (
    L1L4PreambleMixin,
    PreambleResult,
    kpiHintFields,
    secondFrameTierFields,
    splitTierBPreamble,
    tierHintClearedFields,
    tierHintFields,
)
from app.services.kpi_semantic_match_service import KpiMatchResult

# 与 chat_service.processMessage 的分支保持一致：允许集合 = 会落到
# `_handleGenericQuery`（随后 `_attachKpiHint`）的意图；其余意图早退，不得带 Tier B
# 观测字段。两集合必须穷尽 IntentType（见下方覆盖断言）。
_REJECTED_INTENTS = (
    IntentType.METRIC,
    IntentType.DEFINE,
    IntentType.MAP,
    IntentType.CHITCHAT,
    IntentType.SUPPLIER_360,
    IntentType.SUPPLIER_RISK,
    IntentType.GRAPH_REASONING,
    IntentType.AGENT_RUN,
)
_ALLOWED_INTENTS = (
    IntentType.QUERY,
    IntentType.NEW_QUERY,
    IntentType.REFINE,
    IntentType.FOLLOW_UP,
    IntentType.CLARIFY,
)


def _tierB() -> PreambleResult:
    return PreambleResult(
        routing_layer="L1B",
        answer="",
        kpi_code="KPI_X",
        kpi_name="指标X",
        confidence=0.9,
        kpi_match_method="containment",
        execution_tier="B",
        degraded_reason="explicit_time_scope",
    )


def _mismatchTierB() -> PreambleResult:
    """datasource mismatch 的 Tier B hint：kpiHintFields 会多带 3 个警告键。"""
    return _tierB().model_copy(update={
        "datasource_mismatch": True,
        "mismatch_warning": "KPI 数据源 1 与所选数据源 2 不一致",
    })


def test_split_tier_b_sentinel_returns_hint() -> None:
    preamble, hint = splitTierBPreamble(_tierB())
    assert preamble is None
    assert hint is not None and hint.routing_layer == "L1B"


def test_split_non_sentinel_returns_preamble() -> None:
    l1 = PreambleResult(routing_layer="L1", answer="a", execution_tier="A")
    preamble, hint = splitTierBPreamble(l1)
    assert preamble is l1 and hint is None


def test_split_none() -> None:
    assert splitTierBPreamble(None) == (None, None)


def test_hint_fields_wire_shape() -> None:
    assert kpiHintFields(_tierB()) == {
        "kpiCode": "KPI_X",
        "kpiName": "指标X",
        "confidence": 0.9,
        "matchMethod": "containment",
        "executionTier": "B",
        "degradedReason": "explicit_time_scope",
    }


def test_hint_fields_none_is_empty() -> None:
    assert kpiHintFields(None) == {}


def test_tier_hint_cleared_fields_cover_all_hint_keys() -> None:
    """第二帧清空字段必须覆盖首帧 Tier B 字段的**全部键**（防将来漏清）。"""
    hint = _tierB()
    cleared = tierHintClearedFields(hint)
    assert cleared
    assert set(cleared) == set(kpiHintFields(hint))
    assert set(cleared.values()) == {None}


def test_tier_hint_cleared_fields_cover_mismatch_warning_keys() -> None:
    """mismatch 追加的 datasourceMismatch/warning/code 也必须被清空。"""
    hint = _mismatchTierB()
    cleared = tierHintClearedFields(hint)
    assert set(cleared) == set(kpiHintFields(hint))
    assert {"datasourceMismatch", "warning", "code"} <= set(cleared)
    assert set(cleared.values()) == {None}


def test_second_frame_clears_every_first_frame_field_on_early_exit() -> None:
    """帧级：首帧发 Tier B 字段，重分类到早退意图后第二帧逐键显式置 None。"""
    hint = _mismatchTierB()

    first = secondFrameTierFields(hint, IntentType.QUERY)
    assert first
    assert first["executionTier"] == "B"
    assert first["datasourceMismatch"] is True

    # 重分类到早退意图：门控不发明文，且 hint 非 None ⇒ 显式清空整组键。
    second = secondFrameTierFields(hint, IntentType.METRIC)
    assert set(second) == set(first)
    assert set(second.values()) == {None}

    # 前端逐帧合并后的最终观测：首帧每个键都被覆盖为 None（不残留旧警告）。
    merged = {**first, **second}
    assert all(merged[key] is None for key in first)


def test_hint_fields_datasource_mismatch_adds_warning_fields() -> None:
    """D4 评审：Tier B 哨兵不得吞掉 datasource mismatch 警告。"""
    hint = _mismatchTierB()
    assert kpiHintFields(hint) == {
        "kpiCode": "KPI_X",
        "kpiName": "指标X",
        "confidence": 0.9,
        "matchMethod": "containment",
        "executionTier": "B",
        "degradedReason": "explicit_time_scope",
        "datasourceMismatch": True,
        "warning": "KPI 数据源 1 与所选数据源 2 不一致",
        "code": "KPI_DATASOURCE_MISMATCH",
    }


def test_tier_b_preamble_carries_datasource_mismatch() -> None:
    """`_tierBPreamble` 必须把 mismatch 信息带进哨兵（Tier B 主链路继续执行）。"""
    match = KpiMatchResult(code="KPI_X", kpi_name="指标X", confidence=0.9)

    class _Kpi:
        kpi_name = "指标X"
        datasource_id = 1

    sentinel = L1L4PreambleMixin._tierBPreamble(
        match, _Kpi(), "explicit_time_scope", 2
    )
    assert sentinel.datasource_mismatch is True
    assert sentinel.mismatch_warning is not None
    assert "1" in sentinel.mismatch_warning and "2" in sentinel.mismatch_warning


def test_tier_hint_intent_sets_cover_all_intent_types() -> None:
    """允许 + 拒绝必须互斥且穷尽 ``IntentType``。

    将来新增意图若没归类，本断言立即失败，避免静默漏配成「流式有、非流式无」。
    """
    allowed = set(_ALLOWED_INTENTS)
    rejected = set(_REJECTED_INTENTS)
    assert allowed.isdisjoint(rejected)
    assert allowed | rejected == set(IntentType)


@pytest.mark.parametrize("intent", _REJECTED_INTENTS)
def test_tier_hint_fields_rejects_non_generic_intents(intent) -> None:
    """非通用查询意图不得带 Tier B 观测字段（与 processMessage 早退分支同口径）。

    使用**非 None** 的 Tier B hint：被挡掉必须是意图门控的结果，而不是 hint 为空。
    """
    hint = _tierB()
    assert hint is not None
    assert tierHintFields(hint, intent) == {}


@pytest.mark.parametrize("intent", _ALLOWED_INTENTS)
def test_tier_hint_fields_allows_generic_query_intents(intent) -> None:
    fields = tierHintFields(_tierB(), intent)
    assert fields["kpiCode"] == "KPI_X"
    assert fields["executionTier"] == "B"
    assert fields["degradedReason"] == "explicit_time_scope"


def test_tier_hint_fields_none_hint_allowed_intent_is_empty() -> None:
    """交叉守卫：允许意图 + 无 hint ⇒ 空（门控不凭空造字段）。"""
    assert tierHintFields(None, IntentType.QUERY) == {}


def test_tier_hint_fields_rejected_intent_none_hint_is_empty() -> None:
    """交叉守卫：被拒意图 + 无 hint ⇒ 空。"""
    assert tierHintFields(None, IntentType.METRIC) == {}
