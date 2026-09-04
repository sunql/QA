"""chat_service AGENT_RUN record-on-finish — 4 cases.

Tests audit writes on agent run completion (not CRUD — fire-and-forget on finish).
Covers:
1. Agent run completes successfully → AGENT_RUN CREATE with status=SUCCESS
2. Agent run fails → AGENT_RUN CREATE with status=FAILED (finally block fires)
3. actor + actor_departments captured
4. Querying by entity_type=agent_run_log returns the run record

These tests use real PostgreSQL (pgApiClient fixture from _pg_support.py).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    AgentPermission,
    AgentResponseLatency,
    AgentStatus,
    AgentTriggerType,
    FeatureRefreshFrequency,
    FeatureStatus,
    MatchRule,
    SourceSystem,
)
from app.domain.schemas import AgentAccessPolicyCreate, AgentDefinitionCreate
from app.infrastructure import database as dbModule
from app.services.agent_registry_service import AgentRegistryService
from app.workers.audit_worker import AuditWorker

_ADMIN = {"X-User-Id": "audit-chat-agent-run", "X-User-Tenant": "default"}

_CHAT_DATASOURCE_ID = 9602


async def _seedDatasource(dbSession: AsyncSession) -> None:
    from app.domain.models import DataSource
    ds = DataSource(
        id=_CHAT_DATASOURCE_ID,
        name="ds-chat-agent-audit",
        type="postgresql",
        host="localhost",
        port=5432,
        database_name="x",
        username="u",
        password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.commit()


async def _seedAgent(
    dbSession: AsyncSession,
    code: str,
    *,
    status: AgentStatus = AgentStatus.ACTIVE,
    policies: list[AgentAccessPolicyCreate] | None = None,
) -> None:
    service = AgentRegistryService()
    dto = AgentDefinitionCreate(
        agent_code=code,
        agent_name=f"{code} Audit Test",
        description="audit test agent",
        trigger_type=AgentTriggerType.USER_QUESTION,
        response_latency=AgentResponseLatency.REALTIME,
        data_domains=["PROCUREMENT"],
        data_layers=["FEATURE"],
        status=status,
        version="v1.0",
        policies=policies
        if policies is not None
        else [
            AgentAccessPolicyCreate(
                data_object="SUPPLIER",
                permission=AgentPermission.READ,
                data_layer=None,
                notes="audit test",
            )
        ],
    )
    await service.createAgent(dbSession, dto, _ADMIN)


async def _seedSupplier(dbSession: AsyncSession, key: int, code: str) -> None:
    from app.domain.models import EntityMapping
    dbSession.add(
        EntityMapping(
            entity_type="SUPPLIER",
            enterprise_key=key,
            enterprise_code=code,
            source_system=SourceSystem.ERP,
            source_key=f"V{key}",
            source_code=f"V{key}",
            match_rule=MatchRule.MDM_MASTER,
            owner="procurement",
        )
    )
    await dbSession.commit()


async def _seedFeatureAndValue(
    dbSession: AsyncSession,
    feature_id: int,
    feature_name: str,
    code: str,
    value: float,
    *,
    unit: str = "%",
    window: str = "3M",
) -> None:
    from datetime import date, datetime, timezone
    from app.domain.models import FeatureDefinition, FeatureValue
    dbSession.add(
        FeatureDefinition(
            id=feature_id,
            feature_name=feature_name,
            feature_alias=feature_name,
            feature_definition="auto",
            entity_type="SUPPLIER",
            calculation_logic="SELECT 1",
            window_size=window,
            refresh_frequency=FeatureRefreshFrequency.DAILY,
            unit=unit,
            status=FeatureStatus.ACTIVE,
            is_enabled=True,
            datasource_id=_CHAT_DATASOURCE_ID,
            version="v1.0",
        )
    )
    dbSession.add(
        FeatureValue(
            feature_id=feature_id,
            entity_key=code,
            value=value,
            valid_at=date(2026, 8, 31),
            computed_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
    )
    await dbSession.commit()


# ---------------------------------------------------------------------------
# Chat fakes (same as test_chat_agent_run.py)
# ---------------------------------------------------------------------------


class _StubEmbedding:
    async def embed(self, text):  # noqa: ARG002
        return [0.0] * 8
    async def storeQueryEmbedding(self, **kwargs):  # noqa: ARG002
        return None
    async def searchSimilarQueries(self, *args, **kwargs):  # noqa: ARG002
        return []


class _FakeAdapter:
    async def execute_read_only(self, sql):  # noqa: ARG002
        return []


class _NoopLlm:
    @staticmethod
    def _content(msg) -> str:
        if isinstance(msg, dict):
            return msg.get("content", "")
        return getattr(msg, "content", "")

    async def complete(self, messages, **kwargs):  # noqa: ARG002
        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 1
            completionTokens = 1
            cost = 0.0
        return _Resp()

    async def completeStream(self, messages, **kwargs):  # noqa: ARG002
        class _Chunk:
            content = ""
            modelName = "test-model"
            promptTokens = 1
            completionTokens = 1
            cost = 0.0
            done = True
        yield _Chunk()


async def _setupChatFakes(monkeypatch, dbSession: AsyncSession) -> None:
    """Replace ChatService dependencies with noop fakes (same as test_chat_agent_run.py)."""
    from app.services import chat_service as chatSvcMod
    monkeypatch.setattr(chatSvcMod, "_StubEmbedding", _StubEmbedding, raising=False)
    monkeypatch.setattr(chatSvcMod, "_FakeAdapter", _FakeAdapter, raising=False)
    monkeypatch.setattr(chatSvcMod, "_NoopLlm", _NoopLlm, raising=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestChatAgentRunAudit:
    """4 cases: SUCCESS / FAILED / actor_departments / query by entity_type."""

    async def test_agent_run_success_writes_audit(
        self, client: AsyncClient, dbSession: AsyncSession, monkeypatch
    ) -> None:
        """Agent run completes successfully → AGENT_RUN CREATE audit with status=SUCCESS."""
        await _seedDatasource(dbSession)
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        await _seedSupplier(dbSession, 100001, "SUPAUD001")
        await _seedFeatureAndValue(
            dbSession, 1, "SUPPLIER_RISK_SCORE", "SUPAUD001", 0.50,
            unit="score", window="12M",
        )
        await _setupChatFakes(monkeypatch, dbSession)

        resp = await client.post(
            "/api/v1/chat",
            headers={**_ADMIN, "X-User-Departments": "采购部,研发部"},
            json={
                "sessionId": "test-audit-success",
                "question": "用 supplier_risk_agent 评估供应商 100001",
                "datasourceId": _CHAT_DATASOURCE_ID,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "agent_run"
        assert body["agentRun"] is not None

        # Drain outbox
        await AuditWorker().drainOnce(dbSession)

        # Query audit log
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_run_log&action=CREATE",
            headers=_ADMIN,
        )
        rows = audit_resp.json()["rows"]
        matching = [
            r for r in rows
            if r.get("afterJson", {}).get("agentCode") == "SUPPLIER_RISK_AGENT"
        ]
        assert len(matching) >= 1, f"No AGENT_RUN CREATE audit found. Available rows: {rows}"
        record = matching[0]
        assert record["action"] == "CREATE"
        after = record.get("afterJson", {})
        assert after.get("status") == "SUCCESS", f"Expected SUCCESS, got {after.get('status')}"
        assert after.get("agentCode") == "SUPPLIER_RISK_AGENT"
        assert "SUPAUD001" in after.get("answer", "")

    async def test_agent_run_failure_writes_audit(
        self, client: AsyncClient, dbSession: AsyncSession, monkeypatch
    ) -> None:
        """Agent run fails → AGENT_RUN CREATE audit with status=FAILED (finally block fires)."""
        await _seedDatasource(dbSession)
        # Seed a DRAFT agent — chat_service catches ConflictError and returns friendly answer
        await _seedAgent(dbSession, "DRAFT_AGENT", status=AgentStatus.DRAFT)

        await _setupChatFakes(monkeypatch, dbSession)

        resp = await client.post(
            "/api/v1/chat",
            headers={**_ADMIN, "X-User-Departments": "财务部"},
            json={
                "sessionId": "test-audit-failure",
                "question": "用 draft_agent 评估供应商 100001",
                "datasourceId": _CHAT_DATASOURCE_ID,
            },
        )
        # Returns 200 with friendly answer (chat_service catches and degrades)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "agent_run"
        assert body["agentRun"] is None  # degraded, no agent_run in response

        # Drain outbox
        await AuditWorker().drainOnce(dbSession)

        # Query audit log — should have FAILED record even though exception was caught
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_run_log&action=CREATE",
            headers=_ADMIN,
        )
        rows = audit_resp.json()["rows"]
        # The failed run should still produce an audit record with status=FAILED
        matching = [
            r for r in rows
            if r.get("afterJson", {}).get("agentCode") == "DRAFT_AGENT"
            and r.get("afterJson", {}).get("status") == "FAILED"
        ]
        assert len(matching) >= 1, (
            f"No AGENT_RUN CREATE audit with status=FAILED for DRAFT_AGENT. "
            f"Available rows: {rows}"
        )

    async def test_agent_run_actor_departments_injected(
        self, client: AsyncClient, dbSession: AsyncSession, monkeypatch
    ) -> None:
        """actor + actor_departments captured in AGENT_RUN audit record."""
        await _seedDatasource(dbSession)
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        await _seedSupplier(dbSession, 100002, "SUPAUD002")
        await _seedFeatureAndValue(
            dbSession, 2, "SUPPLIER_RISK_SCORE", "SUPAUD002", 0.30,
            unit="score", window="12M",
        )
        await _setupChatFakes(monkeypatch, dbSession)

        headers = {**_ADMIN, "X-User-Departments": "采购部,财务部,研发部"}
        resp = await client.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "sessionId": "test-audit-actor-dept",
                "question": "用 supplier_risk_agent 评估供应商 100002",
                "datasourceId": _CHAT_DATASOURCE_ID,
            },
        )
        assert resp.status_code == 200, resp.text

        await AuditWorker().drainOnce(dbSession)

        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_run_log",
            headers=_ADMIN,
        )
        rows = audit_resp.json()["rows"]
        matching = [
            r for r in rows
            if r.get("actor") == "audit-chat-agent-run"
            and "采购" in (r.get("actorDepartments") or "")
        ]
        assert len(matching) >= 1, (
            f"No audit record with actor=audit-chat-agent-run and departments containing 采购. "
            f"Available rows: {rows}"
        )

    async def test_agent_run_query_by_entity_type(
        self, client: AsyncClient, dbSession: AsyncSession, monkeypatch
    ) -> None:
        """Querying by entity_type=agent_run_log returns the run record."""
        await _seedDatasource(dbSession)
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        await _seedSupplier(dbSession, 100003, "SUPAUD003")
        await _seedFeatureAndValue(
            dbSession, 3, "SUPPLIER_RISK_SCORE", "SUPAUD003", 0.70,
            unit="score", window="12M",
        )
        await _setupChatFakes(monkeypatch, dbSession)

        resp = await client.post(
            "/api/v1/chat",
            headers=_ADMIN,
            json={
                "sessionId": "test-audit-query-entity",
                "question": "用 supplier_risk_agent 评估供应商 100003",
                "datasourceId": _CHAT_DATASOURCE_ID,
            },
        )
        assert resp.status_code == 200, resp.text

        await AuditWorker().drainOnce(dbSession)

        # Query specifically by entity_type=agent_run_log
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_run_log",
            headers=_ADMIN,
        )
        assert audit_resp.status_code == 200, audit_resp.text
        rows = audit_resp.json()["rows"]
        agent_run_rows = [r for r in rows if r.get("entityType") == "agent_run_log"]
        assert len(agent_run_rows) >= 1, f"No agent_run_log records found. Available: {rows}"
        assert all(r["action"] == "CREATE" for r in agent_run_rows)
