"""WikiQaService 单元测试（feat-wiki-chat）。

镜像 test_rag_qa_service.py 模式：纯 mock 注入，不触 DB / Milvus / LLM。
覆盖 unit/conftest.py 的 autouse warmBusinessObjectRegistry fixture（NO-OP）。
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 覆盖 parent conftest 的 autouse db-dependent fixture（必须放最前）
@pytest.fixture(autouse=True)
async def warmBusinessObjectRegistry() -> None:
    # NO-OP: 本模块测试纯 mock，不 warm 任何 registry
    pass

from app.dependencies import CurrentUser
from app.domain.wiki_schemas import WikiChatRequest
from app.infrastructure.llm.base_client import StreamChunk
from app.services.stream_events import (
    EVENT_QA_CITATIONS,
    EVENT_QA_DONE,
    EVENT_QA_META,
    EVENT_TOKEN,
)
from app.services.wiki_qa_service import WikiQaService


def _make_fake_client(chunks_text: list[str], model_name: str = "fake-mdl") -> MagicMock:
    """fake BaseLlmClient：completeStream 返回给定 chunk 序列，记录调用。"""
    client = MagicMock()
    _calls: list = []

    async def wrapped_stream(messages, **kwargs):
        _calls.append((messages, kwargs))
        for idx, text in enumerate(chunks_text):
            is_done = idx == len(chunks_text) - 1
            yield StreamChunk(
                content=text,
                isDone=is_done,
                promptTokens=10,
                completionTokens=5 * (idx + 1),
                modelName=model_name,
            )

    client.completeStream = wrapped_stream
    client._calls = _calls
    return client


def _cfg(**overrides) -> MagicMock:
    base = dict(
        id=1,
        model_name="fake-mdl",
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
        is_active=True,
        provider="openai",
        cost_threshold=Decimal("9999"),
        weight=1,
    )
    base.update(overrides)
    return MagicMock(**base)


_HIT = {
    "pageId": "PAGE-A",
    "title": "准入规则",
    "status": "EFFECTIVE",
    "dimension": "RULE",
    "chunkText": "注册资本一千万以上",
    "chunkSequence": 0,
    "distance": 0.8,
    "score": 0.55,
}


def _svcWithHits(hits: list[dict], fake_client: MagicMock | None = None) -> WikiQaService:
    svc = WikiQaService()
    svc._wiki_svc = MagicMock()
    svc._wiki_svc.searchSemantic = AsyncMock(return_value=hits)
    if fake_client is not None:
        svc._llm_factory = MagicMock(return_value=fake_client)
    return svc


def _mockSession(history_rows: list | None = None) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: history_rows or []))
    )
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


_DTO = WikiChatRequest(session_id="sess-1", question="厂家合作有什么门槛", top_k=8)
_ACTOR = CurrentUser(userId="u-1", departments=[])


class TestLoadHistory:
    @pytest.mark.asyncio
    async def test_load_history_filters_channel_wiki_qa(self) -> None:
        """load_history 只取 channel='wiki_qa'。"""

        class FakeRow:
            __slots__ = ("role", "content", "channel", "created_time")

            def __init__(self, role: str, content: str, channel: str, created_time):
                self.role = role
                self.content = content
                self.channel = channel
                self.created_time = created_time

        rows = [
            FakeRow("assistant", "回答", "wiki_qa", datetime.datetime(2026, 1, 1)),
            FakeRow("user", "提问", "wiki_qa", datetime.datetime(2026, 1, 2)),
        ]
        session = MagicMock()
        execute = AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: rows)))
        session.execute = execute

        svc = WikiQaService()
        out = await svc.load_history(session, session_id="sess-1")

        assert out == [
            {"role": "user", "content": "提问"},
            {"role": "assistant", "content": "回答"},
        ]
        compiled = str(execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "channel = 'wiki_qa'" in compiled


class TestShortCircuit:
    @pytest.mark.asyncio
    async def test_no_hits_returns_template_zero_llm(self) -> None:
        """无命中 → meta + citations(空) + token(模板) + done，零 LLM。"""
        svc = _svcWithHits([], fake_client=MagicMock())

        events = []
        async for ev in svc.answer_stream(_mockSession(), _DTO, actor=_ACTOR, configs=[]):
            events.append(ev)

        assert len(events) == 4
        assert events[0].event == EVENT_QA_META
        assert events[0].data["intent"] == "wiki_qa"
        assert events[1].event == EVENT_QA_CITATIONS
        assert events[1].data["citations"] == []
        assert "未在企业 Wiki" in events[2].data["content"]
        assert events[3].event == EVENT_QA_DONE
        assert events[3].data["tokensUsed"] == 0
        svc._llm_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_score_short_circuits(self) -> None:
        """top1 score < 0.3 → 同样短路。"""
        svc = _svcWithHits(
            [{**_HIT, "score": 0.2}], fake_client=MagicMock()
        )
        events = []
        async for ev in svc.answer_stream(_mockSession(), _DTO, actor=_ACTOR, configs=[]):
            events.append(ev)
        assert "未在企业 Wiki" in events[2].data["content"]
        svc._llm_factory.assert_not_called()


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_emits_meta_citations_tokens_done(self) -> None:
        fake_client = _make_fake_client(["根据", "准入规则", "…"])
        svc = _svcWithHits([dict(_HIT)], fake_client=fake_client)
        session = _mockSession()

        events = []
        async for ev in svc.answer_stream(session, _DTO, actor=_ACTOR, configs=[_cfg()]):
            events.append(ev)

        # meta + citations + 3 token + done
        assert len(events) == 6
        assert events[1].data["citations"][0]["id"] == 1
        assert events[1].data["citations"][0]["pageId"] == "PAGE-A"
        assert events[2].data["content"] == "根据"
        assert events[5].event == EVENT_QA_DONE
        assert events[5].data["modelName"] == "fake-mdl"
        assert events[5].data["tokensUsed"] == 10 + 5 * 3

    @pytest.mark.asyncio
    async def test_citations_numbered_1_based(self) -> None:
        svc = _svcWithHits(
            [dict(_HIT), {**_HIT, "pageId": "PAGE-B", "title": "罚则"}],
            fake_client=_make_fake_client(["答"]),
        )
        events = []
        async for ev in svc.answer_stream(_mockSession(), _DTO, actor=_ACTOR, configs=[_cfg()]):
            events.append(ev)
        ids = [c["id"] for c in events[1].data["citations"]]
        assert ids == [1, 2]

    @pytest.mark.asyncio
    async def test_prompt_contains_chunks_json_and_question(self) -> None:
        fake_client = _make_fake_client(["答"])
        svc = _svcWithHits([dict(_HIT)], fake_client=fake_client)

        async for _ in svc.answer_stream(_mockSession(), _DTO, actor=_ACTOR, configs=[_cfg()]):
            pass

        messages_arg = fake_client._calls[0][0]
        system_msg = next(m for m in messages_arg if m.role == "system")
        user_msg = next(m for m in messages_arg if m.role == "user")
        assert "PAGE-A" in system_msg.content
        assert "厂家合作有什么门槛" in user_msg.content

    @pytest.mark.asyncio
    async def test_history_injected_as_plain_text(self) -> None:
        fake_client = _make_fake_client(["答"])
        svc = _svcWithHits([dict(_HIT)], fake_client=fake_client)
        history_rows = [
            MagicMock(role="user", content="上一问", channel="wiki_qa"),
            MagicMock(role="assistant", content="上一答", channel="wiki_qa"),
        ]
        session = _mockSession(history_rows)

        async for _ in svc.answer_stream(session, _DTO, actor=_ACTOR, configs=[_cfg()]):
            pass

        messages_arg = fake_client._calls[0][0]
        user_msg = next(m for m in messages_arg if m.role == "user")
        assert "上一问" in user_msg.content
        assert "上一答" in user_msg.content


class TestPersist:
    @pytest.mark.asyncio
    async def test_persists_user_and_assistant_wiki_qa_channel(self) -> None:
        svc = _svcWithHits([dict(_HIT)], fake_client=_make_fake_client(["回答"]))
        session = _mockSession()

        async for _ in svc.answer_stream(session, _DTO, actor=_ACTOR, configs=[_cfg()]):
            pass

        assert session.add.call_count >= 2
        added = [c.args[0] for c in session.add.call_args_list]
        # record（未 patch 的真实实现）也会 add 一行 WikiTokenUsage，过滤掉
        user_rows = [e for e in added if getattr(e, "role", None) == "user"]
        asst_rows = [e for e in added if getattr(e, "role", None) == "assistant"]
        assert len(user_rows) == 1
        assert len(asst_rows) == 1
        assert user_rows[0].channel == "wiki_qa"
        assert user_rows[0].content == "厂家合作有什么门槛"
        assert user_rows[0].user_id == "u-1"
        assert asst_rows[0].channel == "wiki_qa"
        assert asst_rows[0].content == "回答"
        assert asst_rows[0].citations[0]["pageId"] == "PAGE-A"

    @pytest.mark.asyncio
    async def test_short_circuit_still_persists(self) -> None:
        """短路（无命中）也要落库，历史面板才能看到这轮问答。"""
        svc = _svcWithHits([], fake_client=MagicMock())
        session = _mockSession()

        async for _ in svc.answer_stream(session, _DTO, actor=_ACTOR, configs=[]):
            pass

        assert session.add.call_count == 2


class TestModelSelection:
    @pytest.mark.asyncio
    async def test_rejects_inactive_model_when_specified(self) -> None:
        from app.services.messages_zh import MSG_MODEL_CONFIG_UNAVAILABLE

        svc = _svcWithHits([dict(_HIT)], fake_client=MagicMock())
        session = _mockSession()
        dto = WikiChatRequest(
            session_id="sess-1", question="问题", top_k=8, model_id=99
        )

        events = []
        with pytest.raises(ValueError) as excInfo:
            async for ev in svc.answer_stream(
                session, dto, actor=_ACTOR, configs=[_cfg(id=99, is_active=False)]
            ):
                events.append(ev)

        assert str(excInfo.value) == MSG_MODEL_CONFIG_UNAVAILABLE.format(id=99)
        assert any(ev.event == EVENT_QA_META for ev in events)
        svc._llm_factory.assert_not_called()


class TestDefaultLlmFactory:
    @pytest.mark.asyncio
    async def test_default_factory_resolves_via_create_client(self, monkeypatch) -> None:
        """未注入 factory 时走 createClient 缺省；client 为 None 报 LLMUnavailableError。"""
        from app.domain.exceptions import LLMUnavailableError
        from app.infrastructure.llm import factory as llm_factory_mod

        svc = _svcWithHits([dict(_HIT)])  # 不注入 llm_factory
        monkeypatch.setattr(llm_factory_mod, "createClient", lambda cfg: None)
        session = _mockSession()

        with pytest.raises(LLMUnavailableError):
            async for _ in svc.answer_stream(session, _DTO, actor=_ACTOR, configs=[_cfg()]):
                pass


class TestMetering:
    @pytest.mark.asyncio
    async def test_records_qa_mechanism_usage(self) -> None:
        """LLM 答案必须落 wiki_token_usage（mechanism=QA, purpose=wiki_chat_answer）。"""
        svc = _svcWithHits([dict(_HIT)], fake_client=_make_fake_client(["回答"]))
        session = _mockSession()

        with patch("app.services.wiki_qa_service.WikiTokenUsageService") as mockUsageCls:
            mockRecord = AsyncMock()
            mockUsageCls.return_value.record = mockRecord
            async for _ in svc.answer_stream(session, _DTO, actor=_ACTOR, configs=[_cfg()]):
                pass

        mockRecord.assert_awaited_once()
        kwargs = mockRecord.await_args.kwargs
        assert kwargs["mechanism"] == "QA"
        assert kwargs["purpose"] == "wiki_chat_answer"
        assert kwargs["modelConfigId"] == 1
        assert kwargs["modelName"] == "fake-mdl"
        assert kwargs["promptTokens"] == 10
        assert kwargs["completionTokens"] == 5
        assert kwargs["cost"] == Decimal("0.001") * Decimal(10) / Decimal(1000) + Decimal(
            "0.002"
        ) * Decimal(5) / Decimal(1000)
        # 与 persist 共用主 session（同一事务收口；record 首位参数是 session）
        assert mockRecord.await_args.args[0] is session

    @pytest.mark.asyncio
    async def test_short_circuit_records_nothing(self) -> None:
        """短路零 LLM → 不落 QA 计量行。"""
        svc = _svcWithHits([], fake_client=MagicMock())

        with patch("app.services.wiki_qa_service.WikiTokenUsageService") as mockUsageCls:
            mockUsageCls.return_value.record = AsyncMock()
            async for _ in svc.answer_stream(_mockSession(), _DTO, actor=_ACTOR, configs=[]):
                pass

        mockUsageCls.return_value.record.assert_not_awaited()
