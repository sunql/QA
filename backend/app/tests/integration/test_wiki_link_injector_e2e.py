"""wiki-ontology-link Task 6: WikiInjector NL2SQL pipeline integration E2E tests.

Run: cd backend && pytest app/tests/integration/test_wiki_link_injector_e2e.py -v
"""
import asyncio
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
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


class _RecordingOntology:
    """包住真实 OntologyService，只拦截 searchByKeyword 并记录 typeFilter。

    类召回继续走真实实现，只有 property / metric 这两个新增的按需召回被替换成可控结果。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[str | None] = []
        self.hits: dict[str, list[Any]] = {}
        self.raiseOn: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def searchByKeyword(self, query, *, topK=5, typeFilter=None):
        self.calls.append(typeFilter)
        if typeFilter in self.raiseOn:
            raise RuntimeError("recall boom")
        if typeFilter in self.hits:
            return list(self.hits[typeFilter])
        return await self._inner.searchByKeyword(query, topK=topK, typeFilter=typeFilter)


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
async def property_link_seed(
    dbSession: AsyncSession,
    wiki_page_seed: None,  # noqa: ARG001 —— 显式声明 FK 前置：link.page_id → wiki_page
) -> None:
    """Seed a *property* wiki link —— 证明 property 类型真的会生效（修复前它死链）。"""
    row = (await dbSession.execute(
        select(WikiOntologyLink).where(
            WikiOntologyLink.page_id == "wp001",
            WikiOntologyLink.ontology_type == "property",
            WikiOntologyLink.ontology_id == 401,
        )
    )).scalar_one_or_none()
    if row is None:
        dbSession.add(WikiOntologyLink(
            page_id="wp001", chunk_id=None, ontology_type="property",
            ontology_id=401, weight=Decimal("1.0"), note="属性口径", created_by=1,
        ))
    await dbSession.commit()


@pytest.fixture
async def recording_ontology(
    client_with_fakes: AsyncClient,  # noqa: ARG001 —— 触发 _service 装配
    monkeypatch: pytest.MonkeyPatch,
) -> _RecordingOntology:
    """把 _service._ontology 换成记录型替身。

    **必须走 monkeypatch**（与同文件 `client_with_fakes` 的 `_modelRouter` / `_embedding`
    一致）：裸赋值 `chatModule._service._ontology = recorder` 不会还原，`hits` /
    `raiseOn` 会泄漏到后续用例 —— 尤其 `test_extra_recall_skipped_when_no_extra_links`
    会被前一个用例留下的 `hits["property"]` 假命中，成本守卫静默失效。
    """
    import app.api.v1.chat as chatModule
    recorder = _RecordingOntology(chatModule._service._ontology)
    monkeypatch.setattr(chatModule._service, "_ontology", recorder)
    return recorder


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
    # 绑的是 class 链接（wiki_link_seed），trace 必须记下同一个类型 ——
    # 原断言 `in ("class","property")` 由 DB CHECK 约束保证恒真，等于没测。
    assert t.ontology_type == "class"
    assert t.ontology_id > 0
    assert t.page_id is not None
    assert t.prompt_position == "after_context"
    assert t.injected_chars >= 0
    assert t.score >= 0


async def test_extra_recall_skipped_when_no_extra_links(
    client_with_fakes: AsyncClient,
    recording_ontology: _RecordingOntology,
    dbSession: AsyncSession,  # noqa: ARG001 —— 由 client fixture 触发 truncate
) -> None:
    """成本回归守卫：库里没有 property/metric 链接时，绝不发起额外语义召回。

    这一条守的是「按需付费」这个设计本身。少了它，将来有人把
    listConfiguredOntologyTypes 的判断删掉，每轮 chat 都会白花一次 embedding +
    一次 Milvus 检索，而其它用例全是绿的。
    """
    resp = await client_with_fakes.post(
        "/api/v1/chat",
        json={"question": "上个月供应商准时交付率", "datasourceId": 1,
              "sessionId": "s-wiki-cost-guard"},
        headers={"X-User-Id": "1", "X-User-Roles": "admin"},
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    assert "property" not in recording_ontology.calls
    assert "metric" not in recording_ontology.calls


async def test_property_link_injected_when_property_recalled(
    client_with_fakes: AsyncClient,
    recording_ontology: _RecordingOntology,
    property_link_seed: None,
    dbSession: AsyncSession,
) -> None:
    """property 链接在其属性被召回时必须注入（修复前：恒不注入）。"""
    recording_ontology.hits["property"] = [
        SimpleNamespace(id=401, score=0.9),
    ]

    resp = await client_with_fakes.post(
        "/api/v1/chat",
        json={"question": "上个月供应商准时交付率", "datasourceId": 1,
              "sessionId": "s-wiki-property"},
        headers={"X-User-Id": "1", "X-User-Roles": "admin"},
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    assert recording_ontology.calls.count("property") == 1

    traces = (await dbSession.execute(
        select(Nl2sqlWikiTrace).where(
            Nl2sqlWikiTrace.session_id == "s-wiki-property",
            Nl2sqlWikiTrace.ontology_type == "property",
        )
    )).scalars().all()
    assert len(traces) == 1
    assert traces[0].ontology_id == 401
    assert traces[0].page_id == "wp001"


async def test_property_link_skipped_when_property_not_recalled(
    client_with_fakes: AsyncClient,
    recording_ontology: _RecordingOntology,
    property_link_seed: None,
    dbSession: AsyncSession,
) -> None:
    """配了属性链接、但该属性没被召回 → 不注入（不能退化成「配了就无脑注入」）。"""
    recording_ontology.hits["property"] = [SimpleNamespace(id=999, score=0.9)]

    resp = await client_with_fakes.post(
        "/api/v1/chat",
        json={"question": "上个月供应商准时交付率", "datasourceId": 1,
              "sessionId": "s-wiki-property-miss"},
        headers={"X-User-Id": "1", "X-User-Roles": "admin"},
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"

    traces = (await dbSession.execute(
        select(Nl2sqlWikiTrace).where(
            Nl2sqlWikiTrace.session_id == "s-wiki-property-miss",
            Nl2sqlWikiTrace.ontology_type == "property",
        )
    )).scalars().all()
    assert traces == []


async def test_property_recall_failure_does_not_block_class_injection(
    client_with_fakes: AsyncClient,
    recording_ontology: _RecordingOntology,
    property_link_seed: None,
    dbSession: AsyncSession,
) -> None:
    """属性召回抛异常 → 只跳过 property，class 链接照常注入，且不抛到调用方。

    失败隔离的双向断言：坏输入（抛异常的 property）被降级处理，
    正确输入（class 链接）不受牵连。
    """
    recording_ontology.raiseOn = {"property"}

    resp = await client_with_fakes.post(
        "/api/v1/chat",
        json={"question": "上个月供应商准时交付率", "datasourceId": 1,
              "sessionId": "s-wiki-property-fail"},
        headers={"X-User-Id": "1", "X-User-Roles": "admin"},
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"

    class_traces = (await dbSession.execute(
        select(Nl2sqlWikiTrace).where(
            Nl2sqlWikiTrace.session_id == "s-wiki-property-fail",
            Nl2sqlWikiTrace.ontology_type == "class",
        )
    )).scalars().all()
    assert len(class_traces) >= 1
    assert class_traces[0].ontology_id == 201


async def test_acl_blocks_non_admin(client: AsyncClient) -> None:
    """Non-admin user gets 403 on admin wiki-link API."""
    resp = await client.get(
        "/api/v1/admin/wiki-links",
        headers={"X-User-Id": "1", "X-User-Roles": "user"},
    )
    assert resp.status_code == 403


async def test_stale_ontology_skipped(
    client_with_fakes: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """Link points to non-existent ontology → no wiki block and no trace for stale target.

    Strategy: seed 2 classes so recall can distinguish (class id=12 linked, 99999 stale).
    Override _ontology._selectRelevantClasses to only return class id=12 (the live one),
    so class id=99999 is never recalled → wiki block empty → no trace for stale link.
    """
    # Seed second class (the "live" one that will be recalled)
    row12 = (await dbSession.execute(
        select(OntologyClass).where(OntologyClass.id == 12)
    )).scalar_one_or_none()
    if row12 is None:
        dbSession.add(OntologyClass(
            id=12,
            class_name="DWD_PO_STALE",
            source_table="DWD_PO",
            version=1,
            valid_from=datetime.now(),
        ))
    # Seed stale link (page wp-stale → ontology_id=99999)
    row_link = (await dbSession.execute(
        select(WikiOntologyLink).where(
            WikiOntologyLink.page_id == "wp-stale",
            WikiOntologyLink.ontology_type == "class",
            WikiOntologyLink.ontology_id == 99999,
        )
    )).scalar_one_or_none()
    if row_link is None:
        dbSession.add(WikiOntologyLink(
            page_id="wp-stale",
            chunk_id=None,
            ontology_type="class",
            ontology_id=99999,
            weight=Decimal("1.0"),
            note="stale",
            created_by=1,
        ))
    await dbSession.commit()

    # Override _selectRelevantClasses to only return class id=12 (the linked one)
    import app.api.v1.chat as chatModule

    class _SelectiveRecallFake:
        """Fake ontology service that returns only class id=12."""

        def __init__(self, real_ontology: Any) -> None:
            self._real = real_ontology

        async def listClasses(self, session: AsyncSession) -> list[Any]:
            return await self._real.listClasses(session)

        async def _selectRelevantClasses(
            self, session: AsyncSession, question: str, allClasses: list[Any],
        ) -> tuple[list[Any], Any]:
            from app.domain.schemas import ClassRecallInfo
            filtered = [c for c in allClasses if c.id == 12]
            return filtered, ClassRecallInfo(
                mode="recall", hitCount=len(filtered),
                classCount=len(filtered), truncated=False,
            )

        async def listJoins(self, session: AsyncSession) -> list[Any]:
            return await self._real.listJoins(session)

    # Get the real ontology service from the chat service
    real_ontology = chatModule._service._ontology
    selective = _SelectiveRecallFake(real_ontology)
    # Replace the _ontology instance variable
    chatModule._service._ontology = selective  # type: ignore

    try:
        resp = await client_with_fakes.post(
            "/api/v1/chat",
            json={
                "question": "供应商准时交付率",
                "datasourceId": 1,
                "sessionId": "s-stale",
            },
            headers={"X-User-Id": "1", "X-User-Roles": "admin"},
        )
        assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"

        # Verify no trace for the stale ontology_id
        traces = (await dbSession.execute(
            select(Nl2sqlWikiTrace).where(
                Nl2sqlWikiTrace.session_id == "s-stale",
                Nl2sqlWikiTrace.ontology_id == 99999,
            )
        )).scalars().all()
        assert len(traces) == 0, f"Expected no trace for stale ontology, got {len(traces)}"
    finally:
        # Restore real ontology
        chatModule._service._ontology = real_ontology  # type: ignore


async def test_concurrent_link_create_no_deadlock(
    dbSession: AsyncSession,
) -> None:
    """Concurrent wiki-link creation at the service level → all succeed (no deadlock).

    Tests that concurrent inserts on the same (page_id, chunk_id, ontology_type,
    ontology_id) are properly serialized by the unique index without deadlocking.
    Uses separate sessions per concurrent task to avoid flush conflicts.
    """
    from app.infrastructure import database as dbModule

    # Ensure page exists
    row = (await dbSession.execute(
        select(WikiPage).where(WikiPage.page_id == "wp-concurrent")
    )).scalar_one_or_none()
    if row is None:
        dbSession.add(WikiPage(
            page_id="wp-concurrent", title="Concurrent", content="c", status="published",
        ))
        await dbSession.commit()

    # Create a minimal actor stub for the service
    class _ActorStub:
        dbUserId = 1
        roles = ["admin"]
        departments = []

    from app.services.wiki_link_service import WikiLinkService

    async def _create_link(note: str) -> Any:
        """Create link using a fresh session per call."""
        factory = dbModule.getSessionFactory()
        async with factory() as sess:
            return await WikiLinkService().createLink(
                session=sess,
                page_id="wp-concurrent",
                chunk_id=None,
                ontology_type="class",
                ontology_id=201,
                weight=Decimal("1.0"),
                note=note,
                actor=_ActorStub(),
            )

    tasks = [_create_link(f"concurrent-{i}") for i in range(10)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        assert not isinstance(r, Exception), f"Unexpected exception: {r}"
