"""RagQaService 单元测试（纯函数 + mock 注入）。
覆盖 unit/conftest.py 的 autouse warmBusinessObjectRegistry fixture，
因为本模块测试使用纯 mock，不触 DB。
"""
from __future__ import annotations

import pytest

# 覆盖 parent conftest 的 autouse db-dependent fixture（必须放最前）
# 用空 fixture 替代，禁用该自动行为
@pytest.fixture(autouse=True)
async def warmBusinessObjectRegistry() -> None:
    # NO-OP: 本模块测试纯 mock，不 warm 任何 registry
    pass


from unittest.mock import AsyncMock, MagicMock

from app.dependencies import CurrentUser
from app.domain.models import SessionMessage
from app.domain.schemas import DocQaRequest
from app.services.rag_qa_service import RagQaService
from app.services.stream_events import EVENT_QA_DONE, EVENT_QA_META


@pytest.mark.asyncio
async def test_load_history_returns_plain_text_doc_qa_only() -> None:
    """load_history 只取 channel='doc_qa'，返回 [{role, content}] 无 citations。"""
    import datetime

    # 使用真实对象替代 MagicMock，确保 reversed() 迭代顺序确定
    class FakeRow:
        """模拟 SQLAlchemy ORM 行对象。"""
        __slots__ = ("role", "content", "citations", "channel", "created_time")
        def __init__(self, role: str, content: str, citations, channel: str, created_time: datetime.datetime):
            self.role = role
            self.content = content
            self.citations = citations
            self.channel = channel
            self.created_time = created_time
        def __repr__(self):
            return f"FakeRow(role={self.role!r}, content={self.content!r})"

    fake_row_user = FakeRow(
        role="user", content="什么是质量协议？", citations=None, channel="doc_qa",
        created_time=datetime.datetime(2026, 1, 2),  # newer → ORDER BY DESC first → reversed gives user first ✓
    )
    fake_row_asst = FakeRow(
        role="assistant", content="质量协议是...", citations=[{"id": 1}], channel="doc_qa",
        created_time=datetime.datetime(2026, 1, 1),  # older
    )
    fake_row_chat = FakeRow(
        role="user", content="chat 消息", citations=None, channel="chat",
        created_time=datetime.datetime(2026, 1, 3),
    )

    # SQL WHERE channel='doc_qa' 过滤后只返回 doc_qa 行
    # reversed([asst, user]) → [user, asst] ✓
    result = MagicMock()
    result.scalars.return_value.all.return_value = [fake_row_asst, fake_row_user]
    execute = AsyncMock(return_value=result)

    session = MagicMock()
    session.execute = execute

    svc = RagQaService()
    out = await svc.load_history(session, session_id="sess-1")

    assert out == [
        {"role": "user", "content": "什么是质量协议？"},
        {"role": "assistant", "content": "质量协议是..."},
    ]
    # 必须按 channel='doc_qa' 过滤
    stmt = execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "channel = 'doc_qa'" in compiled


@pytest.mark.asyncio
async def test_answer_stream_no_chunks_returns_template() -> None:
    """top1 score < 0.3 → 直接返回固定模板，零 LLM 调用。"""
    svc = RagQaService()
    svc._rag_svc = MagicMock()
    svc._rag_svc.searchDocuments = AsyncMock(return_value=[])  # 无 chunks
    svc._llm_factory = MagicMock()  # 若被调用会抛错

    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: [])))
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()  # _persist 收尾提交（缺它此前必红）
    # TokenUsageService.recordUsage 收尾 refresh（MagicMock 默认同步，await 必炸）
    session.refresh = AsyncMock()
    dto = DocQaRequest(session_id="sess-1", question="abc", top_k=8)

    actor = CurrentUser(userId="u-1", departments=[])
    configs: list = []

    events = []
    async for ev in svc.answer_stream(session, dto, actor=actor, configs=configs):
        events.append(ev)

    # 必须有 meta + citations(空) + token(模板) + done；无 LLM 调用
    # StreamEvent 字段是 .event（不是 .type）
    assert len(events) == 4
    assert events[0].event == EVENT_QA_META
    assert events[1].event == "qa_citations"
    assert events[1].data["citations"] == []
    assert "未在已上传文档中找到相关依据" in events[2].data["content"]
    assert events[3].event == EVENT_QA_DONE
    svc._llm_factory.assert_not_called()  # 关键：没调 LLM


