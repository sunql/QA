"""integration tests for chat L1 KPI + L4 Agent in stream path.

RED: 流式入口未调 KPI/L4 => RED。
GREEN: 流式入口接 KPI/L4 pre-amble => GREEN。
反向断言（P0 验收硬性）：
- 用户选 datasource_B + 命中 datasource_A 固化的 KPI -> 流式帧含 warning 文案
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog, KpiStatus
from app.services.kpi_match_cache import kpi_match_cache

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAT_DATASOURCE_ID = 9201


def _chat_payload(
    question: str,
    datasourceId: int = CHAT_DATASOURCE_ID,
    sessionId: str | None = None,
) -> dict:
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


async def _seedKpiWithFeature(
    dbSession: AsyncSession,
    *,
    kpi_code: str,
    datasource_id: int = CHAT_DATASOURCE_ID,
    unit: str | None = None,
) -> KpiCatalog:
    """seed 一条可执行的 KPI：business_object + feature_definition + kpi_catalog。

    KPI 固化在特定 datasource，验证跨数据源 mismatch 警告。
    """
    from app.domain.models import BusinessObject, FeatureDefinition

    dbSession.add(BusinessObject(code="BO_L1_STREAM", name="L1 流式测试对象"))
    await dbSession.flush()
    KPI_SQL = "SELECT 0.954 AS otd_rate"
    dbSession.add(
        FeatureDefinition(
            feature_name=f"l1-stream-{kpi_code}",
            entity_type="BO_L1_STREAM",
            feature_definition=KPI_SQL,
            calculation_logic=KPI_SQL,
            datasource_id=datasource_id,
            is_enabled=True,
        )
    )
    await dbSession.flush()
    kpi = KpiCatalog(
        kpi_code=kpi_code,
        kpi_name="供应商及时交货率",
        business_definition="供应商按时交货的订单占比",
        formula=KPI_SQL,
        unit=unit,
        status=KpiStatus.PUBLISHED.value,
        match_threshold=Decimal("0.5"),
        semantic_keywords=["otd", "供应商", "交货"],
        data_source=str(datasource_id),
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


class _ScalarAdapter:
    """只回一行标量的假适配器。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def execute_read_only(self, sql: str) -> list[dict]:
        return self._rows


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_kpi(dbSession: AsyncSession) -> dict:
    """seed datasource + KPI（含 feature，固化在 datasource_id=9201）。"""
    await _seedDatasource(dbSession)
    kpi = await _seedKpiWithFeature(
        dbSession,
        kpi_code="KPI_SUPPLIER_OTD",
        datasource_id=CHAT_DATASOURCE_ID,
        unit="%",
    )
    kpi_match_cache.onKpiChanged()
    await kpi_match_cache.warmUp(dbSession)
    return {
        "datasource_id": CHAT_DATASOURCE_ID,
        "kpi_code": kpi.kpi_code,
        "user_id": 1,  # default test user
    }


@pytest.fixture
async def seeded_user(dbSession: AsyncSession) -> dict:
    """seed datasource（无 KPI），用于 chitchat 对照组。"""
    await _seedDatasource(dbSession)
    return {
        "datasource_id": CHAT_DATASOURCE_ID,
        "user_id": 1,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_stream_kpi_match_returns_zero_llm_cost(
    client: AsyncClient,
    dbSession: AsyncSession,
    seeded_kpi: dict,
    monkeypatch,
) -> None:
    """L1 KPI 命中 -> 流式 SSE 包含 chart=KPI + routing_layer=L1 + done 帧携带 cost=0。"""
    import app.api.v1.chat as chat_module

    # monkeypatch _adapterProvider so the KPI formula executes without a real DB
    monkeypatch.setattr(
        chat_module._service,
        "_adapterProvider",
        lambda dsId, ds: _ScalarAdapter([{"otd_rate": Decimal("0.954")}]),
    )

    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "question": "KPI_SUPPLIER_OTD 这个指标是多少",
            "sessionId": "stream-test-1",
            "datasourceId": seeded_kpi["datasource_id"],
        },
        headers={"X-User-Id": str(seeded_kpi["user_id"])},
    )
    assert resp.status_code == 200, resp.text
    text = resp.text
    # SSE frames: meta + chart + [token] + done (KPI answer via token, not LLM streaming)
    assert "event: meta" in text, f"expected 'event: meta' in SSE, got: {text[:200]}"
    assert "routingLayer" in text, f"expected 'routingLayer' in meta, got: {text[:200]}"
    assert '"L1"' in text, f"expected 'L1' routing layer, got: {text[:200]}"
    assert "event: chart" in text, f"expected 'event: chart' in SSE, got: {text[:200]}"
    assert "kpi" in text, f"expected 'kpi' in chart payload, got: {text[:200]}"
    assert "event: done" in text, f"expected 'event: done' in SSE, got: {text[:200]}"
    # KPI answer must appear (chart value + token text + zero cost confirms L1 path)
    assert "0.954" in text, f"expected KPI value '0.954' in SSE, got: {text[:200]}"
    assert '"tokenCostUsd": 0.0' in text, f"expected zero cost in done event, got: {text[:200]}"


