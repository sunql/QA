"""KPI 发布闸门（纯函数层，不碰库）。

闸门条件来自 spec §7.5：

- KPI 方向 5 项：metric_id / metric.status / execution_source / 对账 / datasource_id
- 本体指标方向：结构化齐全 + 技术形态未降级 + 关联 KPI 无 mismatch

「打破循环依赖」：本体指标方向**不要求**治理侧对账 pass（否则 KPI 与指标互锁）。
"""

from __future__ import annotations

from app.services.kpi_metric_publish_gate import (
    GateItem,
    GateResult,
    buildKpiGateItems,
    buildMetricGateItems,
    renderReconcileFindings,
)
from app.services.kpi_metric_reconcile import (
    LAYER_GOVERNANCE,
    VERDICT_UNKNOWN,
    ReconcileFinding,
)


def _codes(items) -> list[str]:
    return [i.code for i in items]


def _failed(items) -> list[str]:
    return [i.code for i in items if not i.ok]


# ── KPI 方向 ─────────────────────────────────────────────────────────


def test_kpi_gate_all_ok_passes() -> None:
    items = buildKpiGateItems(
        metricId=1,
        metricStatus="PUBLISHED",
        executionSource="COMPILED",
        datasourceId=7,
        reconcileVerdict="pass",
        reconcileDetail="",
    )
    assert _failed(items) == []


def test_kpi_gate_missing_metric_link() -> None:
    items = buildKpiGateItems(
        metricId=None,
        metricStatus=None,
        executionSource="COMPILED",
        datasourceId=7,
        reconcileVerdict="pass",
        reconcileDetail="",
    )
    assert "metric_linked" in _failed(items)


def test_kpi_gate_metric_not_published() -> None:
    items = buildKpiGateItems(
        metricId=1,
        metricStatus="DRAFT",
        executionSource="COMPILED",
        datasourceId=7,
        reconcileVerdict="pass",
        reconcileDetail="",
    )
    assert "metric_published" in _failed(items)


def test_kpi_gate_execution_source_none_blocked() -> None:
    items = buildKpiGateItems(
        metricId=1,
        metricStatus="PUBLISHED",
        executionSource="NONE",
        datasourceId=7,
        reconcileVerdict="pass",
        reconcileDetail="",
    )
    assert "execution_source" in _failed(items)


def test_kpi_gate_execution_source_empty_blocked() -> None:
    items = buildKpiGateItems(
        metricId=1,
        metricStatus="PUBLISHED",
        executionSource=None,
        datasourceId=7,
        reconcileVerdict="pass",
        reconcileDetail="",
    )
    assert "execution_source" in _failed(items)


def test_kpi_gate_reconcile_unknown_blocked() -> None:
    """unknown 不允许发布（spec §7.5 ④）。"""
    items = buildKpiGateItems(
        metricId=1,
        metricStatus="PUBLISHED",
        executionSource="COMPILED",
        datasourceId=7,
        reconcileVerdict="unknown",
        reconcileDetail="治理侧未填结构化列",
    )
    assert "reconcile" in _failed(items)


def test_kpi_gate_reconcile_mismatch_blocked() -> None:
    items = buildKpiGateItems(
        metricId=1,
        metricStatus="PUBLISHED",
        executionSource="COMPILED",
        datasourceId=7,
        reconcileVerdict="mismatch",
        reconcileDetail="粒度键不一致",
    )
    assert "reconcile" in _failed(items)


def test_kpi_gate_missing_datasource_blocked() -> None:
    items = buildKpiGateItems(
        metricId=1,
        metricStatus="PUBLISHED",
        executionSource="COMPILED",
        datasourceId=None,
        reconcileVerdict="pass",
        reconcileDetail="",
    )
    assert "datasource" in _failed(items)


def test_kpi_gate_reports_every_missing_item() -> None:
    """一次报全所有缺项（spec：409 + 明确缺哪一项）。"""
    items = buildKpiGateItems(
        metricId=None,
        metricStatus=None,
        executionSource="NONE",
        datasourceId=None,
        reconcileVerdict="unknown",
        reconcileDetail="",
    )
    failed = set(_failed(items))
    assert failed == {
        "metric_linked",
        "metric_published",
        "execution_source",
        "reconcile",
        "datasource",
    }


def test_kpi_gate_mismatch_message_carries_detail() -> None:
    items = buildKpiGateItems(
        metricId=1,
        metricStatus="PUBLISHED",
        executionSource="COMPILED",
        datasourceId=7,
        reconcileVerdict="mismatch",
        reconcileDetail="粒度键不一致",
    )
    reconcile = next(i for i in items if i.code == "reconcile")
    assert "粒度键不一致" in reconcile.message


def test_kpi_gate_unknown_message_renders_actual_cause() -> None:
    """unknown 分支按真实成因渲染（不是「治理侧未填结构化列」静态文案）。"""
    items = buildKpiGateItems(
        metricId=1,
        metricStatus="PUBLISHED",
        executionSource="COMPILED",
        datasourceId=7,
        reconcileVerdict="unknown",
        reconcileDetail="[治理层] 治理侧未声明结构化粒度键",
    )
    reconcile = next(i for i in items if i.code == "reconcile")
    assert reconcile.ok is False
    assert "治理侧未声明结构化粒度键" in reconcile.message


# ── 对账 findings 渲染（FIX-2）───────────────────────────────────────


def test_render_reconcile_findings_uses_chinese_layer_and_detail() -> None:
    findings = (
        ReconcileFinding(
            layer=LAYER_GOVERNANCE,
            reason="governance_columns_absent",
            detail="治理侧未声明分子列",
            verdict=VERDICT_UNKNOWN,
        ),
    )
    text = renderReconcileFindings(findings)
    assert text == "[治理层] 治理侧未声明分子列"
    assert "governance_columns_absent" not in text


