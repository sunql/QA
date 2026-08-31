"""Token Usage 服务测试（真实 PostgreSQL，验证 DB 持久化与聚合查询）。

dbSession fixture 由本目录 conftest.py 提供（真实 PG + 每测试 TRUNCATE 隔离）。

时间处理注意：session_token_usage.request_time 实际列类型为 timestamp without
time zone（模型声明 timezone=True，但迁移 0001 建表未带 tz、无后续 alter，见
alembic 漂移）。手动构造 request_time 必须用 aware UTC：naive datetime 会被
asyncpg 按本地时区解析再转 UTC（本机 UTC+8 时 08-13 00:00 会落成 08-12 16:00，
日期偏移一天）。生产路径统一走 _utcnow（aware UTC），与本文件构造方式一致。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.domain.models import LlmConfig, SessionMessage, SessionTokenUsage
from app.services.token_usage_service import TokenUsageService


async def _seedConfig(session) -> LlmConfig:
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
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return config


@pytest.mark.asyncio
class TestTokenUsageService:
    async def test_recordUsage_persists_row_and_returns_it(self, dbSession) -> None:
        # Arrange
        config = await _seedConfig(dbSession)
        svc = TokenUsageService()
        # Act
        usage = await svc.recordUsage(
            dbSession,
            sessionId="sess_1",
            modelConfigId=config.id,
            modelName="gpt-4o",
            promptTokens=100,
            completionTokens=50,
            cost=Decimal("0.006"),
            purpose="chat",
        )
        # Assert
        assert usage.id is not None
        assert usage.session_id == "sess_1"
        # DB 中确实有一行
        rows = (await dbSession.execute(select(SessionTokenUsage))).scalars().all()
        assert len(rows) == 1
        assert rows[0].total_tokens == 150

    async def test_getSessionCost_sums_costs(self, dbSession) -> None:
        # Arrange
        config = await _seedConfig(dbSession)
        svc = TokenUsageService()
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=config.id, modelName="gpt-4o", promptTokens=10, completionTokens=5, cost=Decimal("0.002"), purpose="chat")
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=config.id, modelName="gpt-4o", promptTokens=20, completionTokens=10, cost=Decimal("0.003"), purpose="chat")
        await svc.recordUsage(dbSession, sessionId="sess_2", modelConfigId=config.id, modelName="gpt-4o", promptTokens=5, completionTokens=2, cost=Decimal("0.001"), purpose="chat")
        # Act
        cost = await svc.getSessionCost(dbSession, "sess_1")
        # Assert
        assert cost == Decimal("0.005")

    async def test_getSessionTurnCount_counts_rows(self, dbSession) -> None:
        config = await _seedConfig(dbSession)
        svc = TokenUsageService()
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=config.id, modelName="gpt-4o", promptTokens=1, completionTokens=1, cost=Decimal("0"), purpose="chat")
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=config.id, modelName="gpt-4o", promptTokens=1, completionTokens=1, cost=Decimal("0"), purpose="chat")
        count = await svc.getSessionTurnCount(dbSession, "sess_1")
        assert count == 2

    async def test_getLastModelId_returns_most_recent(self, dbSession) -> None:
        config = await _seedConfig(dbSession)
        svc = TokenUsageService()
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=config.id, modelName="gpt-4o", promptTokens=1, completionTokens=1, cost=Decimal("0"), purpose="chat")
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=None, modelName="ollama", promptTokens=1, completionTokens=1, cost=Decimal("0"), purpose="chat")
        lastId = await svc.getLastModelId(dbSession, "sess_1")
        # 最后一条 modelConfigId 为 None
        assert lastId is None

    async def test_summarize_aggregates_session(self, dbSession) -> None:
        config = await _seedConfig(dbSession)
        svc = TokenUsageService()
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=config.id, modelName="gpt-4o", promptTokens=100, completionTokens=50, cost=Decimal("0.005"), purpose="chat")
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=None, modelName="llama3.1", promptTokens=20, completionTokens=10, cost=Decimal("0"), purpose="chat")
        summary = await svc.summarize(dbSession, "sess_1")
        assert summary.session_id == "sess_1"
        assert summary.total_requests == 2
        assert summary.total_tokens == 180
        assert summary.total_cost == Decimal("0.005")
        # by_model 按模型分组
        modelNames = {entry.model_name for entry in summary.by_model}
        assert modelNames == {"gpt-4o", "llama3.1"}

    async def test_getSessionCost_returns_zero_for_unknown_session(self, dbSession) -> None:
        svc = TokenUsageService()
        cost = await svc.getSessionCost(dbSession, "nonexistent")
        assert cost == Decimal("0")

    async def test_listSessions_returns_distinct_sessions_with_aggregates(self, dbSession) -> None:
        config = await _seedConfig(dbSession)
        svc = TokenUsageService()
        # sess_1：2 次调用；sess_2：1 次调用
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=config.id, modelName="gpt-4o", promptTokens=100, completionTokens=50, cost=Decimal("0.005"), purpose="nl2sql")
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=config.id, modelName="gpt-4o", promptTokens=20, completionTokens=10, cost=Decimal("0.002"), purpose="answer")
        await svc.recordUsage(dbSession, sessionId="sess_2", modelConfigId=config.id, modelName="gpt-4o", promptTokens=5, completionTokens=2, cost=Decimal("0.001"), purpose="nl2sql")
        # sess_1 关联一条用户消息（用于 lastQuestion 预览）
        dbSession.add(SessionMessage(session_id="sess_1", role="user", content="各供应商收货量", question="各供应商收货量"))
        await dbSession.commit()

        sessions = await svc.listSessions(dbSession)

        assert len(sessions) == 2
        # 按最近活动降序：sess_2 的唯一调用晚于 sess_1 的第二条 -> 但两条 sess_1 在前；
        # 实际顺序取决于 lastRequestTime，这里校验集合与聚合即可
        byId = {s.session_id: s for s in sessions}
        assert byId["sess_1"].total_requests == 2
        assert byId["sess_1"].total_tokens == 180
        assert byId["sess_1"].total_cost == Decimal("0.007")
        assert byId["sess_1"].last_question == "各供应商收货量"
        assert byId["sess_2"].total_requests == 1
        assert byId["sess_2"].last_question is None  # 无关联消息

    async def test_listSessions_empty_when_no_usage(self, dbSession) -> None:
        svc = TokenUsageService()
        sessions = await svc.listSessions(dbSession)
        assert sessions == []

    async def test_getGlobalSummary_aggregates_across_sessions(self, dbSession) -> None:
        # Arrange
        config = await _seedConfig(dbSession)
        svc = TokenUsageService()
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=config.id, modelName="gpt-4o", promptTokens=100, completionTokens=50, cost=Decimal("0.005"), purpose="chat")
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=config.id, modelName="gpt-4o", promptTokens=20, completionTokens=10, cost=Decimal("0.002"), purpose="chat")
        await svc.recordUsage(dbSession, sessionId="sess_2", modelConfigId=None, modelName="ollama", promptTokens=5, completionTokens=2, cost=Decimal("0.001"), purpose="chat")
        # Act
        summary = await svc.getGlobalSummary(dbSession)
        # Assert
        assert summary.total_sessions == 2
        assert summary.total_requests == 3
        assert summary.total_tokens == 187
        assert summary.total_cost == Decimal("0.008")

    async def test_getGlobalSummary_empty_when_no_usage(self, dbSession) -> None:
        svc = TokenUsageService()
        summary = await svc.getGlobalSummary(dbSession)
        assert summary.total_sessions == 0
        assert summary.total_requests == 0
        assert summary.total_tokens == 0
        assert summary.total_cost == Decimal("0")

    async def test_getDailyTrends_groups_by_date_ascending(self, dbSession) -> None:
        # Arrange：直接插入指定 request_time 的行（aware UTC，与生产 _utcnow 写入路径一致）。
        # 日期相对今天计算（day-2 两行 + day-1 一行），避免硬编码绝对日期随时间
        # 滑出 30 天窗口导致测试腐烂（getDailyTrends 过滤 request_time >= now-30d）。
        config = await _seedConfig(dbSession)
        today = datetime.now(UTC).date()
        day1 = today - timedelta(days=2)
        day2 = today - timedelta(days=1)
        dbSession.add_all(
            [
                SessionTokenUsage(
                    session_id="s1", model_config_id=config.id, model_name="gpt-4o",
                    prompt_tokens=10, completion_tokens=5, total_tokens=15,
                    cost=Decimal("0.002"), request_time=datetime.combine(day1, time(9, 0), tzinfo=UTC), purpose="chat",
                ),
                SessionTokenUsage(
                    session_id="s1", model_config_id=config.id, model_name="gpt-4o",
                    prompt_tokens=10, completion_tokens=5, total_tokens=15,
                    cost=Decimal("0.003"), request_time=datetime.combine(day1, time(12, 0), tzinfo=UTC), purpose="chat",
                ),
                SessionTokenUsage(
                    session_id="s2", model_config_id=None, model_name="ollama",
                    prompt_tokens=2, completion_tokens=1, total_tokens=3,
                    cost=Decimal("0.001"), request_time=datetime.combine(day2, time(8, 0), tzinfo=UTC), purpose="chat",
                ),
            ]
        )
        await dbSession.commit()
        svc = TokenUsageService()
        # Act
        trends = await svc.getDailyTrends(dbSession, limit=30)
        # Assert
        assert [t.date for t in trends] == [day1, day2]
        assert trends[0].requests == 2
        assert trends[0].tokens == 30
        assert trends[0].cost == Decimal("0.005")
        assert trends[1].requests == 1
        assert trends[1].tokens == 3

    async def test_getDailyTrends_excludes_rows_outside_window(self, dbSession) -> None:
        # Arrange：40 天前的行应被 30 天窗口排除
        config = await _seedConfig(dbSession)
        today = datetime.now(UTC).date()
        oldDay = today - timedelta(days=40)
        dbSession.add_all(
            [
                SessionTokenUsage(
                    session_id="s1", model_config_id=config.id, model_name="gpt-4o",
                    prompt_tokens=1, completion_tokens=1, total_tokens=2,
                    cost=Decimal("0.001"), request_time=datetime.combine(today, datetime.min.time(), tzinfo=UTC),
                ),
                SessionTokenUsage(
                    session_id="s1", model_config_id=config.id, model_name="gpt-4o",
                    prompt_tokens=1, completion_tokens=1, total_tokens=2,
                    cost=Decimal("0.001"), request_time=datetime.combine(oldDay, datetime.min.time(), tzinfo=UTC),
                ),
            ]
        )
        await dbSession.commit()
        svc = TokenUsageService()
        # Act
        trends = await svc.getDailyTrends(dbSession, limit=30)
        # Assert
        assert [t.date for t in trends] == [today]

    async def test_getModelUsage_groups_by_model(self, dbSession) -> None:
        # Arrange
        config = await _seedConfig(dbSession)
        svc = TokenUsageService()
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=config.id, modelName="gpt-4o", promptTokens=100, completionTokens=50, cost=Decimal("0.005"), purpose="chat")
        await svc.recordUsage(dbSession, sessionId="sess_1", modelConfigId=config.id, modelName="gpt-4o", promptTokens=20, completionTokens=10, cost=Decimal("0.002"), purpose="chat")
        await svc.recordUsage(dbSession, sessionId="sess_2", modelConfigId=None, modelName=None, promptTokens=5, completionTokens=2, cost=Decimal("0.001"), purpose="chat")
        # Act
        byModel = await svc.getModelUsage(dbSession)
        # Assert
        byName = {m.model_name: m for m in byModel}
        assert set(byName) == {"gpt-4o", "unknown"}
        assert byName["gpt-4o"].requests == 2
        assert byName["gpt-4o"].tokens == 180
        assert byName["gpt-4o"].cost == Decimal("0.007")
        assert byName["unknown"].requests == 1

    async def test_getModelUsage_empty_when_no_usage(self, dbSession) -> None:
        svc = TokenUsageService()
        byModel = await svc.getModelUsage(dbSession)
        assert byModel == []