from decimal import Decimal

from app.infrastructure.llm.base_client import LlmMessage, StreamChunk
from app.services.stream_events import EVENT_TOKEN


def _make_fake_client(chunks_text: list[str], model_name: str = "fake-mdl") -> MagicMock:
    """构造 fake BaseLlmClient，completeStream 返回给定 chunk 序列。"""
    client = MagicMock()

    async def _stream(messages, **kwargs):
        for idx, text in enumerate(chunks_text):
            is_done = idx == len(chunks_text) - 1
            yield StreamChunk(
                content=text,
                isDone=is_done,
                promptTokens=10,
                completionTokens=5 * (idx + 1),
                modelName=model_name,
            )

    # wrap so tests can assert call count without making it a Mock
    _calls: list = []

    async def wrapped_stream(messages, **kwargs):
        _calls.append((messages, kwargs))
        async for chunk in _stream(messages, **kwargs):
            yield chunk

    client.completeStream = wrapped_stream
    client._calls = _calls
    return client


@pytest.mark.asyncio
async def test_answer_stream_full_pipeline_emits_meta_citations_tokens_done() -> None:
    """完整流水线：检索命中 → meta + citations + token×N + done，调用 LLM。"""
    svc = RagQaService()
    svc._rag_svc = MagicMock()
    svc._rag_svc.searchDocuments = AsyncMock(return_value=[
        {"document_id": "DOC-A", "document_name": "合同", "chunk_text": "条款", "score": 0.85},
    ])

    fake_client = _make_fake_client(["根据", "合同条款", "..."])
    svc._llm_factory = MagicMock(return_value=fake_client)

    session = MagicMock()
    # history 为空
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: []))
    )
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()  # _persist 收尾提交（缺它此前必红）
    # TokenUsageService.recordUsage 收尾 refresh（MagicMock 默认同步，await 必炸）
    session.refresh = AsyncMock()

    dto = DocQaRequest(session_id="sess-1", question="什么是质量协议？", top_k=8)
    actor = CurrentUser(userId="u-1", departments=[])
    cfg = MagicMock(id=1, model_name="fake-mdl", cost_per_1k_input=Decimal("0.001"), cost_per_1k_output=Decimal("0.002"),
                    is_active=True, provider="openai", cost_threshold=Decimal("9999"), weight=1)

    events: list = []
    async for ev in svc.answer_stream(session, dto, actor=actor, configs=[cfg]):
        events.append(ev)

    # 序列：1 meta + 1 citations + 3 token + 1 done
    assert len(events) == 6
    assert events[0].event == EVENT_QA_META
    assert events[1].event == "qa_citations"
    assert events[1].data["citations"][0]["id"] == 1
    assert events[2].event == EVENT_TOKEN
    assert events[2].data["content"] == "根据"
    assert events[3].data["content"] == "合同条款"
    assert events[4].data["content"] == "..."
    assert events[5].event == EVENT_QA_DONE
    # done 携带 token 统计
    assert events[5].data["modelName"] == "fake-mdl"
    assert events[5].data["tokensUsed"] > 0


