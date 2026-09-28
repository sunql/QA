"""wiki-ontology-link Task 6: WikiInjector NL2SQL pipeline integration E2E tests.

Run: cd backend && pytest app/tests/integration/test_wiki_link_injector_e2e.py -v
"""
import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    DataSource,
    LlmConfig,
    Nl2sqlWikiTrace,
    OntologyClass,
    WikiOntologyLink,
    WikiPage,
)
from app.infrastructure.llm.base_client import LlmMessage, LlmResponse
from app.models.system_config import SystemConfig

pytestmark = pytest.mark.asyncio


class _FakeLlmClient:
    """Fake LLM that alternates plan/sql responses."""

    _PLAN_JSON = (
        '{"target":"供应商准时交付率","interpretation":"计算供应商准时交付率",'
        '"selectedClasses":["DWD_PO"],"selectedProperties":[],'
        '"conditions":[],"aggregations":[],'
        '"groupBy":[],"joins":[],"sortBy":[],"partitionBy":[],'
        '"rowLimit":100}'
    )
    _SQL = "SELECT 1 AS result LIMIT 1"

    def __init__(self) -> None:
        self.calls = 0
        self.last_messages: list[LlmMessage] = []

    async def complete(self, messages: list[LlmMessage], **kwargs: Any) -> LlmResponse:
        self.calls += 1
        self.last_messages = list(messages)
        content = self._PLAN_JSON if self.calls <= 2 else self._SQL
        return LlmResponse(
            content=content,
            modelName="fake-model",
            promptTokens=100,
            completionTokens=50,
            totalTokens=150,
        )


class _FakeAdapter:
    """Fake business DB adapter that returns empty results."""

    async def execute(self, sql: str, params: Any = None) -> list:
        return []

    async def execute_read_only(self, sql: str, params: Any = None) -> list:
        return []

    async def get_adapter_type(self) -> str:
        return "postgresql"


class _StubEmbeddingService:
    """Stub embedding service that returns fixed vectors."""

    async def generateEmbedding(self, text: str) -> list[float]:
        seed = sum(ord(c) for c in text) % 100
        return [float(seed) / 100.0 + 0.001 * i for i in range(1024)]

    async def embed(self, texts: list[str]) -> list[float]:
        return [0.0] * 8

    async def storeQueryEmbedding(self, **kwargs) -> None:
        return None

    async def searchSimilarQueries(
        self,
        question: str,
        *,
        topK: int = 3,
        datasourceId: int | None = None,
    ) -> list[Any]:
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def datasource_seed(dbSession: AsyncSession) -> None:
    """Seed a DataSource with id=1 (required for chat endpoint)."""
    row = (await dbSession.execute(select(DataSource).where(DataSource.id == 1))).scalar_one_or_none()
    if row is None:
        dbSession.add(DataSource(
            id=1,
            name="Test DataSource",
            type="postgresql",
            username="qa_user",
            password_encrypted="dummy",
            host="localhost",
            port=5432,
            database_name="qa_metadata",
            is_default=True,
        ))
    await dbSession.commit()


@pytest.fixture
async def wiki_page_seed(dbSession: AsyncSession) -> None:
    """Seed wiki pages for link FK (wp001 used for valid links, wp-stale for stale tests)."""
    row = (await dbSession.execute(select(WikiPage).where(WikiPage.page_id == "wp001"))).scalar_one_or_none()
    if row is None:
        dbSession.add(WikiPage(
            page_id="wp001",
            title="供应商交付规则",
            content="准时交付率=准时数/总数",
            status="published",
        ))
    row2 = (await dbSession.execute(select(WikiPage).where(WikiPage.page_id == "wp-stale"))).scalar_one_or_none()
    if row2 is None:
        dbSession.add(WikiPage(
            page_id="wp-stale",
            title="Stale Rules",
            content="stale content",
            status="published",
        ))
    row3 = (await dbSession.execute(select(WikiPage).where(WikiPage.page_id == "wp-concurrent"))).scalar_one_or_none()
    if row3 is None:
        dbSession.add(WikiPage(
            page_id="wp-concurrent",
            title="Concurrent Page",
            content="concurrent",
            status="published",
        ))
    await dbSession.commit()


@pytest.fixture
async def ontology_class_seed(dbSession: AsyncSession) -> None:
    """Seed an ontology class for linking."""
    row = (await dbSession.execute(select(OntologyClass).where(OntologyClass.id == 201))).scalar_one_or_none()
    if row is None:
        dbSession.add(OntologyClass(
            id=201,
            class_name="DWD_PO",
            source_table="DWD_PO",
            version=1,
            valid_from=datetime.now(),
        ))
    await dbSession.commit()


@pytest.fixture
async def wiki_link_seed(dbSession: AsyncSession) -> None:
    """Seed a wiki-ontology link (depends on wiki_page and ontology_class)."""
    row = (await dbSession.execute(
        select(WikiOntologyLink).where(
            WikiOntologyLink.page_id == "wp001",
            WikiOntologyLink.ontology_type == "class",
            WikiOntologyLink.ontology_id == 201,
        )
    )).scalar_one_or_none()
    if row is None:
        dbSession.add(WikiOntologyLink(
            page_id="wp001",
            chunk_id=None,
            ontology_type="class",
            ontology_id=201,
            weight=Decimal("1.0"),
            note="交付规则",
            created_by=1,
        ))
    await dbSession.commit()


