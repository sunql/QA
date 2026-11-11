"""ChatResponse Tier 观测字段：默认 None + camelCase 序列化（D5 Task 5.1）。"""

from app.domain.schemas import ChatResponse


def test_tier_fields_default_none() -> None:
    r = ChatResponse(answer="a", intent="l1_match")
    assert r.execution_tier is None and r.degraded_reason is None


def test_tier_fields_serialize_camel_case() -> None:
    r = ChatResponse(
        answer="a", intent="l1_match",
        execution_tier="B", degraded_reason="explicit_time_scope",
    )
    payload = r.model_dump(by_alias=True, mode="json")
    assert payload["executionTier"] == "B"
    assert payload["degradedReason"] == "explicit_time_scope"