@pytest.mark.asyncio
async def test_answer_stream_passes_history_to_llm_as_plain_text() -> None:
    """第 2 轮：history 注入到 user message 中，纯文本不含 citations JSON。"""
    svc = RagQaService()
    svc._rag_svc = MagicMock()
    svc._rag_svc.searchDocuments = AsyncMock(return_value=[
        {"document_id": "DOC-A", "document_name": "合同", "chunk_text": "条款", "score": 0.85},
    ])

    fake_client = _make_fake_client(["好。"])
    svc._llm_factory = MagicMock(return_value=fake_client)

    # history 已有 1 轮 user + 1 轮 assistant
    rows_history = [
        MagicMock(role="user", content="什么是质量协议？", citations=None, channel="doc_qa"),
        MagicMock(role="assistant", content="质量协议是...", citations=[{"id":1}], channel="doc_qa"),
    ]
    session = MagicMock()
    # 第 1 次调 execute → history；第 2 次调 execute → persist user 行；第 3 次 → persist assistant
    session.execute = AsyncMock(side_effect=[
        MagicMock(scalars=lambda: MagicMock(all=lambda: rows_history)),  # history
        MagicMock(),  # user 落库
        MagicMock(),  # assistant 落库
    ])
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()  # _persist 收尾提交（缺它此前必红）
    # TokenUsageService.recordUsage 收尾 refresh（MagicMock 默认同步，await 必炸）
    session.refresh = AsyncMock()

    dto = DocQaRequest(session_id="sess-1", question="更详细说说", top_k=8)
    actor = CurrentUser(userId="u-1", departments=[])
    cfg = MagicMock(id=1, model_name="fake-mdl", cost_per_1k_input=Decimal("0.001"), cost_per_1k_output=Decimal("0.002"),
                    is_active=True, provider="openai", cost_threshold=Decimal("9999"), weight=1)

    async for _ in svc.answer_stream(session, dto, actor=actor, configs=[cfg]):
        pass

    # 验证 LLM 收到的 messages 含历史纯文本
    assert len(fake_client._calls) == 1
    messages_arg = fake_client._calls[0][0]
    user_msg = next(m for m in messages_arg if m.role == "user")
    assert "什么是质量协议？" in user_msg.content
    assert "质量协议是..." in user_msg.content
    assert "citations" not in user_msg.content  # 不含 JSON
    assert '{"id"' not in user_msg.content


@pytest.mark.asyncio
async def test_answer_stream_persists_user_and_assistant_with_citations() -> None:
    """流结束后落库 2 行 session_message：user + assistant（assistant 带 citations）。"""
    svc = RagQaService()
    svc._rag_svc = MagicMock()
    svc._rag_svc.searchDocuments = AsyncMock(return_value=[
        {"document_id": "DOC-A", "document_name": "合同", "chunk_text": "条款", "score": 0.85},
    ])
    fake_client = _make_fake_client(["回答"])
    svc._llm_factory = MagicMock(return_value=fake_client)

    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        MagicMock(scalars=lambda: MagicMock(all=lambda: [])),  # history 空
        MagicMock(),  # user 落库
        MagicMock(),  # assistant 落库
    ])
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()  # _persist 收尾提交（缺它此前必红）
    # TokenUsageService.recordUsage 收尾 refresh（MagicMock 默认同步，await 必炸）
    session.refresh = AsyncMock()

    dto = DocQaRequest(session_id="sess-1", question="问题", top_k=8)
    actor = CurrentUser(userId="u-1", departments=[])
    cfg = MagicMock(id=1, model_name="fake-mdl", cost_per_1k_input=Decimal("0.001"), cost_per_1k_output=Decimal("0.002"),
                    is_active=True, provider="openai", cost_threshold=Decimal("9999"), weight=1)

    async for _ in svc.answer_stream(session, dto, actor=actor, configs=[cfg]):
        pass

    # session.add 必须被调 ≥ 2 次（user + assistant）
    assert session.add.call_count >= 2
    # 只挑 SessionMessage：计量台账行（SessionTokenUsage）也会被 add，它没有 content
    added_entities = [
        c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], SessionMessage)
    ]
    # 找到 user 行（content=dto.question）和 assistant 行（content=回答）
    user_rows = [e for e in added_entities if e.content == "问题" and e.role == "user"]
    asst_rows = [e for e in added_entities if e.content == "回答" and e.role == "assistant"]
    assert len(user_rows) == 1
    assert len(asst_rows) == 1
    assert user_rows[0].channel == "doc_qa"
    assert user_rows[0].user_id == "u-1"
    assert asst_rows[0].citations is not None
    assert asst_rows[0].citations[0]["document_id"] == "DOC-A"