async def test_stream_kpi_datasource_mismatch_warns(
    client: AsyncClient,
    dbSession: AsyncSession,
    seeded_kpi: dict,
    monkeypatch,
) -> None:
    """KPI 固化在 datasource_A，用户选 datasource_B -> SSE 含 mismatch 警告，不静默。"""
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(
        chat_module._service,
        "_adapterProvider",
        lambda dsId, ds: _ScalarAdapter([{"otd_rate": Decimal("0.954")}]),
    )

    other_ds_id = CHAT_DATASOURCE_ID + 1
    # seed the other datasource so the request is valid
    from app.domain.models import DataSource

    dbSession.add(
        DataSource(
            id=other_ds_id,
            name="ds-other",
            type="postgresql",
            host="localhost",
            port=5432,
            database_name="x",
            username="u",
            password_encrypted="x",
        )
    )
    await dbSession.commit()

    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "question": "KPI_SUPPLIER_OTD 这个指标是多少",
            "sessionId": "stream-mismatch",
            "datasourceId": other_ds_id,
        },
        headers={"X-User-Id": str(seeded_kpi["user_id"])},
    )
    assert resp.status_code == 200, resp.text
    text = resp.text
    assert "datasourceMismatch" in text, f"expected mismatch warning in SSE, got: {text[:200]}"
    assert "KPI" in text, f"expected 'KPI' in mismatch warning, got: {text[:200]}"
    # The warning must be explicit, not silent
    mismatch_indicators = ["bound to", "固化", "不一致", "mismatch", "different"]
    assert any(ind.lower() in text.lower() for ind in mismatch_indicators), (
        f"expected explicit mismatch indicator in SSE, got: {text[:300]}"
    )


async def test_stream_l4_agent_runs_when_kpi_misses(
    client: AsyncClient,
    dbSession: AsyncSession,
    seeded_user: dict,
) -> None:
    """KPI 未命中 -> L4 Agent Loop 命中 -> 流式走 agent card 帧。

    L4 is triggered by questions like '用 supplier_risk_agent 评估 100001'.
    """
    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "question": "用 supplier_risk_agent 评估供应商 100001",
            "sessionId": "stream-l4",
            "datasourceId": seeded_user["datasource_id"],
        },
        headers={"X-User-Id": str(seeded_user["user_id"])},
    )
    assert resp.status_code == 200, resp.text
    text = resp.text
    assert "agentRun" in text, f"expected agentRun in SSE, got: {text[:200]}"
    assert "event: done" in text, f"expected 'event: done' in SSE, got: {text[:200]}"


async def test_stream_chitchat_skips_l1_l4(
    client: AsyncClient,
    dbSession: AsyncSession,
    seeded_user: dict,
) -> None:
    """'你好' -> chitchat 帧，不触发 KPI/L4 预检。"""
    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "question": "你好",
            "sessionId": "stream-chitchat",
            "datasourceId": seeded_user["datasource_id"],
        },
        headers={"X-User-Id": str(seeded_user["user_id"])},
    )
    assert resp.status_code == 200, resp.text
    text = resp.text
    assert "event: meta" in text, f"expected 'event: meta' in SSE, got: {text[:200]}"
    assert "chitchat" in text.lower(), f"expected chitchat in SSE, got: {text[:200]}"
    # chitchat does NOT include routingLayer (L1/L4 is a KPI/agent concept, not chitchat)
    assert "routingLayer" not in text, (
        f"chitchat should not include 'routingLayer', got: {text[:200]}"
    )
