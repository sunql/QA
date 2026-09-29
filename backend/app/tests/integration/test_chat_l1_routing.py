"""Phase 1 Task 1.4: Chat L1 KPI Semantic Match Routing 集成测试。

真实 PG + 完整 API 链路（强制规则：Harness/rules/测试规范.md）：

本文件测试 L1 精确 alias 匹配（KPI code 形式大写下划线词 → confidence=1.0），
因为精确匹配不依赖 Jaccard 阈值或 formula 执行，可在真实 DB 环境下验证。

L1 语义关键词匹配（依赖 Jaccard + formula 执行）在单元测试
test_chat_service.py::TestChatL1Routing 中覆盖（patch mock _buildL1Response）。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog, KpiStatus
from app.services.kpi_match_cache import kpi_match_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAT_DATASOURCE_ID = 9201


def _chat_payload(question: str, datasourceId: int = CHAT_DATASOURCE_ID, sessionId: str | None = None) -> dict:
    return {
        "sessionId": sessionId or f"s-{uuid.uuid4().hex[:8]}",
        "question": question,
        "datasourceId": datasourceId,
    }


async def _seedDatasource(dbSession: AsyncSession) -> None:
    """seed 一个最小可用 DataSource（id=9201），chat 请求必填 datasourceId。"""
    from app.domain.models import DataSource
    ds = DataSource(
        id=CHAT_DATASOURCE_ID,
        name="ds-chat-l1",
        type="postgresql",
        host="localhost",
        port=5432,
        database_name="x",
        username="u",
        password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.commit()


async def _seedKpi(dbSession: AsyncSession, **kwargs) -> KpiCatalog:
    """在 kpi_catalog 插入一条 PUBLISHED KPI（最小必要子集）。"""
    kpi = KpiCatalog(
        kpi_code=kwargs.get("kpi_code", "TEST_KPI_CODE"),
        kpi_name=kwargs.get("kpi_name", "测试指标"),
        business_definition=kwargs.get("business_definition", "这是一个测试指标"),
        formula=kwargs.get("formula"),
        status=KpiStatus.PUBLISHED.value,
        match_threshold=Decimal("0.5"),
        semantic_keywords=kwargs.get("semantic_keywords", ["测试", "指标"]),
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestChatL1Routing:
    """L1 KPI 语义匹配路由测试套件。"""

    @pytest.mark.asyncio
    async def test_chat_l1_match_exact_alias(self, client, dbSession: AsyncSession) -> None:
        """精确 alias 匹配（KPI code 形式大写下划线词）直接返回 1.0 置信度。

        精确 alias 匹配是 L1 快车道最高优先级路径：
        FEATURE_NAME_RE 提取大写下划线 token → hasCode 精确查找 → confidence=1.0。
        不依赖 Jaccard 阈值或 formula 执行，故可在最小 DB 环境下验证。
        """
        # Arrange：seed datasource + KPI（code 形式别名）
        await _seedDatasource(dbSession)
        await _seedKpi(
            dbSession,
            kpi_code="KPI_SUPPLIER_OTD",
            kpi_name="供应商及时交货率",
            business_definition="供应商按时交货的订单占比",
            formula="SELECT 0.954 AS otd_rate",
            semantic_keywords=["otd", "供应商", "交货"],
        )
        kpi_match_cache.onKpiChanged()
        await kpi_match_cache.warmUp(dbSession)

        # Act：问题直接包含 KPI code（FEATURE_NAME_RE 提取到 KPI_SUPPLIER_OTD）
        resp = await client.post(
            "/api/v1/chat",
            json=_chat_payload(question="KPI_SUPPLIER_OTD 这个指标是多少"),
        )

        # Assert：L1 精确命中，confidence=1.0，0 token 消耗
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["intent"] == "l1_match"
        assert data.get("kpiCode") == "KPI_SUPPLIER_OTD"
        assert data.get("confidence") == 1.0, f"expected exact match confidence 1.0, got {data.get('confidence')}"
        assert data.get("tokensUsed", -1) == 0, f"expected 0 tokens, got {data.get('tokensUsed')}"
        assert data.get("cost", -1) == 0.0, f"expected 0 cost, got {data.get('cost')}"

    @pytest.mark.asyncio
    async def test_chat_l1_writes_routing_layer(self, client, dbSession: AsyncSession) -> None:
        """L1 命中时 assistant session_message 应写入 routing_layer='L1'。

        Phase 5 监控管道依赖 routing_layer 列，此测试确保 L1 快车道命中时正确埋点。
        """
        from sqlalchemy import select
        from app.domain.models import SessionMessage

        # Arrange
        await _seedDatasource(dbSession)
        await _seedKpi(
            dbSession,
            kpi_code="KPI_SUPPLIER_OTD",
            kpi_name="供应商及时交货率",
            business_definition="供应商按时交货的订单占比",
            formula="SELECT 0.954 AS otd_rate",
            semantic_keywords=["otd", "供应商", "交货"],
        )
        kpi_match_cache.onKpiChanged()
        await kpi_match_cache.warmUp(dbSession)

        session_id = f"s-{uuid.uuid4().hex[:8]}"

        # Act
        resp = await client.post(
            "/api/v1/chat",
            json=_chat_payload(
                question="KPI_SUPPLIER_OTD 这个指标是多少",
                sessionId=session_id,
            ),
        )

        # Assert：请求成功
        assert resp.status_code == 200, resp.text

        # Assert：assistant 行写入 routing_layer='L1'
        result = await dbSession.execute(
            select(SessionMessage).where(
                SessionMessage.session_id == session_id,
                SessionMessage.role == "assistant",
            )
        )
        assistant_msg = result.scalar_one_or_none()
        assert assistant_msg is not None, "assistant message not found in session_message"
        assert assistant_msg.routing_layer == "L1", (
            f"expected routing_layer='L1', got {assistant_msg.routing_layer!r}"
        )
        assert assistant_msg.token_cost_usd == 0.0, (
            f"expected token_cost_usd=0.0, got {assistant_msg.token_cost_usd!r}"
        )
        assert assistant_msg.latency_ms is not None, "latency_ms should be set for L1"
        assert assistant_msg.latency_ms >= 0, f"latency_ms should be non-negative, got {assistant_msg.latency_ms}"