def test_render_reconcile_findings_empty_is_explicit() -> None:
    """空 findings 返回明确兜底文案，不返回空串（否则正文出现「：」裸尾）。"""
    text = renderReconcileFindings(())
    assert text.strip() != ""


def test_render_reconcile_findings_keeps_unknown_layer_readable() -> None:
    """未知 layer 值原样展示（不静默丢层次信息）。"""
    findings = (ReconcileFinding("future_layer", "x", "明细", VERDICT_UNKNOWN),)
    text = renderReconcileFindings(findings)
    assert "[future_layer] 明细" in text


# ── 本体指标方向 ─────────────────────────────────────────────────────


def _metricItems(**overrides):
    base = dict(
        metricType="MEASURE",
        numeratorMeasure={"agg": "SUM", "expr": "QTY"},
        denominatorMeasure=None,
        aggregationOrder=None,
        grainKeys=("plant_code",),
        executionSource="COMPILED",
        degraded=False,
        linkedMismatch=(),
    )
    base.update(overrides)
    return buildMetricGateItems(**base)


def test_metric_gate_all_ok_passes() -> None:
    assert _failed(_metricItems()) == []


def test_metric_gate_requires_numerator() -> None:
    assert "numerator" in _failed(_metricItems(numeratorMeasure=None))


def test_metric_gate_ratio_requires_denominator() -> None:
    items = _metricItems(metricType="RATIO", aggregationOrder="WEIGHTED_RATIO")
    assert "denominator" in _failed(items)


def test_metric_gate_ratio_requires_aggregation_order() -> None:
    items = _metricItems(
        metricType="RATIO",
        denominatorMeasure={"agg": "SUM", "expr": "AMOUNT"},
        aggregationOrder=None,
    )
    assert "aggregation_order" in _failed(items)


def test_metric_gate_ratio_requires_valid_aggregation_order() -> None:
    items = _metricItems(
        metricType="RATIO",
        denominatorMeasure={"agg": "SUM", "expr": "AMOUNT"},
        aggregationOrder="NONE",
    )
    assert "aggregation_order" in _failed(items)


def test_metric_gate_difference_allows_none_aggregation_order() -> None:
    """DIFFERENCE 没有辛普森悖论 ⇒ 允许 aggregation_order=NONE（对齐 _checkRatio）。"""
    items = _metricItems(
        metricType="DIFFERENCE",
        denominatorMeasure={"agg": "SUM", "expr": "AMOUNT"},
        aggregationOrder="NONE",
    )
    assert _failed(items) == []
    assert "aggregation_order" not in _codes(items)


def test_metric_gate_difference_requires_denominator() -> None:
    """DIFFERENCE 仍需要分母度量（语义是减数）。"""
    items = _metricItems(metricType="DIFFERENCE", aggregationOrder="NONE")
    assert "denominator" in _failed(items)


def test_metric_gate_requires_grain_keys() -> None:
    assert "grain" in _failed(_metricItems(grainKeys=()))


def test_metric_gate_requires_metric_type() -> None:
    assert "metric_type" in _failed(_metricItems(metricType=None))


def test_metric_gate_blocks_none_execution_source() -> None:
    assert "execution_source" in _failed(_metricItems(executionSource="NONE"))


def test_metric_gate_blocks_degraded_technique() -> None:
    assert "degraded" in _failed(_metricItems(degraded=True))


def test_metric_gate_blocks_linked_kpi_mismatch() -> None:
    items = _metricItems(linkedMismatch=("KPI_X: 粒度键不一致",))
    assert "linked_mismatch" in _failed(items)


def test_metric_gate_reports_every_missing_item() -> None:
    failed = set(_failed(_metricItems(
        metricType=None,
        numeratorMeasure=None,
        grainKeys=(),
        executionSource="NONE",
        degraded=True,
        linkedMismatch=("KPI_X: x",),
    )))
    assert failed == {
        "metric_type",
        "numerator",
        "execution_source",
        "grain",
        "degraded",
        "linked_mismatch",
    }


# ── GateResult ───────────────────────────────────────────────────────


def test_gate_result_passed_when_all_items_ok() -> None:
    result = GateResult(
        entityType="kpi_catalog",
        entityId=1,
        items=(GateItem("a", "msg", True),),
    )
    assert result.passed is True
    assert result.failedItems == ()


def test_gate_result_failed_items_and_message() -> None:
    result = GateResult(
        entityType="kpi_catalog",
        entityId=7,
        items=(
            GateItem("a", "缺 A", False),
            GateItem("b", "缺 B", True),
            GateItem("c", "缺 C", False),
        ),
    )
    assert result.passed is False
    assert [i.code for i in result.failedItems] == ["a", "c"]
    message = result.message()
    assert "缺 A" in message
    assert "缺 B" not in message
    assert "缺 C" in message


def test_gate_result_entity_id_may_be_none() -> None:
    """createKpi 路径上闸门在 flush 之前触发 ⇒ entity.id 尚未分配。

    用**非空且含失败项**的 items 断言：空 items 恒通过（``all(()) is True``），
    没有区分度。
    """
    failed = GateItem("metric_linked", "未挂靠本体指标", False)
    result = GateResult(entityType="kpi_catalog", entityId=None, items=(failed,))
    assert result.entityId is None
    assert result.passed is False
    assert result.failedItems == (failed,)
    assert "未挂靠本体指标" in result.message()
