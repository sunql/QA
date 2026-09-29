"""v3.1 B6（M7 Hypothesis Hook）集成测试（真实 PG + 完整 API 链路）。

覆盖：
- 触发问题全链路（HTTP 非流式）：假设落库 analysis_hypothesis + 响应携带
  hypotheses DTO（camelCase）+ LLM 恰好 +1 次（基线 4 → 5）+ purpose="hypothesis"
  进计量（session_token_usage 出现 hypothesis purpose 行）
- 非触发问题：LLM 计数不变（4 次）、零假设落库、零 hypothesis purpose
- LLM 回复解析为空：best-effort 降级——主回答照常返回、零假设
- 流式（POST /chat/stream）：假设只落库、SSE 帧不含假设内容
- GET /chat/sessions/{sid}/hypotheses：归属校验（他人 403 / admin 放行）、
  camelCase DTO、limit

LLM / adapter 假注入复用 test_chat_api 模式；数据层（假设表/计量/消息）全真实 PG。
"""

from __future__ import annotations

import json
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    AnalysisHypothesis,
    DataSource,
    LlmConfig,
    OntologyClass,
    OntologyProperty,
    SessionMessage,
    SessionTokenUsage,
)
from app.infrastructure.llm.base_client import StreamChunk
from app.infrastructure.security.crypto import encryptApiKey
from app.tests.integration.test_chat_api import _chat_payload, _installFakes, _seed

ROWS = [{"NAME": "A", "QTY": Decimal(10)}, {"NAME": "B", "QTY": Decimal(20)}]

TRIGGER_QUESTION = "本月收货数量为什么下降"
NON_TRIGGER_QUESTION = "各供应商的收货数量汇总"

_HYPOTHESES_JSON = json.dumps([
    {
        "statement": "收货量下降可能与供应商交付延期有关",
        "driver": "QTY",
        "verification_sql": "SELECT NAME, SUM(QTY) AS TOTAL FROM ZJTH.PRECEIPT GROUP BY NAME",
    },
    {
        "statement": "可能与本月工作日天数减少有关",
        "driver": None,
        "verification_sql": "SELECT COUNT(*) AS DAYS FROM CALENDAR",
    },
], ensure_ascii=False)


class _HypothesisLlm:
    """按 prompt 内容路由：假设阶段（system 含固定标记）返回假设 JSON。

    共享实例供调用计数（LLM 预算断言）。hypothesisParseOk 可切换解析失败形态。
    """

    def __init__(self, *, hypothesisParseOk: bool = True) -> None:
        self.calls: list[list[tuple[str, str]]] = []
        self.hypothesisParseOk = hypothesisParseOk

    async def complete(self, messages: list, **kwargs) -> object:
        self.calls.append([(m.role, m.content) for m in messages])
        system = messages[0].content
        user = messages[1].content

        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 10
            completionTokens = 5

        if "可能原因假设" in user:
            _Resp.content = _HYPOTHESES_JSON if self.hypothesisParseOk else "不是 JSON"
        elif "图表类型" in user:
            _Resp.content = '{"title":{"text":"t"},"series":[{"type":"bar","data":[1,2]}]}'
        elif "解析为查询计划" in system:
            _Resp.content = (
                '{"target":"收货数量","selectedClasses":["PRECEIPT"],'
                '"selectedProperties":["NAME","QTY"],"groupBy":["NAME"]}'
            )
        elif "生成 SQL 时必须" in system:
            _Resp.content = (
                "```sql\nSELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT "
                "GROUP BY NAME FETCH FIRST 10 ROWS ONLY\n```"
            )
        else:
            _Resp.content = "查询完成，共 2 条记录。"
        return _Resp()

    async def completeStream(self, messages: list, **kwargs):
        """流式回答：与 test_chat_stream_api 同构（增量 + 末块 token 统计）。"""
        self.calls.append([(m.role, m.content) for m in messages])
        for piece in ("查询完成，", "共 2 条记录。"):
            yield StreamChunk(
                content=piece, isDone=False, promptTokens=0,
                completionTokens=0, modelName="test-model",
            )
        yield StreamChunk(
            content="", isDone=True, promptTokens=10,
            completionTokens=5, modelName="test-model",
        )


async def _seedChatSessionMarker(session: AsyncSession, sessionId: str, userId: str) -> None:
    """写一条带归属的 chat session_message（归属守卫事实源）。"""
    session.add(SessionMessage(
        session_id=sessionId, role="user", content=TRIGGER_QUESTION,
        channel="chat", user_id=userId,
    ))
    await session.commit()


