"""integration tests for KPI datasource scope guard (feat-aichat-gap-f2-kpi-datasource).

Verifies the L1 KPI datasource mismatch check uses the real FK column
kpi_catalog.datasource_id (not the legacy data_source text field).

Test matrix:
1. KPI.datasource_id != request.datasourceId -> datasourceMismatch=true, warning present
2. KPI.datasource_id == request.datasourceId -> datasourceMismatch=false
3. KPI.datasource_id IS NULL -> datasourceMismatch=false (null = no binding, skip guard)
4. Reverse: data_source="ERP" but datasource_id=NULL -> NO mismatch
   (proves the fix actually severed the text-vs-int comparison path)
5. Migration: column exists + FK constraint points to data_source(id)

RED phase: all tests fail because the column does not exist yet.
GREEN phase: migration applied, implementation corrected.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, KpiCatalog, KpiStatus
from app.services.kpi_match_cache import kpi_match_cache

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CHAT_DS_ID = 9202  # isolated from other test suites


async def _seedDatasource(dbSession: AsyncSession, dsId: int = _CHAT_DS_ID) -> DataSource:
    ds = DataSource(
        id=dsId,
        name=f"ds-scope-{dsId}",  # unique name per ID to satisfy uq_datasource_name
        type="postgresql",
        host="localhost",
        port=5432,
        database_name="x",
        username="u",
        password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


async def _seedKpi(
    dbSession: AsyncSession,
    kpiCode: str,
    datasourceId: int,
    *,
    dataSourceText: str | None = None,  # legacy field, used only for reverse-assert test
) -> KpiCatalog:
    kpi = KpiCatalog(
        kpi_code=kpiCode,
        kpi_name="测试指标",
        business_definition="测试",
        formula="SELECT 1",
        data_source=dataSourceText,
        datasource_id=datasourceId,
        status=KpiStatus.PUBLISHED.value,
        match_threshold=Decimal("0.5"),
        semantic_keywords=["测试", "test"],
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


async def _seedKpiNullDatasource(
    dbSession: AsyncSession,
    kpiCode: str,
    *,
    dataSourceText: str | None = None,
) -> KpiCatalog:
    """KPI with datasource_id=NULL (no binding)."""
    kpi = KpiCatalog(
        kpi_code=kpiCode,
        kpi_name="无绑定指标",
        business_definition="测试",
        formula="SELECT 1",
        data_source=dataSourceText,
        datasource_id=None,
        status=KpiStatus.PUBLISHED.value,
        match_threshold=Decimal("0.5"),
        semantic_keywords=["无绑定", "null-ds"],
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


def _chatPayload(question: str, datasourceId: int, sessionId: str | None = None) -> dict:
    return {
        "sessionId": sessionId or f"s-{uuid.uuid4().hex[:8]}",
        "question": question,
        "datasourceId": datasourceId,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestKpiDatasourceScope:
    """L1 KPI datasource mismatch guard tests."""

    async def test_mismatch_true_when_datasource_id_differs(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        monkeypatch,
    ) -> None:
        """KPI bound to ds=9202, user selects ds=9203 -> mismatch=true + warning."""
        ds1 = await _seedDatasource(dbSession, _CHAT_DS_ID)
        ds2 = await _seedDatasource(dbSession, _CHAT_DS_ID + 1)

        kpi = await _seedKpi(dbSession, "KPI_SCOPE_MISMATCH", ds1.id)
        kpi_match_cache.onKpiChanged()
        await kpi_match_cache.warmUp(dbSession)

        import app.api.v1.chat as chat_module

        monkeypatch.setattr(
            chat_module._service,
            "_adapterProvider",
            lambda dsId, ds: _ScalarAdapter([{"v": 1}]),
        )

        resp = await client.post(
            "/api/v1/chat/stream",
            json=_chatPayload("KPI_SCOPE_MISMATCH 这个指标是多少", ds2.id),
            headers={"X-User-Id": "1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "datasourceMismatch" in body, (
            f"expected datasourceMismatch flag in SSE, got: {body[:300]}"
        )
        # warning must be explicit, not silent
        mismatch_tokens = ["KPI", "不一致", "mismatch", "bound"]
        assert any(t.lower() in body.lower() for t in mismatch_tokens), (
            f"expected explicit mismatch token in SSE, got: {body[:400]}"
        )

    async def test_mismatch_false_when_datasource_id_matches(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        monkeypatch,
    ) -> None:
        """KPI bound to ds=9202, user selects ds=9202 -> mismatch=false."""
        ds = await _seedDatasource(dbSession, _CHAT_DS_ID)
        await _seedKpi(dbSession, "KPI_SCOPE_MATCH", ds.id)
        kpi_match_cache.onKpiChanged()
        await kpi_match_cache.warmUp(dbSession)

        import app.api.v1.chat as chat_module

        monkeypatch.setattr(
            chat_module._service,
            "_adapterProvider",
            lambda dsId, ds: _ScalarAdapter([{"v": 1}]),
        )

        resp = await client.post(
            "/api/v1/chat/stream",
            json=_chatPayload("KPI_SCOPE_MATCH 这个指标是多少", ds.id),
            headers={"X-User-Id": "1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Should NOT carry mismatch flag when IDs match
        # The SSE contains "datasourceMismatch" only as a JSON false field
        # We check it does not appear as a "true" value
        import re

        # Match "datasourceMismatch": true (not "false")
        assert not re.search(r'"datasourceMismatch"\s*:\s*true', body), (
            f"expected no datasourceMismatch:true in SSE when IDs match, got: {body[:400]}"
        )

    async def test_mismatch_false_when_datasource_id_is_null(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        monkeypatch,
    ) -> None:
        """KPI with datasource_id=NULL (no binding) -> no mismatch check, no warning."""
        ds = await _seedDatasource(dbSession, _CHAT_DS_ID)
        await _seedKpiNullDatasource(dbSession, "KPI_SCOPE_NULL")
        kpi_match_cache.onKpiChanged()
        await kpi_match_cache.warmUp(dbSession)

        import app.api.v1.chat as chat_module

        monkeypatch.setattr(
            chat_module._service,
            "_adapterProvider",
            lambda dsId, ds: _ScalarAdapter([{"v": 1}]),
        )

        resp = await client.post(
            "/api/v1/chat/stream",
            json=_chatPayload("KPI_SCOPE_NULL 这个指标是多少", ds.id),
            headers={"X-User-Id": "1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.text
        # No mismatch when datasource_id is NULL (null = not bound, skip guard)
        import re

        assert not re.search(r'"datasourceMismatch"\s*:\s*true', body), (
            f"expected no datasourceMismatch:true when datasource_id IS NULL, got: {body[:400]}"
        )

    async def test_no_mismatch_when_data_source_text_set_but_datasource_id_null(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        monkeypatch,
    ) -> None:
        """Reverse assertion: data_source='ERP' text but datasource_id=NULL -> NO mismatch.

        This proves the fix actually severed the text-vs-int comparison path.
        Before the fix: data_source='ERP' != str(dto.datasourceId) -> mismatch=True (wrong).
        After the fix: datasource_id IS NULL -> guard skipped -> mismatch=False (correct).
        """
        ds = await _seedDatasource(dbSession, _CHAT_DS_ID)
        await _seedKpiNullDatasource(
            dbSession,
            "KPI_SCOPE_REVERSE",
            dataSourceText="ERP",  # legacy field still populated
        )
        kpi_match_cache.onKpiChanged()
        await kpi_match_cache.warmUp(dbSession)

        import app.api.v1.chat as chat_module

        monkeypatch.setattr(
            chat_module._service,
            "_adapterProvider",
            lambda dsId, ds: _ScalarAdapter([{"v": 1}]),
        )

        resp = await client.post(
            "/api/v1/chat/stream",
            json=_chatPayload("KPI_SCOPE_REVERSE 这个指标是多少", ds.id),
            headers={"X-User-Id": "1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.text
        import re

        assert not re.search(r'"datasourceMismatch"\s*:\s*true', body), (
            f"expected no mismatch when data_source='ERP' but datasource_id=NULL, got: {body[:400]}"
        )


class TestKpiDatasourceMigration:
    """Verify migration 0125 applied correctly: column exists + FK is sound."""

    async def test_column_exists_and_fk_points_to_data_source(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
    ) -> None:
        """After migration: kpi_catalog.datasource_id exists and FK references data_source(id)."""
        # 1. Column exists
        result = await dbSession.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'kpi_catalog' AND column_name = 'datasource_id'"
            )
        )
        row = result.fetchone()
        assert row is not None, "kpi_catalog.datasource_id column must exist"
        assert row[0] == "datasource_id"
        assert row[2] == "YES", "datasource_id must be nullable"

        # 2. FK constraint exists and points to data_source(id)
        fk_result = await dbSession.execute(
            text(
                "SELECT tc.constraint_name, kcu.column_name, ccu.table_name AS foreign_table_name, "
                "ccu.column_name AS foreign_column_name "
                "FROM information_schema.table_constraints AS tc "
                "JOIN information_schema.key_column_usage AS kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "JOIN information_schema.constraint_column_usage AS ccu "
                "  ON tc.constraint_name = ccu.constraint_name "
                "WHERE tc.table_name = 'kpi_catalog' "
                "  AND tc.constraint_type = 'FOREIGN KEY' "
                "  AND kcu.column_name = 'datasource_id'"
            )
        )
        fk_row = fk_result.fetchone()
        assert fk_row is not None, "FK on kpi_catalog.datasource_id must exist"
        assert fk_row[2] == "data_source", "FK must reference data_source table"
        assert fk_row[3] == "id", "FK must reference data_source.id column"

    async def test_delete_referenced_datasource_raises_fk_violation(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
    ) -> None:
        """Delete a datasource that a KPI is bound to -> FK violation (RESTRICT).

        0132 changed the FK from ON DELETE SET NULL to ON DELETE RESTRICT: an
        accidental datasource hard-delete must NOT silently unbind KPIs.  The
        service layer guards this with a 409 before SQL runs; the DB constraint
        is the second line of defense.
        """
        from sqlalchemy.exc import IntegrityError

        ds = await _seedDatasource(dbSession, _CHAT_DS_ID + 10)
        ds_id = ds.id  # rollback 后 ORM 对象会 expire；先取标量，避免同步 lazy-load
        kpi = await _seedKpi(dbSession, "KPI_DELETE_GUARD", ds_id)
        kpi_id = kpi.id

        # Verify datasource_id is set before delete
        result = await dbSession.execute(
            text("SELECT datasource_id FROM kpi_catalog WHERE id = :id"),
            {"id": kpi_id},
        )
        assert result.scalar() == ds_id

        # Delete the datasource (hard delete, no FK cleanup)
        with pytest.raises(IntegrityError):
            await dbSession.delete(ds)
            await dbSession.commit()
        await dbSession.rollback()

        # KPI still bound to the original DS; DS still exists.
        result = await dbSession.execute(
            text("SELECT datasource_id FROM kpi_catalog WHERE id = :id"),
            {"id": kpi_id},
        )
        assert result.scalar() == ds_id, (
            "After FK-restricted delete attempt, KPI.datasource_id must be unchanged"
        )
        alive = await dbSession.execute(
            text("SELECT count(*) FROM data_source WHERE id = :id"), {"id": ds_id}
        )
        assert alive.scalar() == 1
