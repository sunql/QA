"""parse-description LLM 集成测试（mock LLM client）。
真实 PG（qa_metadata_test）+ 完整 API 链路。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.exceptions import LLMUnavailableError
from app.domain.schemas import FeatureRuleParseDescriptionRequest
from app.services.feature_rule_llm_service import parseFeatureRuleDescription

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


def _makeFakeResponse(content: str) -> MagicMock:
    """返回一个 sync MagicMock，其 .complete() 返回 AsyncMock(真实 content)。

    用普通类实例作 response，避免 MagicMock 对 .content 的自动生成。
    """
    class _FakeResponse:
        def __init__(self, text: str) -> None:
            self.content = text

    mock_complete = AsyncMock(return_value=_FakeResponse(content))
    mock_client = MagicMock()
    mock_client.complete = mock_complete
    return mock_client


class TestParseDescriptionHappyPath:
    """直接调用 service 函数的单元级集成测试（mock LLM）。"""

    async def test_parse_description_returns_suggestions(self, client, dbSession) -> None:
        """seed feature_definition（warmAgentCaches autouse 已 seed）。"""
        fake_response = _makeFakeResponse('''
        {
          "suggested_thresholds": [
            {"feature_name": "SUPPLIER_OTD_3M", "severity": "HIGH", "operator": "lt",
             "threshold_value": 90, "unit": "%", "confidence": 0.9, "rationale": "OTD < 90%"}
          ],
          "reasoning": "识别出 1 条规则建议",
          "overall_confidence": 0.9,
          "warnings": []
        }
        ''')

        payload = FeatureRuleParseDescriptionRequest(
            data_object="SUPPLIER",
            data_layer="FEATURE",
            target_level="RISK",
            natural_language="OTD 低于 90% 即高风险",
        )
        result = await parseFeatureRuleDescription(
            session=dbSession,
            payload=payload,
            llm_client=fake_response,
            actor="test-admin",
        )
        assert len(result.suggested_thresholds) == 1
        assert result.suggested_thresholds[0].feature_name == "SUPPLIER_OTD_3M"
        assert result.suggested_thresholds[0].severity.value == "HIGH"
        assert result.overall_confidence == 0.9


class TestParseDescriptionLLMUnavailable:
    """service 函数异常路径测试。"""

    async def test_llm_unavailable_raises_503(self, client, dbSession) -> None:
        """LLM 调用失败 → LLMUnavailableError → 503。"""
        fake_client = _makeFakeResponse("{}")
        fake_client.complete.side_effect = RuntimeError("LLM down")

        payload = FeatureRuleParseDescriptionRequest(
            data_object="SUPPLIER",
            data_layer="FEATURE",
            target_level="RISK",
            natural_language="OTD 低于 90% 即高风险，价格偏差超 15% 是中等风险",
        )
        with pytest.raises(LLMUnavailableError):
            await parseFeatureRuleDescription(
                session=dbSession,
                payload=payload,
                llm_client=fake_client,
                actor="test-admin",
            )


class TestParseDescriptionEndpoint:
    """HTTP 端点测试（patch createClient）。"""

    async def test_post_parse_description_returns_suggestions(self, client) -> None:
        """POST /api/v1/feature-rules/parse-description → 200 + suggestions。"""
        fake_response = _makeFakeResponse('''
        {
          "suggested_thresholds": [
            {"feature_name": "SUPPLIER_OTD_3M", "severity": "HIGH", "operator": "lt",
             "threshold_value": 90, "unit": "%", "confidence": 0.85, "rationale": "OTD below 90"}
          ],
          "reasoning": "Parsed 1 rule",
          "overall_confidence": 0.85,
          "warnings": []
        }
        ''')

        with patch(
            "app.api.v1.feature_rules.createClient",
            return_value=fake_response,
        ):
            r = await client.post(
                "/api/v1/feature-rules/parse-description",
                json={
                    "data_object": "SUPPLIER",
                    "data_layer": "FEATURE",
                    "target_level": "RISK",
                    "natural_language": "OTD 低于 90% 即高风险",
                },
                headers=ADMIN_HEADERS,
            )

        assert r.status_code == 200
        body = r.json()
        assert len(body["suggestedThresholds"]) == 1
        assert body["suggestedThresholds"][0]["featureName"] == "SUPPLIER_OTD_3M"
        assert body["overallConfidence"] == 0.85

    async def test_post_parse_description_non_admin_403(self, client) -> None:
        """非 admin → 403。"""
        fake_response = _makeFakeResponse(
            '{"suggested_thresholds": [], "reasoning": "", "overall_confidence": 0, "warnings": []}'
        )

        with patch(
            "app.api.v1.feature_rules.createClient",
            return_value=fake_response,
        ):
            r = await client.post(
                "/api/v1/feature-rules/parse-description",
                json={
                    "data_object": "SUPPLIER",
                    "data_layer": "FEATURE",
                    "target_level": "RISK",
                    "natural_language": "OTD 低于 90% 即高风险",
                },
                headers={"X-User-Id": "user1", "X-User-Roles": "user"},
            )

        assert r.status_code == 403