class TestHypothesisFullChain:
    async def test_triggered_question_persists_and_attaches(
        self, client: AsyncClient, dbSession: AsyncSession, monkeypatch,
    ) -> None:
        """触发问题：假设落库 + 响应携带 + LLM 恰好 +1 + purpose=hypothesis 计量。"""
        config, ds = await _seed(dbSession)
        llm = _HypothesisLlm()
        _installFakesWithLlm(monkeypatch, config, llm)

        resp = await client.post(
            "/api/v1/chat", json=_chat_payload(TRIGGER_QUESTION, ds.id, sessionId="hyp-1"),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # 响应携带假设 DTO（camelCase）
        assert body["hypotheses"], "触发问题应返回假设"
        assert len(body["hypotheses"]) == 2
        first = body["hypotheses"][0]
        assert first["statement"].startswith("收货量下降")
        assert first["driver"] == "QTY"
        assert first["verificationSql"].startswith("SELECT")
        assert "turnQuestion" in first

        # 落库（真实 PG）
        rows = (await dbSession.execute(
            select(AnalysisHypothesis).where(
                AnalysisHypothesis.session_id == "hyp-1"
            ).order_by(AnalysisHypothesis.id)
        )).scalars().all()
        assert len(rows) == 2
        assert rows[0].turn_question == TRIGGER_QUESTION
        assert rows[0].verification_sql.startswith("SELECT")

        # LLM 预算：基线 4 次（plan/SQL/chart/answer）+ 假设 1 次 = 5
        assert len(llm.calls) == 5, f"LLM 总调用={len(llm.calls)}，期望 5"

        # 计量：session_token_usage 出现 purpose=hypothesis 行
        usage = (await dbSession.execute(
            select(SessionTokenUsage).where(
                SessionTokenUsage.session_id == "hyp-1",
                SessionTokenUsage.purpose == "hypothesis",
            )
        )).scalars().all()
        assert len(usage) == 1, "假设生成必须恰好记账一次 purpose=hypothesis"
        assert usage[0].prompt_tokens == 10
        assert usage[0].completion_tokens == 5

    async def test_non_triggered_question_zero_extra_llm(
        self, client: AsyncClient, dbSession: AsyncSession, monkeypatch,
    ) -> None:
        """非触发问题：LLM 计数保持基线 4，零假设、零 hypothesis purpose。"""
        config, ds = await _seed(dbSession)
        llm = _HypothesisLlm()
        _installFakesWithLlm(monkeypatch, config, llm)

        resp = await client.post(
            "/api/v1/chat", json=_chat_payload(NON_TRIGGER_QUESTION, ds.id, sessionId="hyp-2"),
        )
        assert resp.status_code == 200
        assert resp.json()["hypotheses"] is None
        assert len(llm.calls) == 4, f"LLM 总调用={len(llm.calls)}，期望基线 4"
        rows = (await dbSession.execute(
            select(AnalysisHypothesis).where(AnalysisHypothesis.session_id == "hyp-2")
        )).scalars().all()
        assert rows == []
        usage = (await dbSession.execute(
            select(SessionTokenUsage).where(
                SessionTokenUsage.session_id == "hyp-2",
                SessionTokenUsage.purpose == "hypothesis",
            )
        )).scalars().all()
        assert usage == []

    async def test_parse_empty_degrades_gracefully(
        self, client: AsyncClient, dbSession: AsyncSession, monkeypatch,
    ) -> None:
        """LLM 回复解析为空：best-effort 降级——主回答照常，零假设落库。"""
        config, ds = await _seed(dbSession)
        llm = _HypothesisLlm(hypothesisParseOk=False)
        _installFakesWithLlm(monkeypatch, config, llm)

        resp = await client.post(
            "/api/v1/chat", json=_chat_payload(TRIGGER_QUESTION, ds.id, sessionId="hyp-3"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["answer"], "主回答不得被假设链路失败阻断"
        assert body["hypotheses"] is None
        rows = (await dbSession.execute(
            select(AnalysisHypothesis).where(AnalysisHypothesis.session_id == "hyp-3")
        )).scalars().all()
        assert rows == []


class TestHypothesisStreamPath:
    async def test_stream_persists_without_sse_frame(
        self, client: AsyncClient, dbSession: AsyncSession, monkeypatch,
    ) -> None:
        """流式路径：假设落库但 SSE 帧序列不含假设内容（B5 教训：两路都接线）。"""
        config, ds = await _seed(dbSession)
        llm = _HypothesisLlm()
        _installFakesWithLlm(monkeypatch, config, llm)

        resp = await client.post(
            "/api/v1/chat/stream",
            json=_chat_payload(TRIGGER_QUESTION, ds.id, sessionId="hyp-stream"),
        )
        assert resp.status_code == 200
        # SSE 全文不含假设陈述（不进流式帧）
        assert "收货量下降" not in resp.text
        # 但已落库
        rows = (await dbSession.execute(
            select(AnalysisHypothesis).where(
                AnalysisHypothesis.session_id == "hyp-stream"
            )
        )).scalars().all()
        assert len(rows) == 2


class TestHypothesisGetApi:
    async def test_get_returns_camel_case_for_owner(
        self, client: AsyncClient, dbSession: AsyncSession,
    ) -> None:
        await _seedChatSessionMarker(dbSession, "hyp-get", "user-a")
        dbSession.add_all([
            AnalysisHypothesis(
                session_id="hyp-get", statement="假设一", driver="QTY",
                verification_sql="SELECT 1", turn_question=TRIGGER_QUESTION,
            ),
            AnalysisHypothesis(
                session_id="hyp-get", statement="假设二", driver=None,
                verification_sql="SELECT 2", turn_question=TRIGGER_QUESTION,
            ),
        ])
        await dbSession.commit()

        resp = await client.get(
            "/api/v1/chat/sessions/hyp-get/hypotheses",
            headers={"X-User-Id": "user-a"},
        )
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        # created_time 倒序 + id 倒序（同事务内 created_time 并列，新插入者在前）
        item = items[0]
        assert item["statement"] == "假设二"
        assert item["verificationSql"] == "SELECT 2"
        assert item["driver"] is None
        assert item["turnQuestion"] == TRIGGER_QUESTION
        assert "createdTime" in item

    async def test_get_respects_limit(
        self, client: AsyncClient, dbSession: AsyncSession,
    ) -> None:
        await _seedChatSessionMarker(dbSession, "hyp-limit", "user-a")
        dbSession.add_all([
            AnalysisHypothesis(
                session_id="hyp-limit", statement=f"假设{i}", driver=None,
                verification_sql="SELECT 1", turn_question=TRIGGER_QUESTION,
            )
            for i in range(5)
        ])
        await dbSession.commit()
        resp = await client.get(
            "/api/v1/chat/sessions/hyp-limit/hypotheses",
            params={"limit": 2},
            headers={"X-User-Id": "user-a"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_get_other_user_session_403(
        self, client: AsyncClient, dbSession: AsyncSession,
    ) -> None:
        """归属校验：非本人 session 403，detail 不回显归属者。"""
        await _seedChatSessionMarker(dbSession, "hyp-own", "user-a")
        dbSession.add(AnalysisHypothesis(
            session_id="hyp-own", statement="s", driver=None,
            verification_sql="SELECT 1", turn_question=TRIGGER_QUESTION,
        ))
        await dbSession.commit()

        resp = await client.get(
            "/api/v1/chat/sessions/hyp-own/hypotheses",
            headers={"X-User-Id": "user-b", "X-User-Roles": "user"},
        )
        assert resp.status_code == 403
        assert "user-a" not in resp.text

    async def test_get_admin_bypasses_ownership(
        self, client: AsyncClient, dbSession: AsyncSession,
    ) -> None:
        await _seedChatSessionMarker(dbSession, "hyp-admin", "user-a")
        resp = await client.get(
            "/api/v1/chat/sessions/hyp-admin/hypotheses",
            headers={"X-User-Id": "admin-user", "X-User-Roles": "admin"},
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_get_unmarked_session_fails_open(
        self, client: AsyncClient, dbSession: AsyncSession,
    ) -> None:
        """无归属标记（存量 NULL 行/新会话）→ fail-open 放行（evidences 同语义）。"""
        resp = await client.get(
            "/api/v1/chat/sessions/hyp-unknown/hypotheses",
            headers={"X-User-Id": "anyone"},
        )
        assert resp.status_code == 200
        assert resp.json() == []


def _installFakesWithLlm(monkeypatch, config: LlmConfig, llm: _HypothesisLlm) -> None:
    """同 test_chat_api._installFakes，但 LLM 工厂共享同一实例以做调用计数。"""
    import app.api.v1.chat as chat_module

    class _FakeAdapter:
        async def execute_read_only(self, sql: str) -> list[dict]:
            return ROWS

    class _StubEmbeddingService:
        async def storeQueryEmbedding(self, **kwargs) -> None:
            return None

    monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
    monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: llm)
    monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingService())


class _RouterFor:
    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx) -> LlmConfig:
        return self._config

    def selectFallbackModel(self, configs, excludeId) -> LlmConfig | None:
        return None
