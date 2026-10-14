"""Integration tests: preamble meta frames must be exactly 1, not 2-3 (F2 regression guard).

Task 1.2c: https://github.com/sunql/qa-system/pull/9

Defect: chat_stream.py yields 2-3 separate EVENT_META frames for a single preamble
(L1 + mismatch_warning + agent_run), but the frontend's onMeta handler overwrites
intent on every frame, so frames 2+ wipe out the intent set by frame 1.

Fix: merge into a single EVENT_META with optional fields.

IMPORTANT constraints from brief:
- intent value "KPI" / "AGENT_RUN" is a PRE-EXISTING defect (not in IntentType enum).
  Do NOT fix it here — report it as out-of-scope.
- Test DB port is 5434 (dedicated), NOT 5433 (shared with prod).
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

CHAT_DATASOURCE_ID = 9201


def _chat_payload(question: str, datasourceId: int, sessionId: str | None = None) -> dict:
    return {
        "sessionId": sessionId or f"s-{uuid.uuid4().hex[:8]}",
        "question": question,
        "datasourceId": datasourceId,
    }


async def _seedDatasource(dbSession: AsyncSession) -> None:
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
    datasource_id: int,
    unit: str | None = None,
) -> KpiCatalog:
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
        datasource_id=datasource_id,  # F2: real FK (Task 1.2a)
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


class _ScalarAdapter:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def execute_read_only(self, sql: str) -> list[dict]:
        return self._rows


@pytest.fixture
async def seeded_kpi_mismatch(dbSession: AsyncSession) -> dict:
    """Seed KPI on datasource_id=9201; user will query with 9202 to trigger mismatch."""
    await _seedDatasource(dbSession)
    # Seed another datasource (9202) so the request itself is valid
    from app.domain.models import DataSource

    other_ds = DataSource(
        id=CHAT_DATASOURCE_ID + 1,
        name="ds-other",
        type="postgresql",
        host="localhost",
        port=5432,
        database_name="x",
        username="u",
        password_encrypted="x",
    )
    dbSession.add(other_ds)
    await dbSession.commit()

    kpi = await _seedKpiWithFeature(
        dbSession,
        kpi_code="KPI_SUPPLIER_OTD",
        datasource_id=CHAT_DATASOURCE_ID,  # KPI固化在9201
        unit="%",
    )
    kpi_match_cache.onKpiChanged()
    await kpi_match_cache.warmUp(dbSession)
    return {
        "kpi_datasource_id": CHAT_DATASOURCE_ID,  # KPI所在数据源
        "user_datasource_id": CHAT_DATASOURCE_ID + 1,  # 用户选的数据源（不同=>mismatch)
        "kpi_code": kpi.kpi_code,
        "user_id": 1,
    }


@pytest.fixture
async def seeded_kpi_no_mismatch(dbSession: AsyncSession) -> dict:
    """Seed KPI on datasource_id=9201; user queries same datasource => no mismatch."""
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
        "kpi_datasource_id": CHAT_DATASOURCE_ID,
        "user_datasource_id": CHAT_DATASOURCE_ID,  # same => no mismatch
        "kpi_code": kpi.kpi_code,
        "user_id": 1,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_l1_mismatch_warning_meta_frame_is_single(
    client: AsyncClient,
    dbSession: AsyncSession,
    seeded_kpi_mismatch: dict,
    monkeypatch,
) -> None:
    """L1 hit + datasource mismatch => preamble yields EXACTLY 1 meta frame (not 2).

    RED evidence (before fix): frames[0] and frames[1] are both EVENT_META.
    GREEN (after fix): exactly 1 EVENT_META containing intent/routingLayer/
    datasourceMismatch/warning/code all in one frame.
    """
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(
        chat_module._service,
        "_adapterProvider",
        lambda dsId, ds: _ScalarAdapter([{"otd_rate": Decimal("0.954")}]),
    )

    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "question": "KPI_SUPPLIER_OTD 这个指标是多少",  # exact alias match
            "sessionId": "stream-mismatch-single-meta",
            "datasourceId": seeded_kpi_mismatch["user_datasource_id"],
        },
        headers={"X-User-Id": str(seeded_kpi_mismatch["user_id"])},
    )
    assert resp.status_code == 200, resp.text

    from app.tests.integration.test_chat_stream_api import _parseFrames

    frames = _parseFrames(resp.text)
    meta_frames = [(e, d) for e, d in frames if e == "meta"]

    # CORE GUARD: must be exactly 1 meta frame
    assert len(meta_frames) == 1, (
        f"expected exactly 1 meta frame, got {len(meta_frames)}: "
        f"{[(e, d) for e, d in meta_frames]}"
    )

    # The single frame must contain all required fields
    single = meta_frames[0][1]
    assert "intent" in single, f"missing intent in meta frame: {single}"
    assert "routingLayer" in single, f"missing routingLayer in meta frame: {single}"
    assert "datasourceMismatch" in single, f"missing datasourceMismatch in meta frame: {single}"
    assert "warning" in single, f"missing warning in meta frame: {single}"
    assert "code" in single, f"missing code in meta frame: {single}"
    assert single["datasourceMismatch"] is True, f"expected datasourceMismatch=True, got {single}"
    assert single["code"] == "KPI_DATASOURCE_MISMATCH", f"expected code=KPI_DATASOURCE_MISMATCH, got {single}"

    # Reverse guard: intent value must be EXACTLY as before (do not fix "KPI")
    # This locks the constraint that we do not change the intent value
    assert single["intent"] == "KPI", (
        f"intent must remain 'KPI' (pre-existing defect, out of scope). "
        f"Got: {single['intent']}"
    )
    assert single["routingLayer"] == "L1", f"expected routingLayer=L1, got {single['routingLayer']}"


async def test_l1_no_mismatch_meta_frame_is_single(
    client: AsyncClient,
    dbSession: AsyncSession,
    seeded_kpi_no_mismatch: dict,
    monkeypatch,
) -> None:
    """L1 hit without mismatch => preamble yields EXACTLY 1 meta frame (regression guard)."""
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(
        chat_module._service,
        "_adapterProvider",
        lambda dsId, ds: _ScalarAdapter([{"otd_rate": Decimal("0.954")}]),
    )

    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "question": "KPI_SUPPLIER_OTD 这个指标是多少",
            "sessionId": "stream-no-mismatch-single-meta",
            "datasourceId": seeded_kpi_no_mismatch["user_datasource_id"],
        },
        headers={"X-User-Id": str(seeded_kpi_no_mismatch["user_id"])},
    )
    assert resp.status_code == 200, resp.text

    from app.tests.integration.test_chat_stream_api import _parseFrames

    frames = _parseFrames(resp.text)
    meta_frames = [(e, d) for e, d in frames if e == "meta"]

    assert len(meta_frames) == 1, (
        f"expected exactly 1 meta frame without mismatch, got {len(meta_frames)}"
    )
    single = meta_frames[0][1]
    assert single["intent"] == "KPI", f"intent must be 'KPI', got {single['intent']}"
    assert single["routingLayer"] == "L1"
    assert "warning" not in single, f"unexpected warning field without mismatch: {single}"
    assert "code" not in single, f"unexpected code field without mismatch: {single}"
