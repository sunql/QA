"""Feature Rule DTO + exception 单元测试（spec §9.1 + §9.2）。"""
from datetime import datetime

import pytest
from pydantic import ValidationError as PydValidationError

from app.domain.exceptions import (
    FeatureRuleNotFoundError,
    FeatureRuleReferencingError,
    FeatureRuleValidationError,
    FeatureRuleVersionConflictError,
    LLMUnavailableError,
)
from app.domain.schemas import (
    FeatureRuleCreate,
    FeatureRuleThresholdCreate,
    FeatureRuleUpdate,
)


def test_threshold_create_decimal_required() -> None:
    with pytest.raises(PydValidationError):
        FeatureRuleThresholdCreate(severity="HIGH", operator="lt", threshold_value=None)


def test_feature_rule_create_requires_thresholds() -> None:
    with pytest.raises(PydValidationError):
        FeatureRuleCreate(
            code="r1", data_object="SUPPLIER", data_layer="FEATURE",
            target_level="RISK", feature_name="X", thresholds=[],
        )


def test_feature_rule_create_severity_unique_within_thresholds() -> None:
    with pytest.raises(PydValidationError):
        FeatureRuleCreate(
            code="r1", data_object="SUPPLIER", data_layer="FEATURE",
            target_level="RISK", feature_name="X",
            thresholds=[
                FeatureRuleThresholdCreate(severity="HIGH", operator="lt", threshold_value=10),
                FeatureRuleThresholdCreate(severity="HIGH", operator="lt", threshold_value=20),
            ],
        )


def test_feature_rule_update_requires_version() -> None:
    with pytest.raises(PydValidationError):
        FeatureRuleUpdate(enabled=False)  # type: ignore[call-arg]


def test_feature_rule_version_conflict_error_carries_current_version() -> None:
    err = FeatureRuleVersionConflictError("version mismatch", current_version=3)
    assert err.current_version == 3
    assert isinstance(err, Exception)


def test_referencing_error_carries_list() -> None:
    err = FeatureRuleReferencingError("feature_rule is referenced", referencing=["AgentA", "AgentB"])
    assert err.referencing == ["AgentA", "AgentB"]


def test_llm_unavailable_error_is_exception() -> None:
    assert isinstance(LLMUnavailableError("test"), Exception)


def test_feature_rule_not_found_error_is_exception() -> None:
    assert isinstance(FeatureRuleNotFoundError("test"), Exception)


def test_validation_error_is_exception() -> None:
    assert isinstance(FeatureRuleValidationError("test"), Exception)
