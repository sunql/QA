from dataclasses import FrozenInstanceError

import pytest

from app.services.kpi_execution_service import (
    KpiExecutionPlan,
    planKpiExecution,
)

_ALL_OK = dict(
    switch_on=True,
    kpi_found=True,
    metric_present=True,
    execution_source="ESCAPE_HATCH",
    tier_a_eligible=True,
    has_datasource=True,
    formula_present=True,
    has_explicit_time_scope=False,
)


def _plan(**overrides) -> KpiExecutionPlan:
    return planKpiExecution(**{**_ALL_OK, **overrides})


@pytest.mark.parametrize(
    ("overrides", "tier", "reason"),
    [
        ({"switch_on": False}, "LEGACY", "tier_a_disabled"),
        ({"switch_on": False, "execution_source": "NONE"}, "LEGACY", "tier_a_disabled"),
        ({"kpi_found": False}, "LEGACY", "kpi_not_found"),
        ({"metric_present": False}, "C", "metric_missing"),
        ({"execution_source": None}, "C", "execution_source_missing"),
        ({"execution_source": "  "}, "C", "execution_source_missing"),
        ({"execution_source": "NONE"}, "C", "execution_source_none"),
        ({"execution_source": "none"}, "C", "execution_source_none"),
        ({"execution_source": "COMPILED"}, "A", None),
        ({"execution_source": "COMPILED", "has_datasource": False}, "C", "datasource_missing"),
        ({"execution_source": "COMPILED", "tier_a_eligible": False}, "B", "reconcile_not_eligible"),
        ({"execution_source": "WEIRD"}, "C", "execution_source_unknown"),
        ({"has_datasource": False}, "C", "datasource_missing"),
        ({"has_explicit_time_scope": True}, "B", "explicit_time_scope"),
        ({"tier_a_eligible": False}, "B", "reconcile_not_eligible"),
        ({"formula_present": False}, "B", "formula_empty"),
        ({}, "A", None),
    ],
)
def test_plan_matrix(overrides, tier, reason) -> None:
    plan = _plan(**overrides)
    assert plan.tier == tier
    assert plan.reason == reason


def test_plan_is_frozen() -> None:
    plan = _plan()
    with pytest.raises(FrozenInstanceError):
        plan.tier = "B"  # type: ignore[misc]