class TestExplicitModelIdRejectsInactive:
    """显式 model_id 选取时必须拒绝已停用（is_active=False）的模型。

    Bug repro（2026-09-08 用户反馈）：与 ChatService 同模式 bug——RagQaService.answer_stream
    在 dto.model_id 指定时仅校验「存在」，未校验「is_active」。修复后应抛
    ValueError(MSG_MODEL_CONFIG_UNAVAILABLE)。
    """

    async def test_rejects_inactive_model_when_user_specifies_model_id(self) -> None:
        from app.services.messages_zh import MSG_MODEL_CONFIG_UNAVAILABLE

        inactive_cfg = MagicMock(
            id=99,
            model_name="disabled-mdl",
            cost_per_1k_input=Decimal("0.001"),
            cost_per_1k_output=Decimal("0.002"),
            is_active=False,  # 关键：停用
            provider="openai",
            cost_threshold=Decimal("9999"),
            weight=1,
        )
        svc = RagQaService()
        svc._rag_svc = MagicMock()
        svc._rag_svc.searchDocuments = AsyncMock(return_value=[
            {"document_id": "DOC-A", "document_name": "合同", "chunk_text": "条款", "score": 0.85},
        ])
        # 若被错误调用会 raise
        svc._llm_factory = MagicMock()

        session = MagicMock()
        session.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: []))
        )
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.commit = AsyncMock()  # _persist 收尾提交（缺它此前必红）
        # TokenUsageService.recordUsage 收尾 refresh（MagicMock 默认同步，await 必炸）
        session.refresh = AsyncMock()
        session.commit = AsyncMock()  # 避免 _persist 路径 noise 让 RED 断言更干净

        dto = DocQaRequest(
            session_id="sess-1", question="什么是质量协议？", top_k=8, model_id=99,
        )
        actor = CurrentUser(userId="u-1", departments=[])

        events = []
        with pytest.raises(ValueError) as excInfo:
            async for ev in svc.answer_stream(
                session, dto, actor=actor, configs=[inactive_cfg],
            ):
                events.append(ev)

        # 复用既有 MSG_MODEL_CONFIG_UNAVAILABLE 消息（声明「不存在或已禁用」）
        assert str(excInfo.value) == MSG_MODEL_CONFIG_UNAVAILABLE.format(id=99)
        # meta + citations 已经发出（先于模型选择），但 LLM 不应被调用
        assert any(ev.event == EVENT_QA_META for ev in events)
        svc._llm_factory.assert_not_called()

    async def test_accepts_active_model_when_user_specifies_model_id(self) -> None:
        """回归保护：合法（is_active=True）显式选择应正常进入 LLM 流式。"""
        svc = RagQaService()
        svc._rag_svc = MagicMock()
        svc._rag_svc.searchDocuments = AsyncMock(return_value=[
            {"document_id": "DOC-A", "document_name": "合同", "chunk_text": "条款", "score": 0.85},
        ])
        fake_client = _make_fake_client(["好。"])
        svc._llm_factory = MagicMock(return_value=fake_client)

        session = MagicMock()
        session.execute = AsyncMock(side_effect=[
            MagicMock(scalars=lambda: MagicMock(all=lambda: [])),  # history 空
            MagicMock(),  # user 落库
            MagicMock(),  # assistant 落库
        ])
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.commit = AsyncMock()  # _persist 收尾提交（缺它此前必红）
        # TokenUsageService.recordUsage 收尾 refresh（MagicMock 默认同步，await 必炸）
        session.refresh = AsyncMock()
        session.commit = AsyncMock()

        active_cfg = MagicMock(
            id=1,
            model_name="fake-mdl",
            cost_per_1k_input=Decimal("0.001"),
            cost_per_1k_output=Decimal("0.002"),
            is_active=True,
            provider="openai",
            cost_threshold=Decimal("9999"),
            weight=1,
        )
        dto = DocQaRequest(
            session_id="sess-1", question="问题", top_k=8, model_id=1,
        )
        actor = CurrentUser(userId="u-1", departments=[])

        events: list = []
        async for ev in svc.answer_stream(session, dto, actor=actor, configs=[active_cfg]):
            events.append(ev)

        # 正常流水线：meta + citations + token + done
        assert any(ev.event == EVENT_QA_META for ev in events)
        assert any(ev.event == EVENT_QA_DONE for ev in events)
        svc._llm_factory.assert_called_once()


