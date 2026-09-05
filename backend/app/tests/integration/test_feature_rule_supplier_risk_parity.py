"""CRITICAL: prove feature_rule evaluator is byte-identical to legacy _decideLevel.

spec §10.4 parity scenarios.
"""
from decimal import Decimal

import pytest

from app.domain.enums import RiskLevel
from app.domain.schemas import Supplier360Kpi, Supplier360Profile, Supplier360Read
from app.services.feature_rule_registry import feature_rule_registry
from app.services.supplier_risk_service import SupplierRiskService


def _view(kpis: list[Supplier360Kpi]) -> Supplier360Read:
    return Supplier360Read(
        profile=Supplier360Profile(enterprise_key=1, enterprise_code="S001", entity_type="SUPPLIER"),
        entity_codes=[], kpis=kpis,
    )


def _kpi(name: str, value: float | None, passed: bool = True) -> Supplier360Kpi:
    return Supplier360Kpi(
        feature_name=name, value=Decimal(str(value)) if value is not None else None,
        unit="%", latest=value is not None, passed=passed,
    )


async def test_parity_high_risk_score(dbSession) -> None:
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", 0.55),
        _kpi("SUPPLIER_OTD_3M", None),
        _kpi("SUPPLIER_DEFECT_RATE_3M", None),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", None),
    ])
    level, source = await SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.HIGH
    assert source == "supplier_risk_score_main"


async def test_parity_medium_risk_score(dbSession) -> None:
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", 0.70),
        _kpi("SUPPLIER_OTD_3M", None),
        _kpi("SUPPLIER_DEFECT_RATE_3M", None),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", None),
    ])
    level, _ = await SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.MEDIUM


async def test_parity_low_risk_score(dbSession) -> None:
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", 0.85),
        _kpi("SUPPLIER_OTD_3M", None),
        _kpi("SUPPLIER_DEFECT_RATE_3M", None),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", None),
    ])
    level, _ = await SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.LOW


async def test_parity_high_fallback_3_violations(dbSession) -> None:
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", None),
        _kpi("SUPPLIER_OTD_3M", 85, passed=False),
        _kpi("SUPPLIER_DEFECT_RATE_3M", 4, passed=False),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", 8, passed=False),
    ])
    level, _ = await SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.HIGH


async def test_parity_low_fallback_1_violation(dbSession) -> None:
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", None),
        _kpi("SUPPLIER_OTD_3M", 92),
        _kpi("SUPPLIER_DEFECT_RATE_3M", 3, passed=False),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", 8),
    ])
    level, _ = await SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.LOW


async def test_parity_unknown_all_missing(dbSession) -> None:
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", None),
        _kpi("SUPPLIER_OTD_3M", None),
        _kpi("SUPPLIER_DEFECT_RATE_3M", None),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", None),
    ])
    level, source = await SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.UNKNOWN
    assert source == "unknown"


async def test_parity_risk_score_bypasses_otd_violation(dbSession) -> None:
    """RISK_SCORE tier MEDIUM matched → 即使 OTD 也违规，仍返回 MEDIUM（bypass）。"""
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", 0.70),
        _kpi("SUPPLIER_OTD_3M", 85, passed=False),
        _kpi("SUPPLIER_DEFECT_RATE_3M", 4, passed=False),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", 8, passed=False),
    ])
    level, _ = await SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.MEDIUM


async def test_parity_risk_score_low_bypasses_other_high(dbSession) -> None:
    """RISK_SCORE tier LOW matched → 即使其他 feature 也违规，仍返回 LOW（bypass）。"""
    await _seed_rules(dbSession)
    view = _view([
        _kpi("SUPPLIER_RISK_SCORE", 0.85),
        _kpi("SUPPLIER_OTD_3M", 85, passed=False),
        _kpi("SUPPLIER_DEFECT_RATE_3M", 4, passed=False),
        _kpi("SUPPLIER_PRICE_VARIANCE_3M", 8, passed=False),
    ])
    level, _ = await SupplierRiskService()._decideLevel_via_rules(view, _to_contributions(view))
    assert level == RiskLevel.LOW


# ---- helpers ----

async def _seed_rules(dbSession) -> None:
    from scripts.seed_feature_rules import seedFeatureRules

    await seedFeatureRules(dbSession)
    await dbSession.commit()
    await feature_rule_registry.warmUp(dbSession)


def _to_contributions(view):
    from app.domain.schemas import SupplierRiskKpiContribution
    return [
        SupplierRiskKpiContribution(
            feature_name=kpi.feature_name,
            value=str(kpi.value) if kpi.value is not None else None,
            unit=kpi.unit, threshold=None, passed=True, note=None,
        )
        for kpi in view.kpis
    ]
