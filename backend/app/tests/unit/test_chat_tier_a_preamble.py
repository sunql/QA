"""Tier A/B/C/LEGACY 四分支的 preamble 选择；Tier B 绝不允许落 L4。"""

from __future__ import annotations

import pytest

from app.domain.models import KpiCatalog, OntologyMetric
from app.domain.schemas import ChatRequest
from app.services.chat_stream_output import L1L4PreambleMixin
from app.services.kpi_semantic_match_service import KpiMatchResult, KpiMatchRoute


class _Kpi:
    kpi_code = "KPI_SUPPLIER_OTD"
    kpi_name = "供应商准时交付率"
    semantic_keywords = ["准时交付"]
    metric_id = 5
    datasource_id = 1
    business_definition = "按承诺日期交付"
    unit = "%"


class _Matcher:
    """v2 路径桩：matchRoute 恒返回唯一命中。"""

    async def matchRoute(self, _q: str) -> KpiMatchRoute:
        return KpiMatchRoute(
            kind="hit",
            matches=(
                KpiMatchResult(
                    code="KPI_SUPPLIER_OTD",
                    kpi_name="供应商准时交付率",
                    confidence=0.95,
                    method="containment",
                ),
            ),
        )


class _Preamble(L1L4PreambleMixin):
    def __init__(self, *, tierPlan, executeResult=None, executeReason=None) -> None:
        self._kpiMatcher = _Matcher()
        self.plan = tierPlan
        self.executeResult = executeResult
        self.executeReason = executeReason
        self.l4Called = False
        self.tierACalled = False
        self.legacyCalled = False

    async def _containmentEnabled(self, session):  # noqa: ANN001
        return True

    async def _ensureKpiCacheFresh(self, session):  # noqa: ANN001
        return None

    async def _resolveKpiCatalog(self, code, session):  # noqa: ANN001
        return _Kpi()

    async def _planKpiTier(self, question, kpi, session):  # noqa: ANN001
        return self.plan

    async def _executeTierA(self, match, kpi, session):  # noqa: ANN001
        self.tierACalled = True
        return self.executeResult, self.executeReason

    async def _buildL1Response(self, match, session):  # noqa: ANN001
        self.legacyCalled = True
        return type("R", (), {
            "answer": "legacy", "chartType": None, "chartOption": None,
            "visual_rationale": None, "kpi_name": "n",
        })()

    async def _handleNl2SqlAgent(self, session, dto, user):  # noqa: ANN001
        self.l4Called = True
        return None


def _dto() -> ChatRequest:
    return ChatRequest(question="供应商准时交付率", sessionId="s", datasourceId=1)


@pytest.mark.asyncio()
async def test_tier_a_builds_l1_response_and_skips_l4() -> None:
    response = type("R", (), {
        "answer": "tier-a", "chartType": None, "chartOption": None,
        "visual_rationale": None, "kpi_name": "供应商准时交付率",
    })()
    mixin = _Preamble(
        tierPlan=type("P", (), {"tier": "A", "reason": None})(),
        executeResult=response,
    )
    preamble = await mixin._runL1AndL4Preamble(_dto(), None, None)
    assert preamble is not None and preamble.routing_layer == "L1"
    assert mixin.tierACalled is True and mixin.l4Called is False
    assert mixin.legacyCalled is False


@pytest.mark.asyncio()
async def test_tier_b_returns_sentinel_and_skips_l4() -> None:
    mixin = _Preamble(
        tierPlan=type("P", (), {"tier": "B", "reason": "explicit_time_scope"})()
    )
    preamble = await mixin._runL1AndL4Preamble(_dto(), None, None)
    assert preamble is not None
    assert preamble.routing_layer == "L1B"
    assert preamble.degraded_reason == "explicit_time_scope"
    assert mixin.l4Called is False  # L4 不注入契约块，必须跳过
    assert mixin.legacyCalled is False


@pytest.mark.asyncio()
async def test_tier_c_returns_l1_without_execution() -> None:
    mixin = _Preamble(
        tierPlan=type("P", (), {"tier": "C", "reason": "execution_source_none"})()
    )
    preamble = await mixin._runL1AndL4Preamble(_dto(), None, None)
    assert preamble is not None and preamble.routing_layer == "L1"
    assert preamble.execution_tier == "C"
    assert mixin.tierACalled is False and mixin.l4Called is False


@pytest.mark.asyncio()
async def test_legacy_calls_build_l1_response() -> None:
    mixin = _Preamble(
        tierPlan=type("P", (), {"tier": "LEGACY", "reason": "tier_a_disabled"})()
    )
    preamble = await mixin._runL1AndL4Preamble(_dto(), None, None)
    assert preamble is not None and preamble.routing_layer == "L1"
    assert preamble.execution_tier == "LEGACY"
    assert mixin.legacyCalled is True


