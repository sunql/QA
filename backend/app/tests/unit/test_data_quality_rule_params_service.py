"""DataQualityRuleParamsService unit tests (feat-dq-rule-params Task 6).

AsyncMock session — no real DB. Tests pure orchestration logic:
mutual exclusion, structured compile, custom passthrough, config_mode derivation.
"""
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.domain.enums import RuleType, Severity
from app.domain.schemas_dq_rule_params import DataQualityRuleParamsCreate
from app.services.data_quality_rule_params_service import (
    DataQualityRuleParamsService,
)


@pytest.fixture
def session():
    s = AsyncMock()
    s.flush = AsyncMock()

    async def _refresh(obj):
        # Mock ORM refresh: assign a deterministic id for downstream assertions
        if getattr(obj, "id", None) is None:
            obj.id = 1
    s.refresh = _refresh
    return s


@pytest.fixture
def service(session):
    return DataQualityRuleParamsService(session)


class TestCreateStructured:
    async def test_validity_range_compiles_expression(self, service, session):
        dto = DataQualityRuleParamsCreate(
            rule_code="DQ_TEST", rule_name="test", rule_type=RuleType.VALIDITY,
            target_table="PO_LINE", target_column="ORDER_QTY",
            threshold=Decimal("95"), severity=Severity.MEDIUM, datasource_id=1,
            rule_params={"kind": "range", "min": 0, "max": 100},
        )
        added: list = []
        session.add = lambda obj: added.append(obj)
        result = await service.create(dto)
        assert len(added) == 1
        rule = added[0]
        assert rule.rule_params == {"kind": "range", "min": 0, "max": 100}
        assert "BETWEEN" in rule.rule_expression
        assert "ORDER_QTY" in rule.rule_expression
        assert result.config_mode == "structured"


class TestCreateCustom:
    async def test_validity_expression_passthrough(self, service, session):
        dto = DataQualityRuleParamsCreate(
            rule_code="DQ_TEST2", rule_name="t", rule_type=RuleType.VALIDITY,
            target_table="PO_LINE", target_column="ORDER_QTY",
            threshold=Decimal("95"), severity=Severity.MEDIUM, datasource_id=1,
            rule_expression="ORDER_QTY > 0",
        )
        added: list = []
        session.add = lambda obj: added.append(obj)
        result = await service.create(dto)
        rule = added[0]
        assert rule.rule_expression == "ORDER_QTY > 0"
        assert rule.rule_params is None
        assert result.config_mode == "custom"


class TestMutex:
    def test_both_rejected_by_schema(self):
        with pytest.raises(PydanticValidationError):
            DataQualityRuleParamsCreate(
                rule_code="X", rule_name="x", rule_type=RuleType.VALIDITY,
                target_table="T", target_column="C",
                threshold=Decimal("95"), severity=Severity.LOW, datasource_id=1,
                rule_params={"kind": "range", "min": 0},
                rule_expression="C > 0",
            )

    def test_neither_rejected_by_schema(self):
        with pytest.raises(PydanticValidationError):
            DataQualityRuleParamsCreate(
                rule_code="X", rule_name="x", rule_type=RuleType.VALIDITY,
                target_table="T", target_column="C",
                threshold=Decimal("95"), severity=Severity.LOW, datasource_id=1,
            )


class TestUpdateStructured:
    async def test_params_change_recompiles_expression(self, service, session):
        from app.domain.models import DataQualityRule
        existing = DataQualityRule(
            id=1, rule_code="X", rule_name="old",
            rule_type=RuleType.VALIDITY.value, target_table="PO_LINE",
            target_column="ORDER_QTY", threshold=Decimal("95"),
            severity=Severity.MEDIUM, datasource_id=1,
            rule_expression='"ORDER_QTY" >= 0', rule_params={"kind": "compare", "op": ">=", "value": 0},
        )
        async def fake_get(_model, _id):
            return existing
        session.get = fake_get
        from app.domain.schemas_dq_rule_params import DataQualityRuleParamsUpdate
        dto = DataQualityRuleParamsUpdate(
            rule_params={"kind": "range", "min": 0, "max": 1000},
        )
        result = await service.update(1, dto)
        assert "BETWEEN" in existing.rule_expression
        assert existing.rule_params == {"kind": "range", "min": 0, "max": 1000}
        assert result.config_mode == "structured"

    async def test_expression_change_clears_params(self, service, session):
        from app.domain.models import DataQualityRule
        existing = DataQualityRule(
            id=2, rule_code="X2", rule_name="x",
            rule_type=RuleType.VALIDITY.value, target_table="PO_LINE",
            target_column="ORDER_QTY", threshold=Decimal("95"),
            severity=Severity.MEDIUM, datasource_id=1,
            rule_expression='"ORDER_QTY" BETWEEN 0 AND 100',
            rule_params={"kind": "range", "min": 0, "max": 100},
        )
        async def fake_get(_model, _id):
            return existing
        session.get = fake_get
        from app.domain.schemas_dq_rule_params import DataQualityRuleParamsUpdate
        dto = DataQualityRuleParamsUpdate(rule_expression="ORDER_QTY > 5")
        result = await service.update(2, dto)
        assert existing.rule_expression == "ORDER_QTY > 5"
        assert existing.rule_params is None
        assert result.config_mode == "custom"


class TestGetNotFound:
    async def test_raises_not_found(self, service, session):
        async def fake_get(_model, _id):
            return None
        session.get = fake_get
        from app.domain.exceptions import NotFoundError
        with pytest.raises(NotFoundError):
            await service.get(999)
