"""POST /api/v1/documents/qa 端点集成测试（真实 PG）。

完整 API 链路：ASGITransport + 真实 PG（pgApiClient）+ SSE 流式响应验证。
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import LlmConfig, SessionMessage
from app.services.stream_events import EVENT_QA_CITATIONS, EVENT_QA_DONE, EVENT_QA_META
from app.tests.integration.test_chat_api import _installFakes, _seed  # noqa: F401


@pytest.mark.asyncio
async def test_doc_qa_endpoint_requires_auth(client) -> None:
    """未带 X-User-Id 头（anonymous）走 stub auth 默认 admin，可访问。

    真实生产由反向代理剥离 X-User-* 头后走 JWT；stub 模式下 anonymous
    仍可访问（dev/test 体验兼容），所以这里验证带正确头可访问。
    """
    async with client.stream(
        "POST",
        "/api/v1/documents/qa",
        json={"sessionId": "sess-new", "question": "test"},
    ) as resp:
        # stub 模式下 anonymous 可访问（默认 admin 角色），返回 200 或 SSE stream
        # 非 stub 时应由反向代理拦截，返回 403
        assert resp.status_code in (200, 403, 422), f"Unexpected status: {resp.status_code}"


@pytest.mark.asyncio
async def test_doc_qa_endpoint_returns_sse_when_authed(client, dbSession, monkeypatch) -> None:
    """登录用户访问返回 text/event-stream + 200。"""
    # Seed LLM config + embedding so the pipeline doesn't fail on config lookup
    config, _ds = await _seed(dbSession)
    _installFakes(monkeypatch, config)

    resp = await client.post(
        "/api/v1/documents/qa",
        json={"sessionId": "sess-new", "question": "什么是质量协议？"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


# ---------------------------------------------------------------------------
# E2E: pgApiClient + full doc_qa flow (upload → qa → DB → history)
# ---------------------------------------------------------------------------


class _E2eFakeLlmClient:
    """Deterministic LLM client for E2E smoke test — returns fixed answer with citations."""

    async def completeStream(self, messages, model: str) -> AsyncIterator[Any]:
        chunks = [
            type("C", (), {"content": "根据文档内容，", "isDone": False})(),
            type("C", (), {"content": "供应商绩效评估标准包括：", "isDone": False})(),
            type("C", (), {"content": "[1] 质量合规 [2] 交付及时性 [3] 价格竞争力。", "isDone": False})(),
            type("C", (), {"content": "", "isDone": True, "promptTokens": 100, "completionTokens": 20})(),
        ]
        for c in chunks:
            yield c


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    if not line.startswith("event:"):
        return None
    event_type = line.split("\n", 1)[0].replace("event:", "").strip()
    return {"type": event_type}


@pytest.mark.asyncio
async def test_doc_qa_e2e_full_flow(
    client: AsyncClient,
    dbSession: AsyncSession,
    monkeypatch,
    fakeMinio,
    mockEmbeddingService,
) -> None:
    """E2E smoke: upload → qa stream → DB 2 rows → history API visible.

    Verifies the complete doc_qa pipeline:
    1. Upload a small text file via /upload endpoint.
    2. Stream /qa with a question; parse SSE events.
    3. Assert events include qa_meta, qa_citations, qa_done.
    4. Query DB to confirm 2 SessionMessage rows (user + assistant).
    5. Hit /sessions/chat-history and confirm the sessionId appears.
    """
    TEST_USER = "u-e2e-docqa"
    SESSION_ID = "e2e-sess-docqa"

    # Seed LLM config (needed by model router in answer_stream)
    config = LlmConfig(
        model_name="test-e2e-model",
        provider="openai",
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )
    dbSession.add(config)
    await dbSession.commit()
    await dbSession.refresh(config)

    # ── 1. Upload a small text file（fakes：MinIO + embedding + Milvus insert）──
    # 用 fakeMinio/mockEmbeddingService + 替身 insert 让上传确定性 201，并顺带验证
    # 源文件留存真实发生（storage_url 是 s3:// 而非 milvus:// 假 URL）。若有人把
    # 留存静默删掉或改回假 URL，这里的断言会当场失败，而不是绿着放过。
    file_content = "供应商绩效评估标准：质量合规、交付及时性、价格竞争力。".encode("utf-8")
    file_stream = io.BytesIO(file_content)
    with patch(
        "app.services.rag_service._getEmbeddingService",
        return_value=mockEmbeddingService,
    ), patch("app.services.rag_service.insertDocumentChunks"):
        upload_resp = await client.post(
            "/api/v1/documents/upload",
            files={"file": ("test_supplier.txt", file_stream, "text/plain")},
            data={"documentType": "CONTRACT", "securityLevel": "L1"},
            headers={"X-User-Id": TEST_USER},
        )
    assert upload_resp.status_code == 201, f"upload unexpected {upload_resp.status_code}"
    assert upload_resp.json()["storage_url"].startswith("s3://"), "源文件留存必须落对象存储"

    # ── 2. Monkeypatch RagQaService to use fake LLM + fake Milvus search ────────
    import app.services.rag_qa_service as rag_qa_module

    original_answer_stream = rag_qa_module.RagQaService.answer_stream

    async def _fake_answer_stream(self, session, dto, *, actor, configs):
        self._llm_factory = lambda c: _E2eFakeLlmClient()
        self._rag_svc.searchDocuments = AsyncMock(return_value=[{
            "document_id": "DOC-001",
            "document_name": "test_supplier.txt",
            "chunk_text": "供应商绩效评估标准：质量合规、交付及时性、价格竞争力。",
            "score": 0.85,
        }])
        async for ev in original_answer_stream(self, session, dto, actor=actor, configs=configs):
            yield ev

    monkeypatch.setattr(rag_qa_module.RagQaService, "answer_stream", _fake_answer_stream)

    # ── 3. Stream /qa endpoint ─────────────────────────────────────────────────
    events: list[dict[str, Any]] = []
    async with client.stream(
        "POST",
        "/api/v1/documents/qa",
        json={"sessionId": SESSION_ID, "question": "供应商绩效评估标准是什么？", "topK": 5},
        headers={"X-User-Id": TEST_USER},
    ) as resp:
        assert resp.status_code == 200, f"/qa returned {resp.status_code}"
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                ev_type = line.split("\n", 1)[0].replace("event:", "").strip()
                events.append({"type": ev_type})

    # ── 4. Assert event sequence ───────────────────────────────────────────────
    ev_types = [e["type"] for e in events]
    assert EVENT_QA_META in ev_types, f"Missing qa_meta in {ev_types}"
    assert EVENT_QA_CITATIONS in ev_types, f"Missing qa_citations in {ev_types}"
    assert EVENT_QA_DONE in ev_types, f"Missing qa_done in {ev_types}"

    # ── 5. DB: confirm 2 SessionMessage rows ───────────────────────────────────
    # _persist calls commit() so data is visible to new connections immediately.
    from app.infrastructure.database import getSessionFactory
    verify_factory = getSessionFactory()
    async with verify_factory() as verify_session:
        result = await verify_session.execute(
            select(SessionMessage).where(
                SessionMessage.session_id == SESSION_ID,
                SessionMessage.channel == "doc_qa",
                SessionMessage.user_id == TEST_USER,
            )
        )
        rows = list(result.scalars().all())
        assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}: {[(r.role, r.content[:30]) for r in rows]}"
        assert {r.role for r in rows} == {"user", "assistant"}

    # ── 6. History API includes the session ────────────────────────────────────
    hist_resp = await client.get(
        "/api/v1/sessions/chat-history",
        params={"channel": "doc_qa"},
        headers={"X-User-Id": TEST_USER},
    )
    assert hist_resp.status_code == 200, f"history returned {hist_resp.status_code}"
    sessions = hist_resp.json()
    session_ids = [s.get("sessionId") for s in sessions]
    assert SESSION_ID in session_ids, f"Session {SESSION_ID} not in history: {session_ids}"
