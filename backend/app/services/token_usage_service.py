"""Token 使用记录与聚合服务。

负责将每次 LLM 调用的 Token 消耗持久化，并提供会话级成本/轮次聚合查询，
供模型路由（亲和性、预算降级）与对外查询接口使用。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import SessionMessage, SessionTokenUsage
from app.domain.schemas import (
    DailyUsageTrend,
    GlobalUsageSummary,
    ModelUsageAggregate,
    ModelUsageStat,
    SessionListItem,
    TokenUsageSummary,
)

logger = logging.getLogger(__name__)


class TokenUsageService:
    """Token 消耗流水服务。"""

    async def recordUsage(
        self,
        session: AsyncSession,
        *,
        sessionId: str,
        modelConfigId: int | None,
        modelName: str | None,
        promptTokens: int,
        completionTokens: int,
        cost: Decimal,
        purpose: str | None = None,
    ) -> SessionTokenUsage:
        """记录一次 LLM 调用的 Token 消耗并提交。"""
        totalTokens = promptTokens + completionTokens
        usage = SessionTokenUsage(
            session_id=sessionId,
            model_config_id=modelConfigId,
            model_name=modelName,
            prompt_tokens=promptTokens,
            completion_tokens=completionTokens,
            total_tokens=totalTokens,
            cost=Decimal(str(cost)),
            purpose=purpose,
        )
        session.add(usage)
        await session.commit()
        await session.refresh(usage)
        logger.info(
            "记录 Token 消耗: session=%s model=%s total=%d cost=%.6f",
            sessionId,
            modelName,
            totalTokens,
            float(usage.cost),
        )
        return usage

    async def getSessionCost(self, session: AsyncSession, sessionId: str) -> Decimal:
        """返回会话累计成本（美元）。"""
        stmt = select(func.coalesce(func.sum(SessionTokenUsage.cost), 0)).where(
            SessionTokenUsage.session_id == sessionId
        )
        result = await session.execute(stmt)
        return Decimal(result.scalar())

    async def getSessionTurnCount(self, session: AsyncSession, sessionId: str) -> int:
        """返回会话已记录的调用次数（近似对话轮次）。"""
        stmt = (
            select(func.count())
            .select_from(SessionTokenUsage)
            .where(SessionTokenUsage.session_id == sessionId)
        )
        result = await session.execute(stmt)
        return int(result.scalar() or 0)

    async def getLastModelId(self, session: AsyncSession, sessionId: str) -> int | None:
        """返回会话最近一次调用的 model_config_id（用于亲和性）。"""
        stmt = (
            select(SessionTokenUsage.model_config_id)
            .where(SessionTokenUsage.session_id == sessionId)
            .order_by(SessionTokenUsage.request_time.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def getUsageBySession(
        self, session: AsyncSession, sessionId: str
    ) -> list[SessionTokenUsage]:
        """返回会话的全部消耗流水（按时间升序）。"""
        stmt = (
            select(SessionTokenUsage)
            .where(SessionTokenUsage.session_id == sessionId)
            .order_by(SessionTokenUsage.request_time)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def summarize(self, session: AsyncSession, sessionId: str) -> TokenUsageSummary:
        """聚合会话 Token 与成本，并按模型分组。"""
        usages = await self.getUsageBySession(session, sessionId)
        totalTokens = sum(u.total_tokens for u in usages)
        totalCost = sum((Decimal(u.cost) for u in usages), Decimal("0"))

        byModelMap: dict[str, dict[str, Any]] = {}
        for u in usages:
            name = u.model_name or "unknown"
            bucket = byModelMap.setdefault(name, {"requests": 0, "tokens": 0, "cost": Decimal("0")})
            bucket["requests"] += 1
            bucket["tokens"] += u.total_tokens
            bucket["cost"] += Decimal(u.cost)

        byModel = [
            ModelUsageStat(
                model_name=name,
                requests=stat["requests"],
                total_tokens=stat["tokens"],
                total_cost=stat["cost"],
            )
            for name, stat in byModelMap.items()
        ]

        return TokenUsageSummary(
            session_id=sessionId,
            total_requests=len(usages),
            total_tokens=totalTokens,
            total_cost=totalCost,
            by_model=byModel,
        )

    async def listSessions(self, session: AsyncSession) -> list[SessionListItem]:
        """列出所有有 Token 流水的会话（聚合统计 + 最近活动），按最近活动降序。

        lastQuestion 取该会话最近一条 user 消息内容预览（无消息则为 None），
        经一次性批量查询避免 N+1。
        """
        agg = (
            select(
                SessionTokenUsage.session_id.label("session_id"),
                func.count().label("total_requests"),
                func.coalesce(func.sum(SessionTokenUsage.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(SessionTokenUsage.cost), 0).label("total_cost"),
                func.min(SessionTokenUsage.request_time).label("first_request_time"),
                func.max(SessionTokenUsage.request_time).label("last_request_time"),
            )
            .group_by(SessionTokenUsage.session_id)
            .order_by(func.max(SessionTokenUsage.request_time).desc())
        )
        agg_rows = (await session.execute(agg)).all()

        if not agg_rows:
            return []

        session_ids = [r.session_id for r in agg_rows]
        # 每个会话最近一条 user 消息：先取每会话 max(id)，再回连取 content
        max_id_sub = (
            select(
                SessionMessage.session_id.label("sid"),
                func.max(SessionMessage.id).label("mid"),
            )
            .where(SessionMessage.role == "user")
            .where(SessionMessage.session_id.in_(session_ids))
            .group_by(SessionMessage.session_id)
            .subquery()
        )
        last_msg_q = select(SessionMessage.session_id, SessionMessage.content).join(
            max_id_sub,
            and_(
                SessionMessage.session_id == max_id_sub.c.sid,
                SessionMessage.id == max_id_sub.c.mid,
            ),
        )
        last_msg_map: dict[str, str] = {
            r.session_id: r.content for r in (await session.execute(last_msg_q)).all()
        }

        return [
            SessionListItem(
                session_id=r.session_id,
                total_requests=int(r.total_requests or 0),
                total_tokens=int(r.total_tokens or 0),
                total_cost=Decimal(r.total_cost or 0),
                first_request_time=r.first_request_time,
                last_request_time=r.last_request_time,
                last_question=last_msg_map.get(r.session_id),
            )
            for r in agg_rows
        ]

    async def getGlobalSummary(self, session: AsyncSession) -> GlobalUsageSummary:
        """跨全部会话的全局用量摘要（会话数 / 请求数 / Token / 成本）。"""
        stmt = select(
            func.count(func.distinct(SessionTokenUsage.session_id)),
            func.count(),
            func.coalesce(func.sum(SessionTokenUsage.total_tokens), 0),
            func.coalesce(func.sum(SessionTokenUsage.cost), 0),
        )
        totalSessions, totalRequests, totalTokens, totalCost = (
            (await session.execute(stmt)).one()
        )
        return GlobalUsageSummary(
            total_sessions=int(totalSessions or 0),
            total_requests=int(totalRequests or 0),
            total_tokens=int(totalTokens or 0),
            total_cost=Decimal(totalCost or 0),
        )

    async def getDailyTrends(
        self, session: AsyncSession, limit: int = 30
    ) -> list[DailyUsageTrend]:
        """按天聚合全局用量（近 limit 天），升序返回。

        按日期截断在 SQLite 与 PostgreSQL 中无通用表达式（SQLite 无 date_trunc、
        PostgreSQL 无 date()），故 SQL 只做时间窗过滤，Python 侧按 request_time 的
        日期分组，保证两库行为一致且被测试完整覆盖。
        """
        cutoff = datetime.now(UTC) - timedelta(days=limit)
        stmt = (
            select(
                SessionTokenUsage.request_time,
                SessionTokenUsage.total_tokens,
                SessionTokenUsage.cost,
            )
            .where(SessionTokenUsage.request_time >= cutoff)
            .order_by(SessionTokenUsage.request_time)
        )
        rows = (await session.execute(stmt)).all()

        buckets: dict[date, dict[str, Any]] = {}
        for requestTime, totalTokens, cost in rows:
            day = requestTime.date()
            bucket = buckets.setdefault(
                day, {"requests": 0, "tokens": 0, "cost": Decimal("0")}
            )
            bucket["requests"] += 1
            bucket["tokens"] += totalTokens
            bucket["cost"] += Decimal(cost)

        return [
            DailyUsageTrend(
                date=day,
                requests=bucket["requests"],
                tokens=bucket["tokens"],
                cost=bucket["cost"],
            )
            for day, bucket in sorted(buckets.items())
        ]

    async def getModelUsage(self, session: AsyncSession) -> list[ModelUsageAggregate]:
        """按模型名聚合全局用量（空模型名归入 unknown）。"""
        modelExpr = func.coalesce(SessionTokenUsage.model_name, "unknown")
        stmt = (
            select(
                modelExpr.label("model_name"),
                func.count().label("requests"),
                func.coalesce(func.sum(SessionTokenUsage.total_tokens), 0).label("tokens"),
                func.coalesce(func.sum(SessionTokenUsage.cost), 0).label("cost"),
            )
            .group_by(modelExpr)
            .order_by(modelExpr)
        )
        rows = (await session.execute(stmt)).all()
        return [
            ModelUsageAggregate(
                model_name=row.model_name,
                requests=int(row.requests or 0),
                tokens=int(row.tokens or 0),
                cost=Decimal(row.cost or 0),
            )
            for row in rows
        ]