@pytest.fixture
async def model_config_seed(dbSession: AsyncSession) -> None:
    """Seed a model config for chat routing."""
    row = (await dbSession.execute(select(LlmConfig).where(LlmConfig.id == 9999))).scalar_one_or_none()
    if row is None:
        dbSession.add(LlmConfig(
            id=9999,
            model_name="wiki-test-model",
            provider="openai_compatible_proxy",
            is_active=True,
            cost_per_1k_input=Decimal("0.001"),
            cost_per_1k_output=Decimal("0.002"),
        ))
    await dbSession.commit()


class _RouterForConfig:
    """Fake router that always selects a given config."""

    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    def selectModel(self, candidates: list[LlmConfig], question: str, ctx: Any) -> LlmConfig:
        return self._config

    def selectFallbackModel(self, configs: list[LlmConfig], excludeId: int | None) -> LlmConfig | None:
        return None


@pytest.fixture
async def client_with_fakes(
    client: AsyncClient,
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    datasource_seed: None,  # noqa: ARG001
    wiki_page_seed: None,  # noqa: ARG001
    ontology_class_seed: None,  # noqa: ARG001
    wiki_link_seed: None,  # noqa: ARG001
    model_config_seed: None,  # noqa: ARG001
) -> AsyncClient:
    """Wire ChatService with fake LLM, adapter, embedding, and router."""
    import app.api.v1.chat as chatModule

    fake_llm = _FakeLlmClient()

    config = (await dbSession.execute(select(LlmConfig).where(LlmConfig.id == 9999))).scalar_one()

    monkeypatch.setattr(chatModule._service, "_modelRouter", _RouterForConfig(config))
    monkeypatch.setattr(chatModule._service, "_llmFactory", lambda cfg: fake_llm)
    monkeypatch.setattr(chatModule._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chatModule._service, "_embedding", _StubEmbeddingService())

    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_full_pipeline_injects_wiki_rule(
    client_with_fakes: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """Full pipeline: wiki link exists → wiki block injected → trace recorded."""
    resp = await client_with_fakes.post(
        "/api/v1/chat",
        json={
            "question": "上个月供应商准时交付率",
            "datasourceId": 1,
            "sessionId": "s-wiki-001",
        },
        headers={"X-User-Id": "1", "X-User-Roles": "admin"},
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"

    # Verify trace was written
    traces = (await dbSession.execute(
        select(Nl2sqlWikiTrace).where(Nl2sqlWikiTrace.session_id == "s-wiki-001")
    )).scalars().all()
    assert len(traces) >= 1, f"Expected at least 1 trace row, got {len(traces)}"
    assert traces[0].page_id == "wp001"
    assert traces[0].ontology_id == 201


async def test_disabled_returns_no_trace(
    client_with_fakes: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """WIKI_INJECTION_ENABLED=false → no trace written."""
    # Set flag to false
    row = (await dbSession.execute(
        select(SystemConfig).where(SystemConfig.key == "WIKI_INJECTION_ENABLED")
    )).scalar_one_or_none()
    if row:
        row.value = "false"
    else:
        dbSession.add(SystemConfig(key="WIKI_INJECTION_ENABLED", value="false"))
    await dbSession.commit()

    resp = await client_with_fakes.post(
        "/api/v1/chat",
        json={
            "question": "上个月供应商准时交付率",
            "datasourceId": 1,
            "sessionId": "s-wiki-disabled",
        },
        headers={"X-User-Id": "1", "X-User-Roles": "admin"},
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"

    traces = (await dbSession.execute(
        select(Nl2sqlWikiTrace).where(Nl2sqlWikiTrace.session_id == "s-wiki-disabled")
    )).scalars().all()
    assert len(traces) == 0


async def test_wiki_trace_records_correct_fields(
    client_with_fakes: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """Trace row has correct fields populated."""
    resp = await client_with_fakes.post(
        "/api/v1/chat",
        json={
            "question": "供应商准时交付率",
            "datasourceId": 1,
            "sessionId": "s-wiki-trace-fields",
        },
        headers={"X-User-Id": "1", "X-User-Roles": "admin"},
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"

    traces = (await dbSession.execute(
        select(Nl2sqlWikiTrace).where(Nl2sqlWikiTrace.session_id == "s-wiki-trace-fields")
    )).scalars().all()
    assert len(traces) >= 1
    t = traces[0]
    assert t.session_id == "s-wiki-trace-fields"
    assert t.question is not None
    assert t.ontology_type in ("class", "property")
    assert t.ontology_id > 0
    assert t.page_id is not None
    assert t.prompt_position == "after_context"
    assert t.injected_chars >= 0
    assert t.score >= 0


async def test_acl_blocks_non_admin(client: AsyncClient) -> None:
    """Non-admin user gets 403 on admin wiki-link API."""
    resp = await client.get(
        "/api/v1/admin/wiki-links",
        headers={"X-User-Id": "1", "X-User-Roles": "user"},
    )
    assert resp.status_code == 403
