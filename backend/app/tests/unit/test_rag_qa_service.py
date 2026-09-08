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