def _make_never_done_client(text: str = "回答") -> MagicMock:
    """退化客户端：只吐内容，**从不**把 isDone 置 True（不报 usage）。"""
    client = MagicMock()

    async def _stream(messages, **kwargs):
        yield StreamChunk(
            content=text, isDone=False, promptTokens=0, completionTokens=0,
            modelName="fake-mdl",
        )

    client.completeStream = _stream
    return client


def _mock_session(history_rows: list | None = None) -> MagicMock:
    """与既有用例同构的最小 session mock（commit + refresh 都必须可 await）。"""
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: list(history_rows or [])))
    )
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    # TokenUsageService.recordUsage 收尾 refresh（MagicMock 默认同步，await 必炸）
    session.refresh = AsyncMock()
    return session


def _added_of(session: MagicMock, cls: type) -> list:
    """session.add 的实参里挑出某一类（台账行 / SessionMessage 混在一起）。"""
    return [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], cls)]


def _doc_qa_cfg() -> MagicMock:
    """单条可用 model config（与既有用例同构）。"""
    return MagicMock(
        id=1, model_name="fake-mdl",
        cost_per_1k_input=Decimal("0.001"), cost_per_1k_output=Decimal("0.002"),
        is_active=True, provider="openai",
        cost_threshold=Decimal("9999"), weight=1,
    )


@pytest.mark.asyncio
async def test_answer_stream_low_score_persists_real_citations() -> None:
    """低分短路（top1 < 0.3）也要把**真实检索到的**依据落库，不是空 []。

    H8 的对齐点：wiki_qa 在该分支持久化真实 citations（``wiki_qa_service.py:157``），
    doc_qa 此前存 ``[]``。SSE 事件里的 citations 本来就在阈值判断**之前**发给了前端
    （第 4 步），只在落库时抹成空会让历史消息与前端看到的依据不一致。
    """
    svc = RagQaService()
    svc._rag_svc = MagicMock()
    svc._rag_svc.searchDocuments = AsyncMock(return_value=[
        {"document_id": "DOC-C", "document_name": "低分文档", "chunk_text": "擦边", "score": 0.12},
    ])
    svc._llm_factory = MagicMock()  # 短路 ⇒ 若被调用会抛错

    session = _mock_session()
    dto = DocQaRequest(session_id="sess-low", question="abc", top_k=8)

    events = [
        ev
        async for ev in svc.answer_stream(
            session, dto, actor=CurrentUser(userId="u-1", departments=[]), configs=[],
        )
    ]

    assert events[1].data["citations"][0]["document_id"] == "DOC-C"
    asst = next(m for m in _added_of(session, SessionMessage) if m.role == "assistant")
    assert asst.citations is not None
    assert asst.citations[0]["document_id"] == "DOC-C"


@pytest.mark.asyncio
async def test_answer_stream_zero_usage_writes_no_ledger_row() -> None:
    """客户端没报 usage（退化流）⇒ 不得写 0/0 假台账行。

    其余每个计量点都守了零用量（``_runL4AgentLoop`` / ``_resolveGlobalFilters`` /
    ``_recordDirectUsage``）：零 token 行会把 total_requests 灌水，且让「有台账行」
    不再等于「真的调过 LLM」。
    """
    svc = RagQaService()
    svc._rag_svc = MagicMock()
    svc._rag_svc.searchDocuments = AsyncMock(return_value=[
        {"document_id": "DOC-A", "document_name": "合同", "chunk_text": "条款", "score": 0.85},
    ])
    svc._llm_factory = MagicMock(return_value=_make_never_done_client())

    session = _mock_session()
    dto = DocQaRequest(session_id="sess-zero", question="abc", top_k=8)

    _ = [
        ev
        async for ev in svc.answer_stream(
            session, dto,
            actor=CurrentUser(userId="u-1", departments=[]),
            configs=[_doc_qa_cfg()],
        )
    ]

    from app.domain.models import SessionTokenUsage

    assert _added_of(session, SessionTokenUsage) == []
    # 消息照旧落库（零台账 ≠ 不落消息）
    assert len(_added_of(session, SessionMessage)) >= 2
