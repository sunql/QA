"""Tier B 哨兵拆包 + SSE meta 字段形状（纯函数，无 IO）。"""

from app.services.chat_stream_output import (
    L1L4PreambleMixin,
    PreambleResult,
    kpiHintFields,
    splitTierBPreamble,
)
from app.services.kpi_semantic_match_service import KpiMatchResult


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


def test_hint_fields_datasource_mismatch_adds_warning_fields() -> None:
    """D4 评审：Tier B 哨兵不得吞掉 datasource mismatch 警告。"""
    hint = _tierB()
    hint = hint.model_copy(update={
        "datasource_mismatch": True,
        "mismatch_warning": "KPI 数据源 1 与所选数据源 2 不一致",
    })
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