@pytest.mark.asyncio()
async def test_tier_a_execution_failure_degrades_to_b() -> None:
    mixin = _Preamble(
        tierPlan=type("P", (), {"tier": "A", "reason": None})(),
        executeResult=None,
        executeReason="execution_error",
    )
    preamble = await mixin._runL1AndL4Preamble(_dto(), None, None)
    assert preamble is not None and preamble.routing_layer == "L1B"
    assert preamble.degraded_reason == "execution_error"
    assert mixin.l4Called is False


# ===========================================================================
# 真 `_planKpiTier` 分支覆盖（上面的 _Preamble 覆写了它，测不到真实现）
# ===========================================================================


class _MetricSession:
    """最小 session 桩：只实现 `get(model, pk)`。"""

    def __init__(self, metric: OntologyMetric | None) -> None:
        self._metric = metric

    async def get(self, model, pk):  # noqa: ANN001
        return self._metric


class _PlanProbe(L1L4PreambleMixin):
    """只覆写开关读取，其余（`_planKpiTier`）走 mixin 真实现。"""

    def __init__(self, *, enabled: bool | None = None) -> None:
        self._enabled = enabled

    async def _tierAEnabled(self, session) -> bool:  # noqa: ANN001
        if self._enabled is None:
            return await super()._tierAEnabled(session)
        return self._enabled


def _kpi(*, metric_id: int | None = 5, datasource_id: int | None = 1) -> KpiCatalog:
    return KpiCatalog(
        kpi_code="KPI_SUPPLIER_OTD",
        kpi_name="供应商准时交付率",
        business_definition="按承诺日期交付",
        metric_id=metric_id,
        datasource_id=datasource_id,
        numerator_columns=["qty"],
        grain_keys=["supplier_id"],
    )


def _metric(execution_source: str) -> OntologyMetric:
    """治理/技术可对账通过的一对；execution_source 决定路由分支。"""
    return OntologyMetric(
        metric_name="供应商准时交付率",
        formula="SELECT SUM(qty) FROM delivery GROUP BY supplier_id",
        numerator_measure={"agg": "SUM", "expr": "qty"},
        grain_keys=["supplier_id"],
        execution_source=execution_source,
    )


@pytest.mark.asyncio()
async def test_real_plan_tier_a_disabled_when_session_none() -> None:
    probe = _PlanProbe()  # 不覆写开关：session=None ⇒ env/DB 双闸读不到 ⇒ 关
    plan = await probe._planKpiTier("供应商准时交付率", object(), None)
    assert plan.tier == "LEGACY"
    assert plan.reason == "tier_a_disabled"


@pytest.mark.asyncio()
async def test_real_plan_kpi_missing_is_legacy() -> None:
    probe = _PlanProbe(enabled=True)
    plan = await probe._planKpiTier("供应商准时交付率", None, None)
    assert plan.tier == "LEGACY"
    assert plan.reason == "kpi_not_found"


@pytest.mark.asyncio()
async def test_real_plan_metric_row_absent_is_tier_c_metric_missing() -> None:
    probe = _PlanProbe(enabled=True)
    plan = await probe._planKpiTier(
        "供应商准时交付率", _kpi(), _MetricSession(None)
    )
    assert plan.tier == "C"
    assert plan.reason == "metric_missing"


@pytest.mark.asyncio()
async def test_real_plan_execution_source_none_is_tier_c() -> None:
    probe = _PlanProbe(enabled=True)
    plan = await probe._planKpiTier(
        "供应商准时交付率", _kpi(), _MetricSession(_metric("NONE"))
    )
    assert plan.tier == "C"
    assert plan.reason == "execution_source_none"


@pytest.mark.asyncio()
async def test_real_plan_explicit_time_scope_is_tier_b() -> None:
    probe = _PlanProbe(enabled=True)
    plan = await probe._planKpiTier(
        "近6个月供应商准时交付率", _kpi(), _MetricSession(_metric("ESCAPE_HATCH"))
    )
    assert plan.tier == "B"
    assert plan.reason == "explicit_time_scope"


@pytest.mark.asyncio()
async def test_real_plan_reconciled_escape_hatch_is_tier_a() -> None:
    probe = _PlanProbe(enabled=True)
    plan = await probe._planKpiTier(
        "供应商准时交付率是多少", _kpi(), _MetricSession(_metric("ESCAPE_HATCH"))
    )
    assert plan.tier == "A"
    assert plan.reason is None
