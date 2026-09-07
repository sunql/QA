"""FeatureRuleService 单元测试。"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.exceptions import (
    FeatureRuleNotFoundError,
    FeatureRuleReferencingError,
    FeatureRuleVersionConflictError,
)
from app.domain.models import FeatureRule, FeatureRuleThreshold
from app.domain.schemas import (
    FeatureRuleCreate,
    FeatureRuleThresholdCreate,
    FeatureRuleUpdate,
)
from app.services.feature_rule_service import FeatureRuleService


def _row(**kw) -> SimpleNamespace:
    base = dict(
        id=1, code="r1", data_object="SUPPLIER", data_layer="FEATURE",
        target_level="RISK", feature_name="F1", enabled=True, priority=100,
        policy_description=None, version=1, created_time=datetime.now(timezone.utc),
        updated_time=None, thresholds=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def mockSession() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.rollback = AsyncMock()
    return session


async def test_list_rules_returns_rows(mockSession: AsyncMock) -> None:
    expected = [_row(code="a"), _row(code="b")]
    mockSession.execute.return_value = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=expected))))
    rows = await FeatureRuleService().listRules(mockSession)
    assert len(rows) == 2


async def test_get_rule_not_found_raises(mockSession: AsyncMock) -> None:
    mockSession.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    with pytest.raises(FeatureRuleNotFoundError):
        await FeatureRuleService().getRule(mockSession, "missing")


async def test_create_rule_dup_code_raises_conflict(mockSession: AsyncMock) -> None:
    mockSession.flush.side_effect = IntegrityError("stmt", {}, Exception("uq_feature_rule_scope_code"))
    dto = FeatureRuleCreate(
        code="r1", data_object="SUPPLIER", data_layer="FEATURE",
        target_level="RISK", feature_name="F1",
        thresholds=[FeatureRuleThresholdCreate(severity="HIGH", operator="lt", threshold_value=10)],
    )
    with pytest.raises(Exception):  # ConflictError (parent class)
        await FeatureRuleService().createRule(mockSession, dto, SimpleNamespace(userId="u1"))


async def test_update_rule_version_mismatch_raises(mockSession: AsyncMock) -> None:
    existing = _row(version=5)
    mockSession.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
    dto = FeatureRuleUpdate(version=3, enabled=False)
    with pytest.raises(FeatureRuleVersionConflictError) as exc_info:
        await FeatureRuleService().updateRule(mockSession, "r1", dto, SimpleNamespace(userId="u1"))
    assert exc_info.value.current_version == 5


async def test_delete_rule_referenced_raises(mockSession: AsyncMock) -> None:
    existing = _row()
    # getRule returns existing
    mockSession.execute.side_effect = [
        MagicMock(scalar_one_or_none=MagicMock(return_value=existing)),
        # referencing query
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=["AgentA"])))),
    ]
    with pytest.raises(FeatureRuleReferencingError) as exc_info:
        await FeatureRuleService().deleteRule(mockSession, "r1", SimpleNamespace(userId="u1"))
    assert exc_info.value.referencing == ["AgentA"]
