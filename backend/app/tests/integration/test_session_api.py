"""会话 Token 用量接口集成测试。

验证 HTTP 契约（camelCase JSON）：
- GET /api/v1/sessions/usage/global   全局用量摘要
- GET /api/v1/sessions/usage/daily    按天趋势
- GET /api/v1/sessions/usage/by-model 按模型汇总
- GET /api/v1/sessions                会话列表（保留既有契约回归）
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.domain.models import LlmConfig, SessionTokenUsage


async def _seedConfig(dbSession) -> LlmConfig:
    config = LlmConfig(
        model_name="gpt-4o",
        provider="openai",
        cost_per_1k_input=Decimal("0.03"),
        cost_per_1k_output=Decimal("0.06"),
        max_input_tokens=8000,
        weight=10,
        cost_threshold=Decimal("0.05"),
        is_active=True,
    )
    dbSession.add(config)
    await dbSession.commit()
    await dbSession.refresh(config)
    return config


async def _seedUsage(dbSession, *, sessionId: str, modelName: str, tokens: int, cost: str) -> None:
    """插入一行 Token 消耗流水（request_time 用当前 UTC 时间）。"""
    dbSession.add(
        SessionTokenUsage(
            session_id=sessionId,
            model_config_id=None,
            model_name=modelName,
            prompt_tokens=tokens // 2,
            completion_tokens=tokens - tokens // 2,
            total_tokens=tokens,
            cost=Decimal(cost),
            request_time=datetime.now(UTC),
        )
    )
    await dbSession.commit()


class TestSessionUsageApi:
    async def test_global_summary_returns_totals(self, client, dbSession) -> None:
        # Arrange
        config = await _seedConfig(dbSession)
        await _seedUsage(dbSession, sessionId="s1", modelName="gpt-4o", tokens=150, cost="0.006")
        await _seedUsage(dbSession, sessionId="s1", modelName="gpt-4o", tokens=30, cost="0.002")
        await _seedUsage(dbSession, sessionId="s2", modelName="ollama", tokens=7, cost="0.001")
        assert config.id is not None
        # Act
        resp = await client.get("/api/v1/sessions/usage/global")
        # Assert
        assert resp.status_code == 200
        body = resp.json()
        assert body["totalSessions"] == 2
        assert body["totalRequests"] == 3
        assert body["totalTokens"] == 187
        assert Decimal(body["totalCost"]) == Decimal("0.009")

    async def test_global_summary_empty(self, client, dbSession) -> None:
        resp = await client.get("/api/v1/sessions/usage/global")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "totalSessions": 0,
            "totalRequests": 0,
            "totalTokens": 0,
            "totalCost": "0",
        }

    async def test_daily_trend_returns_camel_case_rows(self, client, dbSession) -> None:
        # Arrange
        await _seedConfig(dbSession)
        await _seedUsage(dbSession, sessionId="s1", modelName="gpt-4o", tokens=150, cost="0.006")
        # Act
        resp = await client.get("/api/v1/sessions/usage/daily")
        # Assert
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert set(body[0]) == {"date", "requests", "tokens", "cost"}
        assert body[0]["requests"] == 1
        assert body[0]["tokens"] == 150
        assert Decimal(body[0]["cost"]) == Decimal("0.006")

    async def test_model_usage_groups_by_model(self, client, dbSession) -> None:
        # Arrange
        await _seedConfig(dbSession)
        await _seedUsage(dbSession, sessionId="s1", modelName="gpt-4o", tokens=150, cost="0.006")
        await _seedUsage(dbSession, sessionId="s2", modelName="ollama", tokens=7, cost="0.001")
        # Act
        resp = await client.get("/api/v1/sessions/usage/by-model")
        # Assert
        assert resp.status_code == 200
        body = resp.json()
        byName = {row["modelName"]: row for row in body}
        assert set(byName) == {"gpt-4o", "ollama"}
        assert byName["gpt-4o"]["requests"] == 1
        assert byName["gpt-4o"]["tokens"] == 150

    async def test_list_sessions_preserves_camel_case_contract(self, client, dbSession) -> None:
        # Arrange
        await _seedConfig(dbSession)
        await _seedUsage(dbSession, sessionId="s1", modelName="gpt-4o", tokens=150, cost="0.006")
        # Act
        resp = await client.get("/api/v1/sessions")
        # Assert
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["sessionId"] == "s1"
        assert body[0]["totalRequests"] == 1
        assert body[0]["totalTokens"] == 150
