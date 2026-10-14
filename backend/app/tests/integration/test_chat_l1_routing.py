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
        unit=kwargs.get("unit"),
        status=KpiStatus.PUBLISHED.value,
        match_threshold=Decimal("0.5"),
        semantic_keywords=kwargs.get("semantic_keywords", ["测试", "指标"]),
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


KPI_SCALAR_SQL = "SELECT 0.954 AS otd_rate"


class _ScalarAdapter:
    """只回一行标量的假适配器：L1 的 formula 是「单值 SELECT」，不需要真连业务库。

    `execute_read_only` 是 `_executeCalculationLogic` 唯一用到的方法（真实适配器
    会先过 SQL Guard，这里关心的不是 SQL 正确性而是**出参怎么变成卡片**）。
    """

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def execute_read_only(self, sql: str) -> list[dict]:
        return self._rows


async def _seedKpiWithFeature(
    dbSession: AsyncSession,
    *,
    kpi_code: str,
    unit: str | None = None,
) -> KpiCatalog:
    """seed 一条**可执行**的 KPI：business_object + feature_definition + kpi_catalog。

    L1 的 formula 走的不是 SQL 生成链路，而是「formula == feature_definition.feature_definition」
    关联到一条 FeatureDefinition（`_resolveCalculationFeature`）。父表必须先有
    business_object —— `feature_definition.entity_type` 是指向它的 FK。
    """
    from app.domain.models import BusinessObject, FeatureDefinition

    dbSession.add(BusinessObject(code="BO_L1_CARD", name="L1 卡片测试对象"))
    await dbSession.flush()
    dbSession.add(
        FeatureDefinition(
            feature_name=f"l1-card-{kpi_code}",
            entity_type="BO_L1_CARD",
            feature_definition=KPI_SCALAR_SQL,  # service 用 feature_definition == kpi.formula 关联
            calculation_logic=KPI_SCALAR_SQL,
            datasource_id=CHAT_DATASOURCE_ID,
            is_enabled=True,
        )
    )
    return await _seedKpi(
        dbSession,
        kpi_code=kpi_code,
        kpi_name="供应商及时交货率",
        business_definition="供应商按时交货的订单占比",
        formula=KPI_SCALAR_SQL,
        unit=unit,
        semantic_keywords=["otd", "供应商", "交货"],
    )


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


class TestL1KpiCard:
    """L1 直答的指标卡走完整 API 链路（决策 7）。

    单元测试 `test_chat_l1_kpi_chart.py` 钉的是负载形状；这里钉的是**接线**——
    卡片的 kind/option 有没有真的从 `_buildL1Response` 走到 JSON 响应里
    （`chartType`/`chartOption` 是 camelCase 契约字段，漏了 alias 就静默变 null）。
    """

    @pytest.mark.asyncio
    async def test_l1_answer_carries_a_kpi_card(
        self, client, dbSession: AsyncSession, monkeypatch
    ) -> None:
        import app.api.v1.chat as chat_module

        # Arrange：可执行的 KPI（带单位）+ 回单值的假适配器（不真连业务库）
        await _seedDatasource(dbSession)
        await _seedKpiWithFeature(dbSession, kpi_code="KPI_SUPPLIER_ODT", unit="%")
        kpi_match_cache.onKpiChanged()
        await kpi_match_cache.warmUp(dbSession)
        monkeypatch.setattr(
            chat_module._service,
            "_adapterProvider",
            lambda dsId, ds: _ScalarAdapter([{"otd_rate": Decimal("0.954")}]),
        )

        # Act
        resp = await client.post(
            "/api/v1/chat",
            json=_chat_payload(question="KPI_SUPPLIER_ODT 这个指标是多少"),
        )

        # Assert
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["intent"] == "l1_match"
        assert data["chartType"] == "kpi"
        assert data["chartOption"] == {
            "kpi": {"label": "供应商及时交货率", "value": 0.954, "unit": "%", "delta": None}
        }
        # 卡上的值与回答文本里的值是同一个（同源提取）
        assert "0.954" in data["answer"]

    @pytest.mark.asyncio
    async def test_l1_answer_without_a_value_carries_no_card(
        self, client, dbSession: AsyncSession, monkeypatch
    ) -> None:
        """formula 回了 NULL → 不发空壳卡，文本退回 `：—` 占位。

        口径说明（business_definition）只在**执行失败**（data 为 None）时出现；
        执行成功但值为 NULL 是另一回事，此时用户已经拿到了「指标是多少」的答案
        ——答案是「没有值」，不该再拿定义去盖掉它。
        """
        import app.api.v1.chat as chat_module

        await _seedDatasource(dbSession)
        await _seedKpiWithFeature(dbSession, kpi_code="KPI_SUPPLIER_ODT")
        kpi_match_cache.onKpiChanged()
        await kpi_match_cache.warmUp(dbSession)
        monkeypatch.setattr(
            chat_module._service,
            "_adapterProvider",
            lambda dsId, ds: _ScalarAdapter([{"otd_rate": None}]),
        )

        resp = await client.post(
            "/api/v1/chat",
            json=_chat_payload(question="KPI_SUPPLIER_ODT 这个指标是多少"),
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["intent"] == "l1_match"
        assert data["chartType"] is None
        assert data["chartOption"] is None
        assert data["answer"] == "指标「供应商及时交货率」：—"

    @pytest.mark.asyncio
    async def test_l1_answer_carries_rationale_and_persists_it(
        self, client, dbSession: AsyncSession, monkeypatch
    ) -> None:
        """F1：L1 直答必须带判断依据（R01_SINGLE_VALUE_KPI），且同轮落库（回放不丢）。

        用户原始需求「不论是否输出图，必须输出一个判断逻辑」——L1 快路径此前只发卡
        （chartType），没有任何依据；回放/导出拿不到依据正是本特性要消灭的问题。
        """
        from sqlalchemy import select

        from app.domain.models import SessionMessage

        import app.api.v1.chat as chat_module

        await _seedDatasource(dbSession)
        await _seedKpiWithFeature(dbSession, kpi_code="KPI_SUPPLIER_ODT", unit="%")
        kpi_match_cache.onKpiChanged()
        await kpi_match_cache.warmUp(dbSession)
        monkeypatch.setattr(
            chat_module._service,
            "_adapterProvider",
            lambda dsId, ds: _ScalarAdapter([{"otd_rate": Decimal("0.954")}]),
        )

        session_id = f"s-{uuid.uuid4().hex[:8]}"
        resp = await client.post(
            "/api/v1/chat",
            json=_chat_payload(
                question="KPI_SUPPLIER_ODT 这个指标是多少", sessionId=session_id
            ),
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["intent"] == "l1_match"
        assert data["visualRationale"]["code"] == "R01_SINGLE_VALUE_KPI"
        assert data["visualRationale"]["params"] == {}

        rows = list((await dbSession.execute(
            select(SessionMessage).where(
                SessionMessage.session_id == session_id
            ).order_by(SessionMessage.id)
        )).scalars().all())
        assert len(rows) == 2
        assert rows[1].visual_rationale == {"code": "R01_SINGLE_VALUE_KPI", "params": {}}
        assert rows[0].visual_rationale is None, "user 行不该带依据"
