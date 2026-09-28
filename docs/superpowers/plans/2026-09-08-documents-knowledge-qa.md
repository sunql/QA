# DocumentsPage 知识问答 Tab 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 DocumentsPage 新增第 4 个 Tab「知识问答」，把已上传文档从「只检索」升级为「检索 + LLM 合成答案 + 引用回标」，支持多轮对话、历史会话侧栏、引用编号回标。

**Architecture:**
- 后端：新建 `RagQaService` 编排「加载历史 → Milvus 检索 → LLM 流式合成 → 落库」；新端点 `POST /api/v1/documents/qa`（SSE）；复用 chat 的 `BaseLlmClient.completeStream`、`ModelRouterService`、`TokenUsageService`。
- 数据：复用 `session_message` 表，Alembic 0047 加 `channel` / `citations` / `user_id` 三列 + 两个复合索引，channel 列隔离 chat 与 doc_qa。
- 前端：DocumentsPage 加 Tab 4；新组件 `DocumentQaPanel` 组合 MessageList + Input + HistoryPanel + CitationList；`chatStore` 加 `channel` 字段。

**Tech Stack:**
- Backend: FastAPI + SQLAlchemy 2.x async + Pydantic v2 + Milvus (pymilvus) + BaseLlmClient.completeStream (AsyncIterator[StreamChunk])
- Frontend: React + TypeScript + antd + Zustand chatStore + EventSource (SSE)
- DB: PostgreSQL + Alembic
- Tests: pytest + vitest（真实 PG；禁止 sqlite 内存库）

## Global Constraints

- 真实 PG + 完整 API 链路 + 真实数据库对象（禁止 sqlite 内存库 + 直接调 service）—— 来自 `Harness/rules/测试规范.md`
- 函数 < 50 行，文件 < 800 行（嵌套 ≤ 4 层）—— 来自 `coding-style.md`
- 不可变数据：spread / `model_copy`；禁止原地修改 —— 来自 `coding-style.md`
- Alembic 迁移必须向后兼容（nullable 列 + server_default）—— 来自既有约定
- SSE 事件名走常量，复用 `stream_events.StreamEvent.toSse()` —— 来自既有约定
- Token 计量必须经过 `TokenUsageService.recordUsage(purpose=...)` —— 来自 `qa-system two DBs policy` 与 audit 约定
- 类型注解：Python 用 snake_case（DB/JSON 一致性偏离 PEP 8）—— 来自项目 CLAUDE.md

---

## 文件结构

| 文件 | 状态 | 职责 |
|---|---|---|
| `backend/alembic/versions/0047_doc_qa_message.py` | 新建 | 加 channel/citations/user_id + 2 索引 |
| `backend/app/domain/models.py` | 改 `SessionMessage` | 加 3 列 + 2 索引 |
| `backend/app/domain/schemas.py` | 改 | 加 `DocQaRequest` |
| `backend/app/services/stream_events.py` | 改 | 加 `EVENT_QA_META/CITATIONS/DONE` |
| `backend/app/services/rag_qa_prompt.py` | 新建 | `DOC_QA_SYSTEM_PROMPT` + `DOC_QA_USER_TEMPLATE` + helpers |
| `backend/app/services/rag_qa_service.py` | 新建 | `RagQaService.answer_stream` |
| `backend/app/services/session_history_service.py` | 改 | `listChatSessions(channel=...)` |
| `backend/app/services/chat_service.py` | 改 | 写入 user_id 字段（chat/doc_qa 共用落库路径） |
| `backend/app/api/v1/documents.py` | 改 | 加 `POST /api/v1/documents/qa` 端点 |
| `backend/app/api/v1/session.py` | 改 | 历史消息端点加 `channel` 过滤 |
| `backend/app/tests/integration/test_doc_qa_api.py` | 新建 | API 端到端测试 |
| `backend/app/tests/unit/test_rag_qa_service.py` | 新建 | RagQaService 单测 |
| `frontend/src/types/document.ts` | 改 | 加 DocQa 类型 |
| `frontend/src/stores/chatStore.ts` | 改 | `channel` 字段 + `sendDocQa` |
| `frontend/src/api/document.ts` | 改 | 加 `searchDocumentsQa` SSE 客户端 |
| `frontend/src/components/documents/DocumentQaCitationList.tsx` | 新建 | 引用卡片列表 |
| `frontend/src/components/documents/DocumentQaMessageList.tsx` | 新建 | 消息列表 + `[n]` 解析 |
| `frontend/src/components/documents/DocumentQaInput.tsx` | 新建 | 输入框 + scope filter |
| `frontend/src/components/documents/DocumentQaHistoryPanel.tsx` | 新建 | 历史会话侧栏 |
| `frontend/src/components/documents/DocumentQaPanel.tsx` | 新建 | 主面板（组合） |
| `frontend/src/pages/DocumentsPage.tsx` | 改 | 加 Tab 4 |
| `frontend/src/tests/DocumentQaMessageList.test.tsx` | 新建 | `[n]` 解析 vitest |
| `frontend/src/tests/DocumentQaPanel.test.tsx` | 新建 | 面板集成 vitest |

---

## Task 1: Alembic 0047 + SessionMessage ORM

**Files:**
- Create: `backend/alembic/versions/0047_doc_qa_message.py`
- Modify: `backend/app/domain/models.py:691-710`
- Test: `backend/app/tests/integration/test_session_message_columns.py`

**Interfaces:**
- Consumes: 既有 `SessionMessage` 表结构（id / session_id / role / content / question / sql_generated + TimestampMixin）
- Produces: `SessionMessage.channel: str` (default "chat", nullable=False) / `.citations: list[dict] | None` (JSONB, nullable=True) / `.user_id: str | None` (String(64), nullable=True)；两个新复合索引

- [ ] **Step 1: 写失败测试 — 期望新列存在**

新建 `backend/app/tests/integration/test_session_message_columns.py`：
```python
"""验证 Alembic 0047 后 session_message 新列/索引落地。"""
import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.settings import get_settings
from app.domain.models import SessionMessage


@pytest.mark.asyncio
async def test_session_message_has_new_columns() -> None:
    """Alembic 0047 后 session_message 必须含 channel/citations/user_id 三列。"""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        insp = await conn.run_sync(lambda sync_conn: inspect(sync_conn))
        cols = {c["name"] for c in insp.get_columns("session_message")}
    await engine.dispose()
    assert "channel" in cols
    assert "citations" in cols
    assert "user_id" in cols


@pytest.mark.asyncio
async def test_session_message_has_new_indexes() -> None:
    """新索引必须存在以支撑 channel + user_id 查询。"""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        insp = await conn.run_sync(lambda sync_conn: inspect(sync_conn))
        idx = {i["name"] for i in insp.get_indexes("session_message")}
    await engine.dispose()
    assert "idx_session_msg_channel_time" in idx
    assert "idx_session_msg_user_channel_time" in idx
```

- [ ] **Step 2: 跑测试，验证 FAIL（迁移未应用）**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/integration/test_session_message_columns.py -v`
Expected: FAIL（`channel` / `citations` / `user_id` 列不存在）

- [ ] **Step 3: 写 Alembic 0047 迁移**

新建 `backend/alembic/versions/0047_doc_qa_message.py`：
```python
"""doc_qa session_message 扩列 + 索引（Alembic 0047）。

新增 channel VARCHAR(16) NOT NULL DEFAULT 'chat'（chat/doc_qa 隔离）；
新增 citations JSONB NULL（doc_qa 轮填引用）；
新增 user_id VARCHAR(64) NULL（doc_qa ownership 守卫 + chat 未来扩展）；
新增 idx_session_msg_channel_time + idx_session_msg_user_channel_time 两个复合索引。
存量零破坏：channel 带 server_default='chat'，citations/user_id 为 nullable。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_message",
        sa.Column(
            "channel",
            sa.String(16),
            nullable=False,
            server_default="chat",
        ),
    )
    op.add_column(
        "session_message",
        sa.Column("citations", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "session_message",
        sa.Column("user_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "idx_session_msg_channel_time",
        "session_message",
        ["channel", "session_id", "created_time"],
    )
    op.create_index(
        "idx_session_msg_user_channel_time",
        "session_message",
        ["user_id", "channel", "created_time"],
    )


def downgrade() -> None:
    op.drop_index("idx_session_msg_user_channel_time", table_name="session_message")
    op.drop_index("idx_session_msg_channel_time", table_name="session_message")
    op.drop_column("session_message", "user_id")
    op.drop_column("session_message", "citations")
    op.drop_column("session_message", "channel")
```

- [ ] **Step 4: 跑迁移**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/alembic upgrade head`
Expected: 迁移成功；输出 `Running upgrade 0046 -> 0047, doc_qa session_message 扩列`

- [ ] **Step 5: 改 SessionMessage ORM**

修改 `backend/app/domain/models.py:691-710`：
```python
class SessionMessage(Base, TimestampMixin):
    """会话消息持久化表。

    role 取值：user / assistant。question 与 sql_generated 仅在 assistant 侧回填。
    channel（0047）：chat / doc_qa 渠道隔离。
    citations（0047）：仅 doc_qa 的 assistant 行填引用 chunks JSON；chat 恒 NULL。
    user_id（0047）：doc_qa ownership 守卫必填；chat 存量行 NULL，新行 API 层透传。
    读取按 (session_id, created_time) 索引，保留最近 N 轮作为上下文。
    """

    __tablename__ = "session_message"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql_generated: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str] = mapped_column(String(16), default="chat", nullable=False)
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=True,
    )
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_session_msg_time", "session_id", "created_time"),
        Index("idx_session_msg_channel_time", "channel", "session_id", "created_time"),
        Index("idx_session_msg_user_channel_time", "user_id", "channel", "created_time"),
    )

    def __repr__(self) -> str:
        return f"<SessionMessage id={self.id} session={self.session_id} role={self.role} channel={self.channel}>"
```

- [ ] **Step 6: 跑测试，验证 PASS**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/integration/test_session_message_columns.py -v`
Expected: 2 PASS

- [ ] **Step 7: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/alembic/versions/0047_doc_qa_message.py \
        backend/app/domain/models.py \
        backend/app/tests/integration/test_session_message_columns.py
git commit -m "feat(db): session_message + channel/citations/user_id (Alembic 0047)

为 doc_qa Tab 4 准备：channel 列隔离 chat/doc_qa 两渠道；
citations JSONB 存 doc_qa 引用；user_id 用于 doc_qa ownership 守卫。
存量零破坏：channel 带 default='chat'，citations/user_id 为 nullable。
新增 idx_session_msg_channel_time 与 idx_session_msg_user_channel_time 两索引。"
```

---

## Task 2: DocQaRequest schema + Stream events + Prompt module

**Files:**
- Modify: `backend/app/domain/schemas.py`（追加 `DocQaRequest` 类）
- Modify: `backend/app/services/stream_events.py`（追加 3 个事件常量）
- Create: `backend/app/services/rag_qa_prompt.py`
- Test: `backend/app/tests/unit/test_rag_qa_prompt.py`

**Interfaces:**
- Consumes: 既有 `CamelModel` 基类 + `Field`；既有 `EVENT_TOKEN` / `EVENT_ERROR` 常量
- Produces:
  - `DocQaRequest` (CamelModel) — `session_id: str` (1-64) / `question: str` (min 1) / `top_k: int` (default 8, 1-20) / `security_level: str | None` / `document_type: str | None` / `model_id: int | None`
  - `EVENT_QA_META: str = "qa_meta"` / `EVENT_QA_CITATIONS: str = "qa_citations"` / `EVENT_QA_DONE: str = "qa_done"`
  - `DOC_QA_SYSTEM_PROMPT: str` / `DOC_QA_USER_TEMPLATE: str` / `format_chunks_json(chunks) -> str` / `format_history_block(messages) -> str`

- [ ] **Step 1: 写失败测试 — prompt helpers 行为正确**

新建 `backend/app/tests/unit/test_rag_qa_prompt.py`：
```python
"""纯模块 rag_qa_prompt 测试。"""
import json

from app.services.rag_qa_prompt import (
    DOC_QA_SYSTEM_PROMPT,
    DOC_QA_USER_TEMPLATE,
    format_chunks_json,
    format_history_block,
)


def test_format_chunks_json_assigns_ids_one_indexed() -> None:
    chunks = [
        {"document_id": "DOC-A", "document_name": "合同", "chunk_text": "条款1"},
        {"document_id": "DOC-B", "document_name": "指南", "chunk_text": "段落"},
    ]
    result = format_chunks_json(chunks)
    parsed = json.loads(result)
    assert parsed[0]["id"] == 1
    assert parsed[1]["id"] == 2
    assert parsed[0]["document_name"] == "合同"


def test_format_history_block_plain_text_only() -> None:
    """历史块只含 role: content，不含 citations JSON。"""
    msgs = [
        {"role": "user", "content": "什么是质量协议？"},
        {"role": "assistant", "content": "质量协议是..."},
    ]
    out = format_history_block(msgs)
    assert "user: 什么是质量协议？" in out
    assert "assistant: 质量协议是..." in out
    assert "citations" not in out
    assert "{" not in out  # 不应含 JSON 结构


def test_system_prompt_contains_chunk_placeholder() -> None:
    assert "{chunks_json}" in DOC_QA_SYSTEM_PROMPT
    assert "[1]" in DOC_QA_SYSTEM_PROMPT  # 引用规则提示


def test_user_template_has_placeholders() -> None:
    assert "{history_block}" in DOC_QA_USER_TEMPLATE
    assert "{question}" in DOC_QA_USER_TEMPLATE
```

- [ ] **Step 2: 跑测试，验证 FAIL（模块不存在）**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/unit/test_rag_qa_prompt.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 加 DocQaRequest schema**

修改 `backend/app/domain/schemas.py`，在已有 `ChatRequest` 类附近追加：
```python
class DocQaRequest(CamelModel):
    """文档问答请求（与 ChatRequest 解耦）。"""

    session_id: str = Field(..., min_length=1, max_length=64)
    question: str = Field(..., min_length=1)
    top_k: int = Field(default=8, ge=1, le=20)
    security_level: str | None = None
    document_type: str | None = None
    model_id: int | None = None
```

- [ ] **Step 4: 加 3 个流式事件常量**

修改 `backend/app/services/stream_events.py`：
```python
# 在 EVENT_TOKEN / EVENT_ERROR 附近追加
EVENT_QA_META: str = "qa_meta"
EVENT_QA_CITATIONS: str = "qa_citations"
EVENT_QA_DONE: str = "qa_done"
```

- [ ] **Step 5: 写 rag_qa_prompt 模块**

新建 `backend/app/services/rag_qa_prompt.py`：
```python
"""文档问答 prompt 模板 + 纯函数 helper（无 IO）。

DOC_QA_SYSTEM_PROMPT：包含引用规则的 system prompt；
DOC_QA_USER_TEMPLATE：history + question 拼接模板；
format_chunks_json：给 chunks 列表加 id 字段并序列化为 JSON 字符串（LLM 用 id 标注 [1][2]）；
format_history_block：上 5 轮对话拼成纯文本（不含 citations JSON）。
"""
from __future__ import annotations

import json
from typing import Any

DOC_QA_SYSTEM_PROMPT = """你是文档问答助手。基于以下引用的文档内容回答用户问题。

规则：
1. 仅基于提供的文档内容回答，不要编造。
2. 引用处用 [1]、[2] 等编号标注，对应下方引用列表。
3. 文档无相关信息时，明确说明「未在已上传文档中找到相关依据」。
4. 用中文回答，简洁准确。

引用文档：
{chunks_json}"""

DOC_QA_USER_TEMPLATE = "{history_block}\n\n当前问题：{question}"


def format_chunks_json(chunks: list[dict[str, Any]]) -> str:
    """为每个 chunk 加 1-indexed id，返回 JSON 字符串给 LLM 当引用表。

    输入：rag_service.searchDocuments 返回的 chunk 字典列表（含 document_id /
        document_name / chunk_text / score 等）。
    输出：JSON 字符串，每项含 id 字段 + 原字段拷贝。
    """
    with_id = [{"id": idx + 1, **c} for idx, c in enumerate(chunks)]
    return json.dumps(with_id, ensure_ascii=False, default=str)


def format_history_block(messages: list[dict[str, str]]) -> str:
    """把上 N 轮对话拼成纯文本 role: content 列表。

    不含 citations JSON（避免 prompt 膨胀）。
    返回新字符串，不修改入参。
    """
    lines = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


__all__ = [
    "DOC_QA_SYSTEM_PROMPT",
    "DOC_QA_USER_TEMPLATE",
    "format_chunks_json",
    "format_history_block",
]
```

- [ ] **Step 6: 跑测试，验证 PASS**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/unit/test_rag_qa_prompt.py -v`
Expected: 4 PASS

- [ ] **Step 7: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/domain/schemas.py \
        backend/app/services/stream_events.py \
        backend/app/services/rag_qa_prompt.py \
        backend/app/tests/unit/test_rag_qa_prompt.py
git commit -m "feat(rag-qa): DTO + SSE events + prompt module

DocQaRequest 与 chat 解耦；EVENT_QA_META/CITATIONS/DONE 三事件复用
现有 StreamEvent.toSse；rag_qa_prompt 纯模块无 IO，含 4 个测试覆盖
id 编号 + 历史块纯文本 + prompt 占位符。"
```

---

## Task 3: RagQaService — history loading + no-chunks 模板短路

**Files:**
- Create: `backend/app/services/rag_qa_service.py`
- Test: `backend/app/tests/unit/test_rag_qa_service.py`

**Interfaces:**
- Consumes: `DocQaRequest`（Task 2）/ `SessionMessage` ORM（Task 1）/ `RagService.searchDocuments`（既有）/ `stream_events`（Task 2）/ `prompt` helpers（Task 2）
- Produces:
  - `RagQaService` 类
  - `async def load_history(session, session_id, *, limit=5) -> list[dict]`（返回 `[{role, content}]`，仅 doc_qa channel，不含 citations）
  - `async def answer_stream(self, session, dto, *, actor, configs) -> AsyncIterator[StreamEvent]`：检索 + meta + citations 事件先行；当 `not chunks or chunks[0]["score"] < 0.3` 时 yield token(template) + done 并返回

- [ ] **Step 1: 写失败测试 — history 加载只取 doc_qa + 纯文本**

新建 `backend/app/tests/unit/test_rag_qa_service.py`：
```python
"""RagQaService 单元测试（纯函数 + mock 注入）。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.dependencies import CurrentUser
from app.domain.schemas import DocQaRequest
from app.services.rag_qa_service import RagQaService
from app.services.stream_events import EVENT_QA_DONE, EVENT_QA_META


@pytest.mark.asyncio
async def test_load_history_returns_plain_text_doc_qa_only() -> None:
    """load_history 只取 channel='doc_qa'，返回 [{role, content}] 无 citations。"""
    svc = RagQaService()

    # 模拟 session.execute 返回的 scalars
    fake_row_user = MagicMock(role="user", content="什么是质量协议？", citations=None, channel="doc_qa")
    fake_row_asst = MagicMock(role="assistant", content="质量协议是...", citations=[{"id": 1}], channel="doc_qa")
    fake_row_chat = MagicMock(role="user", content="chat 消息", citations=None, channel="chat")  # 必须被过滤

    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[fake_row_user, fake_row_asst, fake_row_chat])
    execute = AsyncMock(return_value=MagicMock(scalars=lambda: scalars))

    session = MagicMock()
    session.execute = execute

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
    assert len(events) == 4
    assert events[0].type == EVENT_QA_META
    assert events[1].type == "qa_citations"
    assert events[1].payload["citations"] == []
    assert "未在已上传文档中找到相关依据" in events[2].payload["content"]
    assert events[3].type == EVENT_QA_DONE
    svc._llm_factory.assert_not_called()  # 关键：没调 LLM
```

- [ ] **Step 2: 跑测试，验证 FAIL（类不存在）**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/unit/test_rag_qa_service.py -v`
Expected: FAIL with `ModuleNotFoundError: app.services.rag_qa_service`

- [ ] **Step 3: 写 RagQaService 骨架（含 history loading + no-chunks 短路）**

新建 `backend/app/services/rag_qa_service.py`：
```python
"""文档问答 RAG 服务。

编排：加载上 5 轮历史 → Milvus 检索 → LLM 流式合成 → 落库。
无相关 chunks（top1 score < 0.3）走固定模板，零 LLM 调用。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.models import LlmConfig, SessionMessage
from app.domain.schemas import DocQaRequest
from app.infrastructure.llm.base_client import BaseLlmClient, LlmMessage
from app.services.rag_qa_prompt import (
    DOC_QA_SYSTEM_PROMPT,
    DOC_QA_USER_TEMPLATE,
    format_chunks_json,
    format_history_block,
)
from app.services.rag_service import RagService
from app.services.stream_events import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_QA_CITATIONS,
    EVENT_QA_DONE,
    EVENT_QA_META,
    EVENT_TOKEN,
    StreamEvent,
)

logger = logging.getLogger(__name__)

_HISTORY_ROUNDS = 5
_NO_CHUNKS_TEMPLATE = "未在已上传文档中找到相关依据。"
_SCORE_THRESHOLD = 0.3
LlmFactory = Any  # Callable[[LlmConfig], BaseLlmClient]


class RagQaService:
    """doc_qa 全流程编排。"""

    def __init__(
        self,
        *,
        rag_service: RagService | None = None,
        llm_factory: LlmFactory | None = None,
    ) -> None:
        self._rag_svc = rag_service or RagService()
        self._llm_factory = llm_factory

    async def load_history(
        self,
        session: AsyncSession,
        session_id: str,
        *,
        limit: int = _HISTORY_ROUNDS,
    ) -> list[dict[str, str]]:
        """加载上 N 轮 doc_qa 消息，返回 [{role, content}]，不含 citations。

        按 created_time 倒序取最近 N 条；过滤 channel='doc_qa'。
        返回新列表，不修改入参。
        """
        stmt = (
            select(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.channel == "doc_qa",
            )
            .order_by(SessionMessage.created_time.desc())
            .limit(limit * 2)  # 取两倍以凑齐 N 轮 user+assistant
        )
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        # 倒序恢复时间正序；只保留 role + content，丢弃 citations
        out: list[dict[str, str]] = []
        for row in reversed(rows):
            out.append({"role": row.role, "content": row.content})
            if len(out) >= limit * 2:
                break
        return out

    async def answer_stream(
        self,
        session: AsyncSession,
        dto: DocQaRequest,
        *,
        actor: CurrentUser,
        configs: list[LlmConfig],
    ) -> AsyncIterator[StreamEvent]:
        """一轮 doc_qa SSE 事件序列（流式生成器）。

        无相关 chunks → meta + citations=[] + token(template) + done，零 LLM 调用；
        有 chunks → meta + citations + token×N + done，期间调 LLM。
        """
        # 1. 加载历史
        history = await self.load_history(session, dto.session_id)
        history_block = format_history_block(history)

        # 2. 检索
        chunks = await self._rag_svc.searchDocuments(
            dto.question,
            top_k=dto.top_k,
            security_level=dto.security_level,
            session=session,
        )

        # 3. 加 id 给 LLM 引用
        citations = [{"id": idx + 1, **c} for idx, c in enumerate(chunks)]

        # 4. 事件先行：meta + citations
        yield StreamEvent(EVENT_QA_META, {"intent": "doc_qa"})
        yield StreamEvent(EVENT_QA_CITATIONS, {"citations": citations})

        # 5. 短路：top1 score 过低 → 固定模板
        if not chunks or chunks[0].get("score", 0.0) < _SCORE_THRESHOLD:
            yield StreamEvent(EVENT_TOKEN, {"content": _NO_CHUNKS_TEMPLATE})
            yield StreamEvent(EVENT_QA_DONE, {"tokensUsed": 0, "cost": 0.0, "modelName": None})
            await self._persist(session, dto, _NO_CHUNKS_TEMPLATE, citations, actor=actor)
            return

        # 6. LLM 流式（占位，Task 4 实现）
        # 此处保留供 Task 4 填充完整 LLM 流式逻辑
        raise NotImplementedError("Task 4 填充：LLM 流式 + 落库 + done 事件")
```

- [ ] **Step 4: 跑测试，验证 PASS**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/unit/test_rag_qa_service.py -v`
Expected: 2 PASS

- [ ] **Step 5: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/services/rag_qa_service.py \
        backend/app/tests/unit/test_rag_qa_service.py
git commit -m "feat(rag-qa): RagQaService skeleton + history + no-chunks short circuit

load_history 仅取 channel='doc_qa' 上 N 轮纯文本；
answer_stream 无 chunks / top1<0.3 时走固定模板，零 LLM 调用；
LLM 流式部分留 Task 4 填充。"
```

---

## Task 4: RagQaService — LLM 流式 + persist + done

**Files:**
- Modify: `backend/app/services/rag_qa_service.py:107-120`（填充 `raise NotImplementedError` 块）
- Test: `backend/app/tests/unit/test_rag_qa_service.py`（追加 3 个用例）

**Interfaces:**
- Consumes: `BaseLlmClient.completeStream` 既有接口（返回 `AsyncIterator[StreamChunk]`，末块 `isDone=True` 携带 token）
- Produces: `answer_stream` 完整实现 — yield `EVENT_TOKEN` × N + `EVENT_QA_DONE` 携带 tokensUsed/cost/modelName；流结束后 `_persist` 落库 user + assistant 行

- [ ] **Step 1: 追加失败测试 — 完整流式事件序列**

修改 `backend/app/tests/unit/test_rag_qa_service.py`（追加）：
```python
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

    client.completeStream = _stream
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

    dto = DocQaRequest(session_id="sess-1", question="什么是质量协议？", top_k=8)
    actor = CurrentUser(userId="u-1", departments=[])
    cfg = MagicMock(id=1, model_name="fake-mdl", cost_per_1k_input=0.001, cost_per_1k_output=0.002)

    events: list = []
    async for ev in svc.answer_stream(session, dto, actor=actor, configs=[cfg]):
        events.append(ev)

    # 序列：1 meta + 1 citations + 3 token + 1 done
    assert len(events) == 6
    assert events[0].type == "qa_meta"
    assert events[1].type == "qa_citations"
    assert events[1].payload["citations"][0]["id"] == 1
    assert events[2].type == EVENT_TOKEN
    assert events[2].payload["content"] == "根据"
    assert events[3].payload["content"] == "合同条款"
    assert events[4].payload["content"] == "..."
    assert events[5].type == EVENT_QA_DONE
    # done 携带 token 统计
    assert events[5].payload["modelName"] == "fake-mdl"
    assert events[5].payload["tokensUsed"] > 0


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

    dto = DocQaRequest(session_id="sess-1", question="更详细说说", top_k=8)
    actor = CurrentUser(userId="u-1", departments=[])
    cfg = MagicMock(id=1, model_name="fake-mdl")

    async for _ in svc.answer_stream(session, dto, actor=actor, configs=[cfg]):
        pass

    # 验证 LLM 收到的 messages 含历史纯文本
    fake_client.completeStream.assert_called_once()
    messages_arg = fake_client.completeStream.call_args.args[0]
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

    dto = DocQaRequest(session_id="sess-1", question="问题", top_k=8)
    actor = CurrentUser(userId="u-1", departments=[])
    cfg = MagicMock(id=1, model_name="fake-mdl")

    async for _ in svc.answer_stream(session, dto, actor=actor, configs=[cfg]):
        pass

    # session.add 必须被调 ≥ 2 次（user + assistant）
    assert session.add.call_count >= 2
    added_entities = [c.args[0] for c in session.add.call_args_list]
    # 找到 user 行（content=dto.question）和 assistant 行（content=回答）
    user_rows = [e for e in added_entities if e.content == "问题" and e.role == "user"]
    asst_rows = [e for e in added_entities if e.content == "回答" and e.role == "assistant"]
    assert len(user_rows) == 1
    assert len(asst_rows) == 1
    assert user_rows[0].channel == "doc_qa"
    assert user_rows[0].user_id == "u-1"
    assert asst_rows[0].citations is not None
    assert asst_rows[0].citations[0]["document_id"] == "DOC-A"
```

- [ ] **Step 2: 跑测试，验证 FAIL（NotImplementedError）**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/unit/test_rag_qa_service.py -v`
Expected: 3 FAIL（`NotImplementedError` + history 注入未实现 + persist 未实现）

- [ ] **Step 3: 在 RagQaService 填充 LLM 流式 + persist**

修改 `backend/app/services/rag_qa_service.py`，把 `raise NotImplementedError` 替换为：

```python
        # 6. 选模型（dto.model_id 优先）
        if dto.model_id is not None:
            selected = next((c for c in configs if c.id == dto.model_id), None)
            if selected is None:
                # 与 chat 一致：未找到抛 MSG_MODEL_CONFIG_UNAVAILABLE
                from app.services.messages_zh import MSG_MODEL_CONFIG_UNAVAILABLE
                raise ValueError(MSG_MODEL_CONFIG_UNAVAILABLE.format(id=dto.model_id))
        else:
            # 默认走 router；doc_qa 摘要任务降级到最便宜
            from app.services.model_router_service import ModelRouterService, RoutingContext
            router = ModelRouterService()
            selected = router.selectModel(
                configs, dto.question,
                RoutingContext(sessionId=dto.session_id),
            )

        # 7. 拼 prompt
        client = self._llm_factory(selected) if self._llm_factory else None
        if client is None:
            raise RuntimeError("llm_factory 未配置")

        chunks_json = format_chunks_json(chunks)
        messages: list[LlmMessage] = [
            LlmMessage(role="system", content=DOC_QA_SYSTEM_PROMPT.format(chunks_json=chunks_json)),
            LlmMessage(
                role="user",
                content=DOC_QA_USER_TEMPLATE.format(
                    history_block=history_block, question=dto.question,
                ),
            ),
        ]

        # 8. 流式 LLM
        full_answer = ""
        total_pt = 0
        total_ct = 0
        async for chunk in client.completeStream(messages, model=selected.model_name):
            full_answer += chunk.content
            if chunk.content:
                yield StreamEvent(EVENT_TOKEN, {"content": chunk.content})
            if chunk.isDone:
                total_pt = chunk.promptTokens
                total_ct = chunk.completionTokens

        # 9. 计算 cost
        cost = (
            Decimal(total_pt) * Decimal(str(selected.cost_per_1k_input))
            + Decimal(total_ct) * Decimal(str(selected.cost_per_1k_output))
        ) / Decimal(1000)

        # 10. 落库（user + assistant）
        await self._persist(session, dto, full_answer, citations, actor=actor)

        # 11. done
        yield StreamEvent(
            EVENT_QA_DONE,
            {
                "tokensUsed": total_pt + total_ct,
                "cost": float(cost),
                "modelName": selected.model_name,
            },
        )
```

并在文件顶部加 `from decimal import Decimal`，类内追加私有方法 `_persist`：

```python
    async def _persist(
        self,
        session: AsyncSession,
        dto: DocQaRequest,
        answer: str,
        citations: list[dict[str, Any]],
        *,
        actor: CurrentUser,
    ) -> None:
        """落库 user 行 + assistant 行（doc_qa channel）。"""
        user_msg = SessionMessage(
            session_id=dto.session_id,
            role="user",
            content=dto.question,
            channel="doc_qa",
            user_id=actor.userId,
        )
        session.add(user_msg)

        asst_msg = SessionMessage(
            session_id=dto.session_id,
            role="assistant",
            content=answer,
            question=dto.question,
            citations=citations or None,
            channel="doc_qa",
            user_id=actor.userId,
        )
        session.add(asst_msg)
        await session.flush()
```

- [ ] **Step 4: 跑测试，验证 PASS**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/unit/test_rag_qa_service.py -v`
Expected: 5 PASS

- [ ] **Step 5: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/services/rag_qa_service.py \
        backend/app/tests/unit/test_rag_qa_service.py
git commit -m "feat(rag-qa): LLM streaming + persist in RagQaService

completeStream 集成；选模型支持 dto.model_id 优先；
成本计算 = (pt*input_cost + ct*output_cost)/1000；
落库 user + assistant 两行（channel='doc_qa', user_id 必填）；
assistant 行 citations JSONB 写入引用 chunk 列表。"
```

---

## Task 5: API endpoint POST /api/v1/documents/qa

**Files:**
- Modify: `backend/app/api/v1/documents.py`（追加端点）
- Test: `backend/app/tests/integration/test_doc_qa_api.py`

**Interfaces:**
- Consumes: `DocQaRequest` / `CurrentUser` / `getDb` / `RagQaService`（既有 Task 3+4）/ 既有 `ModelConfigService` / `rate_limit.limiter` / `StreamingResponse`
- Produces: `POST /api/v1/documents/qa` 端点 — 复用 chat 的 `@limiter.limit(rateLimitValue)`、SSE `text/event-stream`、404/422 错误处理；session ownership 守卫（422 不属于自己的 session）

- [ ] **Step 1: 写失败测试 — 端点可访问 + SSE 头正确**

新建 `backend/app/tests/integration/test_doc_qa_api.py`：
```python
"""POST /api/v1/documents/qa 端点集成测试（真实 PG）。"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_doc_qa_endpoint_requires_auth() -> None:
    """未登录访问应被 401 拒绝。"""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/v1/documents/qa",
            json={"sessionId": "sess-x", "question": "test"},
        )
    # 401 or 403 由 auth 决定；关键是拒绝
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_doc_qa_endpoint_returns_sse_when_authed() -> None:
    """登录用户访问返回 text/event-stream + 200。"""
    # 假设有 test fixtures 提供 auth cookie / header
    from tests.conftest import auth_headers

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers=auth_headers("u-test"),
    ) as client:
        resp = await client.post(
            "/api/v1/documents/qa",
            json={"sessionId": "sess-new", "question": "什么是质量协议？"},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
```

- [ ] **Step 2: 跑测试，验证 FAIL（端点未注册）**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/integration/test_doc_qa_api.py -v`
Expected: FAIL（404 Not Found）

- [ ] **Step 3: 加端点**

修改 `backend/app/api/v1/documents.py`，在 `/search` 端点后追加：
```python
@router.post("/qa")
@limiter.limit(rateLimitValue)
async def docQa(
    request: Request,
    dto: DocQaRequest,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> StreamingResponse:
    """文档问答 SSE 流式端点。

    流程：ownership 守卫 → 加载 LlmConfigs → RagQaService.answer_stream →
    每个 StreamEvent 走 toSse 序列化。
    """
    from app.services.rag_qa_service import RagQaService
    from app.services.stream_events import StreamEvent

    # ownership 守卫：session 不属于当前用户 → 422
    from app.services.messages_zh import MSG_SESSION_NOT_OWNED
    from sqlalchemy import select, func
    from app.domain.models import SessionMessage
    if dto.session_id:
        stmt = (
            select(func.count())
            .where(
                SessionMessage.session_id == dto.session_id,
                SessionMessage.channel == "doc_qa",
                SessionMessage.user_id == _user.userId,
            )
        )
        result = await session.execute(stmt)
        if (result.scalar() or 0) == 0:
            raise HTTPException(
                status_code=422,
                detail=MSG_SESSION_NOT_OWNED,
            )

    # 加载可用模型
    from app.services.model_config_service import ModelConfigService
    configs = await ModelConfigService().listActiveConfigs(session)

    svc = RagQaService()

    async def eventSource() -> AsyncIterator[str]:
        try:
            async for event in svc.answer_stream(
                session, dto, actor=_user, configs=configs,
            ):
                yield event.toSse()
        except RagError as exc:
            yield StreamEvent(
                EVENT_ERROR,
                {"error": exc.message if hasattr(exc, "message") else str(exc), "errorType": "LLM"},
            ).toSse()
        except DomainError as exc:
            yield StreamEvent(
                EVENT_ERROR,
                {"error": exc.message, "errorType": "DOMAIN", "detail": exc.detail},
            ).toSse()

    return StreamingResponse(
        eventSource(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

并在文件顶部 imports 追加：
```python
from collections.abc import AsyncIterator
from app.domain.schemas import DocQaRequest
from app.domain.exceptions import DomainError, RagError if not exists else ...
from app.infrastructure.rate_limit import limiter, rateLimitValue
from app.services.stream_events import EVENT_ERROR
```

- [ ] **Step 4: 跑测试，验证 PASS**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/integration/test_doc_qa_api.py -v`
Expected: 2 PASS

- [ ] **Step 5: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/api/v1/documents.py \
        backend/app/tests/integration/test_doc_qa_api.py
git commit -m "feat(api): POST /api/v1/documents/qa SSE endpoint

复用 chat 的 rate_limit + StreamingResponse；ownership 守卫确保
doc_qa session 必属于当前用户；RagError/DomainError 转 SSE 错误事件。"
```

---

## Task 6: session_history_service + session.py 端点 channel 过滤

**Files:**
- Modify: `backend/app/services/session_history_service.py:48-93`
- Modify: `backend/app/api/v1/session.py:97-113`
- Test: `backend/app/tests/unit/test_session_history_channel.py`

**Interfaces:**
- Consumes: `listChatSessions(session, *, limit, offset)` 既有
- Produces:
  - `listChatSessions(session, *, limit, offset, channel="chat") -> list[ChatSessionListItem]`
  - `GET /api/v1/sessions/chat-history?channel=doc_qa&limit=...&offset=...`
  - `GET /api/v1/sessions/{sessionId}/messages?channel=doc_qa`（若已存在）

- [ ] **Step 1: 写失败测试 — channel 过滤正确**

新建 `backend/app/tests/unit/test_session_history_channel.py`：
```python
"""session_history_service.channel 过滤测试。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.session_history_service import SessionHistoryService


@pytest.mark.asyncio
async def test_listChatSessions_filters_by_channel() -> None:
    """channel='doc_qa' 时 SQL 必须含 channel='doc_qa' 过滤。"""
    svc = SessionHistoryService()
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: []))
    )

    await svc.listChatSessions(session, limit=10, offset=0, channel="doc_qa")

    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "channel = 'doc_qa'" in compiled
```

- [ ] **Step 2: 跑测试，验证 FAIL**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/unit/test_session_history_channel.py -v`
Expected: FAIL（`TypeError: listChatSessions() got unexpected keyword 'channel'`）

- [ ] **Step 3: 改 listChatSessions 加 channel 参数**

修改 `backend/app/services/session_history_service.py:48`：
```python
    async def listChatSessions(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
        offset: int = 0,
        channel: str = "chat",
    ) -> list[ChatSessionListItem]:
        """列出历史会话。channel='chat'/'doc_qa' 隔离。"""
        # 在原 SQL 上加 WHERE channel=:channel
        ...
```

并在 GROUP BY 子句 + WHERE 子句加 channel 过滤。

- [ ] **Step 4: 改 session.py 端点透传 channel**

修改 `backend/app/api/v1/session.py:97-113`：
```python
@router.get("/chat-history", response_model=list[ChatSessionListItem])
async def listChatSessions(
    channel: str = Query(default="chat", pattern="^(chat|doc_qa)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[ChatSessionListItem]:
    return await svc.listChatSessions(session, limit=limit, offset=offset, channel=channel)
```

- [ ] **Step 5: 跑测试，验证 PASS**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/unit/test_session_history_channel.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/services/session_history_service.py \
        backend/app/api/v1/session.py \
        backend/app/tests/unit/test_session_history_channel.py
git commit -m "feat(api): chat-history endpoint accepts channel filter

支持 channel='doc_qa' 拉取文档问答历史会话；
默认 'chat' 保持既有 chat 行为不变。"
```

---

## Task 7: 前端类型 + api 客户端 + chatStore channel

**Files:**
- Modify: `frontend/src/types/document.ts`
- Modify: `frontend/src/api/document.ts`
- Modify: `frontend/src/stores/chatStore.ts`
- Test: `frontend/src/tests/chatStoreDocQa.test.ts`

**Interfaces:**
- Consumes: 既有 `RagSearchResult` 类型 / 既有 `chatStore` 接口
- Produces:
  - `DocQaCitation` (interface) — `{id, document_id, document_name, chunk_text, score}`
  - `DocQaSseEvent` (type) — `kind: 'meta'|'citations'|'token'|'done'|'error'`
  - `sendDocQa(question, filters, sessionId)` 方法

- [ ] **Step 1: 写失败测试 — chatStore channel 切换 sessionId 前缀**

新建 `frontend/src/tests/chatStoreDocQa.test.ts`：
```typescript
import { useChatStore } from "../stores/chatStore";

describe("chatStore channel field", () => {
  beforeEach(() => {
    useChatStore.getState().resetSession();
    useChatStore.setState({ channel: "chat" });
  });

  it("resetSession assigns chat- prefix when channel=chat", () => {
    useChatStore.getState().resetSession();
    const sid = useChatStore.getState().sessionId;
    expect(sid).toMatch(/^chat-/);
  });

  it("resetSession assigns docqa- prefix when channel=doc_qa", () => {
    useChatStore.setState({ channel: "doc_qa" });
    useChatStore.getState().resetSession();
    const sid = useChatStore.getState().sessionId;
    expect(sid).toMatch(/^docqa-/);
  });
});
```

- [ ] **Step 2: 跑测试，验证 FAIL（chatStore 无 channel 字段）**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend && npm test -- chatStoreDocQa.test.ts`
Expected: FAIL（TypeScript 或运行时错误）

- [ ] **Step 3: 加 DocQa 类型到 document.ts**

修改 `frontend/src/types/document.ts`：
```typescript
export interface DocQaCitation {
  id: number;
  document_id: string;
  document_name: string;
  chunk_text: string;
  score: number;
}

export type DocQaSseEvent =
  | { kind: "meta"; intent: "doc_qa" }
  | { kind: "citations"; citations: DocQaCitation[] }
  | { kind: "token"; content: string }
  | { kind: "done"; tokensUsed: number; cost: number; modelName: string | null }
  | { kind: "error"; error: string; errorType: string };

export interface DocQaRequestPayload {
  sessionId: string;
  question: string;
  topK?: number;
  securityLevel?: string;
  documentType?: string;
  modelId?: number;
}
```

- [ ] **Step 4: 加 searchDocumentsQa SSE 客户端**

修改 `frontend/src/api/document.ts`：
```typescript
import type { DocQaRequestPayload, DocQaSseEvent, DocQaCitation } from "../types/document";

export async function searchDocumentsQa(
  payload: DocQaRequestPayload,
  onEvent: (event: DocQaSseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch("/api/v1/documents/qa", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // 按 \n\n 拆 SSE event
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      const dataLine = part.split("\n").find((l) => l.startsWith("data: "));
      if (!dataLine) continue;
      const dataStr = dataLine.slice("data: ".length);
      try {
        const obj = JSON.parse(dataStr);
        // kind 由 event 字段推断
        const evType = part.split("\n").find((l) => l.startsWith("event: "))?.slice("event: ".length).trim();
        onEvent({ ...obj, kind: evTypeToKind(evType) });
      } catch {
        // 跳过解析失败
      }
    }
  }
}

function evTypeToKind(t: string | undefined): DocQaSseEvent["kind"] {
  switch (t) {
    case "qa_meta": return "meta";
    case "qa_citations": return "citations";
    case "token": return "token";
    case "qa_done": return "done";
    case "error": return "error";
    default: return "error";
  }
}
```

- [ ] **Step 5: chatStore 加 channel 字段 + sendDocQa**

修改 `frontend/src/stores/chatStore.ts`：
```typescript
interface ChatStore {
  // ... 既有字段 ...
  channel: "chat" | "doc_qa";
  setChannel: (channel: "chat" | "doc_qa") => void;
  sendDocQa: (question: string, filters: { securityLevel?: string; documentType?: string }) => Promise<void>;
}

// 实现：
// setChannel: (c) => set({ channel: c, sessionId: makeSessionId(c) }),
// sendDocQa: 异步；调用 searchDocumentsQa；逐事件 dispatch（meta/citations/token/done/error）
```

sessionId 生成：`makeSessionId(channel) => \`${channel === 'doc_qa' ? 'docqa-' : 'chat-'}${crypto.randomUUID()}\``

- [ ] **Step 6: 跑测试，验证 PASS**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend && npm test -- chatStoreDocQa.test.ts`
Expected: 2 PASS

- [ ] **Step 7: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/types/document.ts \
        frontend/src/api/document.ts \
        frontend/src/stores/chatStore.ts \
        frontend/src/tests/chatStoreDocQa.test.ts
git commit -m "feat(frontend): DocQa types + SSE client + chatStore channel

DocQaCitation/SseEvent/RequestPayload 三类型；
searchDocumentsQa 客户端解析 SSE；
chatStore channel 字段 + sessionId 前缀区分 chat/doc_qa；
sendDocQa 方法走 SSE 流式路径。"
```

---

## Task 8: DocumentQaCitationList 组件

**Files:**
- Create: `frontend/src/components/documents/DocumentQaCitationList.tsx`
- Test: `frontend/src/tests/DocumentQaCitationList.test.tsx`

**Interfaces:**
- Consumes: `DocQaCitation[]` 类型（Task 7）
- Produces: `<DocumentQaCitationList citations={...} />` — 渲染 N 个卡片，每卡片含 `id`（右上角 [n]）/ `document_name` / `score%` / `chunk_text` 摘要（4 行 + 展开）

- [ ] **Step 1: 写失败测试 — 渲染卡片 + id 高亮**

新建 `frontend/src/tests/DocumentQaCitationList.test.tsx`：
```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DocumentQaCitationList } from "../components/documents/DocumentQaCitationList";

describe("DocumentQaCitationList", () => {
  it("renders one card per citation with id and document_name", () => {
    const citations = [
      { id: 1, document_id: "DOC-A", document_name: "合同 V2", chunk_text: "条款...", score: 0.85 },
      { id: 2, document_id: "DOC-B", document_name: "指南", chunk_text: "段落...", score: 0.62 },
    ];
    render(<DocumentQaCitationList citations={citations} />);
    expect(screen.getByText("[1]")).toBeTruthy();
    expect(screen.getByText("[2]")).toBeTruthy();
    expect(screen.getByText("合同 V2")).toBeTruthy();
    expect(screen.getByText("指南")).toBeTruthy();
    expect(screen.getByText("85.0%")).toBeTruthy();
  });
});
```

- [ ] **Step 2: 跑测试，验证 FAIL（组件不存在）**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend && npm test -- DocumentQaCitationList.test.tsx`
Expected: FAIL（找不到模块）

- [ ] **Step 3: 写组件**

新建 `frontend/src/components/documents/DocumentQaCitationList.tsx`：
```tsx
import { Card, Typography } from "antd";
import type { DocQaCitation } from "../../types/document";

const { Paragraph } = Typography;

interface Props {
  citations: DocQaCitation[];
}

export function DocumentQaCitationList({ citations }: Props) {
  if (!citations.length) return null;
  return (
    <div className="doc-qa-citation-list">
      {citations.map((c) => (
        <Card
          key={c.id}
          size="small"
          title={
            <span>
              <span className="doc-qa-citation-id">[{c.id}]</span>{" "}
              {c.document_name}
            </span>
          }
          extra={<span className="doc-qa-citation-score">{(c.score * 100).toFixed(1)}%</span>}
          style={{ marginBottom: 8 }}
          data-citation-id={c.id}
        >
          <Paragraph ellipsis={{ rows: 4, expandable: true, symbol: "展开" }} style={{ marginBottom: 0, fontSize: 13 }}>
            {c.chunk_text}
          </Paragraph>
        </Card>
      ))}
    </div>
  );
}
```

- [ ] **Step 4: 跑测试，验证 PASS**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend && npm test -- DocumentQaCitationList.test.tsx`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/components/documents/DocumentQaCitationList.tsx \
        frontend/src/tests/DocumentQaCitationList.test.tsx
git commit -m "feat(frontend): DocumentQaCitationList renders [n] cards

每卡片含 [id] 编号 + document_name + score% + chunk_text 4 行展开；
data-citation-id 属性供 MessageList 滚动定位用。"
```

---

## Task 9: DocumentQaMessageList + [n] 引用解析

**Files:**
- Create: `frontend/src/components/documents/DocumentQaMessageList.tsx`
- Test: `frontend/src/tests/DocumentQaMessageList.test.tsx`

**Interfaces:**
- Consumes: `Message[]` 既有 chat 类型（带 content/role/citations）+ `DocQaCitation[]`（Task 7）
- Produces: `<DocumentQaMessageList messages={...} loading={...} />` — assistant 文本中的 `[n]` 解析为 `<sup className="doc-qa-citation-ref" data-cite={n}>`，点击滚动到下方对应引用卡片

- [ ] **Step 1: 写失败测试 — `[n]` 解析 + 引用卡片渲染**

新建 `frontend/src/tests/DocumentQaMessageList.test.tsx`：
```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DocumentQaMessageList } from "../components/documents/DocumentQaMessageList";

describe("DocumentQaMessageList citation parsing", () => {
  it("parses [n] markers into clickable citation refs", () => {
    const messages = [
      {
        role: "user",
        content: "什么是质量协议？",
        citations: null,
      },
      {
        role: "assistant",
        content: "根据合同条款[1]，质量协议[2]是...",
        citations: [
          { id: 1, document_id: "DOC-A", document_name: "合同", chunk_text: "...", score: 0.85 },
          { id: 2, document_id: "DOC-B", document_name: "指南", chunk_text: "...", score: 0.62 },
        ],
      },
    ];
    render(<DocumentQaMessageList messages={messages} loading={false} />);
    const refs = screen.getAllByTestId("doc-qa-citation-ref");
    expect(refs.length).toBe(2);
    expect(refs[0].getAttribute("data-cite")).toBe("1");
    expect(refs[1].getAttribute("data-cite")).toBe("2");
    // 引用卡片也渲染
    expect(screen.getByText("合同")).toBeTruthy();
    expect(screen.getByText("指南")).toBeTruthy();
  });

  it("non-citation text rendered normally", () => {
    const messages = [
      { role: "assistant", content: "普通文本", citations: [] },
    ];
    render(<DocumentQaMessageList messages={messages} loading={false} />);
    expect(screen.getByText("普通文本")).toBeTruthy();
  });
});
```

- [ ] **Step 2: 跑测试，验证 FAIL**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend && npm test -- DocumentQaMessageList.test.tsx`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 写组件**

新建 `frontend/src/components/documents/DocumentQaMessageList.tsx`：
```tsx
import { useMemo } from "react";
import { List, Typography } from "antd";
import { DocumentQaCitationList } from "./DocumentQaCitationList";

const { Paragraph } = Typography;

interface Citation { id: number; [k: string]: unknown }
interface Message {
  role: "user" | "assistant";
  content: string;
  citations?: Citation[] | null;
}

interface Props {
  messages: Message[];
  loading: boolean;
}

/** 把 assistant content 中的 [n] 拆成 segments，返回 React 节点。 */
function renderWithCitations(text: string): React.ReactNode[] {
  const parts = text.split(/(\[\d+\])/g);
  return parts.map((part, idx) => {
    const match = part.match(/^\[(\d+)\]$/);
    if (match) {
      const n = match[1];
      return (
        <sup
          key={idx}
          className="doc-qa-citation-ref"
          data-cite={n}
          data-testid="doc-qa-citation-ref"
          onClick={() => {
            const target = document.querySelector(`[data-citation-id="${n}"]`);
            target?.scrollIntoView({ behavior: "smooth", block: "center" });
          }}
          style={{ cursor: "pointer", color: "#00D9C0" }}
        >
          [{n}]
        </sup>
      );
    }
    return <span key={idx}>{part}</span>;
  });
}

export function DocumentQaMessageList({ messages, loading }: Props) {
  return (
    <List
      dataSource={messages}
      loading={loading}
      renderItem={(msg, idx) => (
        <List.Item key={idx} style={{ display: "block" }}>
          <Paragraph>
            <strong>{msg.role === "user" ? "我：" : "助手："}</strong>{" "}
            {msg.role === "assistant"
              ? renderWithCitations(msg.content)
              : msg.content}
          </Paragraph>
          {msg.role === "assistant" && msg.citations && msg.citations.length > 0 && (
            <DocumentQaCitationList citations={msg.citations as any} />
          )}
        </List.Item>
      )}
    />
  );
}
```

- [ ] **Step 4: 跑测试，验证 PASS**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend && npm test -- DocumentQaMessageList.test.tsx`
Expected: 2 PASS

- [ ] **Step 5: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/components/documents/DocumentQaMessageList.tsx \
        frontend/src/tests/DocumentQaMessageList.test.tsx
git commit -m "feat(frontend): DocumentQaMessageList with [n] citation parser

assistant 文本的 [n] 拆为可点击 sup，点击定位下方引用卡片；
普通文本不变；citations 非空时下方渲染 DocumentQaCitationList。"
```

---

## Task 10: DocumentQaInput + DocumentQaHistoryPanel

**Files:**
- Create: `frontend/src/components/documents/DocumentQaInput.tsx`
- Create: `frontend/src/components/documents/DocumentQaHistoryPanel.tsx`
- Test: `frontend/src/tests/DocumentQaInput.test.tsx`
- Test: `frontend/src/tests/DocumentQaHistoryPanel.test.tsx`

**Interfaces:**
- Consumes: chatStore send/loadSessions/onSelect/onNew（Task 7）
- Produces:
  - `<DocumentQaInput sessionId={...} loading={...} onSend={...} />` — 输入框 + documentType Select + securityLevel Select + 发送按钮
  - `<DocumentQaHistoryPanel sessions={...} currentSessionId={...} loading={...} onSelect={...} onNew={...} />` — 历史会话列表 + 新对话按钮

- [ ] **Step 1: 写失败测试 — DocumentQaInput 发送时携带 filters**

新建 `frontend/src/tests/DocumentQaInput.test.tsx`：
```tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DocumentQaInput } from "../components/documents/DocumentQaInput";

describe("DocumentQaInput", () => {
  it("calls onSend with question and selected filters", async () => {
    const onSend = vi.fn();
    render(
      <DocumentQaInput
        sessionId="sess-1"
        loading={false}
        onSend={onSend}
      />
    );
    fireEvent.change(screen.getByPlaceholderText(/输入问题/), { target: { value: "什么是质量协议？" } });
    fireEvent.click(screen.getByRole("button", { name: /发送/ }));
    await waitFor(() => expect(onSend).toHaveBeenCalledWith(
      "什么是质量协议？",
      expect.objectContaining({}),
    ));
  });
});
```

- [ ] **Step 2: 跑测试，验证 FAIL**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend && npm test -- DocumentQaInput.test.tsx`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 写 DocumentQaInput**

新建 `frontend/src/components/documents/DocumentQaInput.tsx`：
```tsx
import { useState } from "react";
import { Button, Input, Select, Space } from "antd";
import { SendOutlined } from "@ant-design/icons";

const { TextArea } = Input;

export interface DocQaFilters {
  securityLevel?: string;
  documentType?: string;
}

interface Props {
  sessionId: string;
  loading: boolean;
  onSend: (question: string, filters: DocQaFilters) => void;
}

export function DocumentQaInput({ loading, onSend }: Props) {
  const [question, setQuestion] = useState("");
  const [securityLevel, setSecurityLevel] = useState<string | undefined>();
  const [documentType, setDocumentType] = useState<string | undefined>();

  const handleSend = () => {
    if (!question.trim()) return;
    onSend(question, { securityLevel, documentType });
    setQuestion("");
  };

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Space>
        <Select
          placeholder="文档类型（可选）"
          allowClear
          style={{ width: 200 }}
          value={documentType}
          onChange={setDocumentType}
          options={[
            { value: "CONTRACT", label: "合同" },
            { value: "POLICY", label: "制度" },
            { value: "GUIDE", label: "指南" },
            { value: "OTHER", label: "其他" },
          ]}
        />
        <Select
          placeholder="安全级别（可选）"
          allowClear
          style={{ width: 160 }}
          value={securityLevel}
          onChange={setSecurityLevel}
          options={[
            { value: "L1", label: "L1 公开" },
            { value: "L2", label: "L2 内部" },
            { value: "L3", label: "L3 机密" },
          ]}
        />
      </Space>
      <TextArea
        placeholder="输入问题，从已上传文档中检索并合成答案..."
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        rows={3}
        onPressEnter={(e) => {
          if (!e.shiftKey) {
            e.preventDefault();
            handleSend();
          }
        }}
      />
      <Button
        type="primary"
        icon={<SendOutlined />}
        onClick={handleSend}
        loading={loading}
      >
        发送
      </Button>
    </Space>
  );
}
```

- [ ] **Step 4: 写 DocumentQaHistoryPanel + 测试**

新建 `frontend/src/components/documents/DocumentQaHistoryPanel.tsx`：
```tsx
import { Button, List } from "antd";
import { PlusOutlined } from "@ant-design/icons";

interface SessionSummary {
  sessionId: string;
  title?: string;
  updatedAt?: string;
}

interface Props {
  sessions: SessionSummary[];
  currentSessionId: string | null;
  loading: boolean;
  onSelect: (sessionId: string) => void;
  onNew: () => void;
}

export function DocumentQaHistoryPanel({ sessions, currentSessionId, loading, onSelect, onNew }: Props) {
  return (
    <div className="doc-qa-history-panel">
      <Button type="primary" icon={<PlusOutlined />} onClick={onNew} block>
        新对话
      </Button>
      <List
        loading={loading}
        dataSource={sessions}
        renderItem={(s) => (
          <List.Item
            onClick={() => onSelect(s.sessionId)}
            style={{
              cursor: "pointer",
              background: s.sessionId === currentSessionId ? "#e6f7ff" : undefined,
              padding: "8px 12px",
            }}
          >
            {s.title || s.sessionId.slice(0, 16)}
          </List.Item>
        )}
      />
    </div>
  );
}
```

- [ ] **Step 5: 跑测试，验证 PASS**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend && npm test -- DocumentQaInput.test.tsx DocumentQaHistoryPanel.test.tsx`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/components/documents/DocumentQaInput.tsx \
        frontend/src/components/documents/DocumentQaHistoryPanel.tsx \
        frontend/src/tests/DocumentQaInput.test.tsx \
        frontend/src/tests/DocumentQaHistoryPanel.test.tsx
git commit -m "feat(frontend): DocumentQaInput + DocumentQaHistoryPanel

Input 含 documentType + securityLevel 两个 Select 过滤；
HistoryPanel 列出历史会话 + 新对话按钮；选中态背景高亮。"
```

---

## Task 11: DocumentQaPanel + DocumentsPage Tab 4 接入

**Files:**
- Create: `frontend/src/components/documents/DocumentQaPanel.tsx`
- Modify: `frontend/src/pages/DocumentsPage.tsx`
- Test: `frontend/src/tests/DocumentQaPanel.test.tsx`

**Interfaces:**
- Consumes: 既有 chatStore / 子组件
- Produces: `<DocumentQaPanel />` 组合 — 内部用 `useChatStore` 锁 channel='doc_qa'；DocumentsPage 新 Tab 4 挂载

- [ ] **Step 1: 写失败测试 — DocumentQaPanel 集成**

新建 `frontend/src/tests/DocumentQaPanel.test.tsx`：
```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DocumentQaPanel } from "../components/documents/DocumentQaPanel";

describe("DocumentQaPanel integration", () => {
  it("renders message list, input, and history panel", () => {
    render(<DocumentQaPanel />);
    expect(screen.getByPlaceholderText(/输入问题/)).toBeTruthy();
    expect(screen.getByText(/新对话/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: 跑测试，验证 FAIL**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend && npm test -- DocumentQaPanel.test.tsx`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 写 DocumentQaPanel**

新建 `frontend/src/components/documents/DocumentQaPanel.tsx`：
```tsx
import { useEffect } from "react";
import { useChatStore } from "../../stores/chatStore";
import { DocumentQaMessageList } from "./DocumentQaMessageList";
import { DocumentQaInput, type DocQaFilters } from "./DocumentQaInput";
import { DocumentQaHistoryPanel } from "./DocumentQaHistoryPanel";

export function DocumentQaPanel() {
  const setChannel = useChatStore((s) => s.setChannel);
  const messages = useChatStore((s) => s.messages);
  const loading = useChatStore((s) => s.loading);
  const sessions = useChatStore((s) => s.sessions);
  const sessionsLoading = useChatStore((s) => s.sessionsLoading);
  const currentSessionId = useChatStore((s) => s.sessionId);
  const sendDocQa = useChatStore((s) => s.sendDocQa);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const loadSessionMessages = useChatStore((s) => s.loadSessionMessages);
  const resetSession = useChatStore((s) => s.resetSession);

  useEffect(() => {
    setChannel("doc_qa");
    void loadSessions("doc_qa");
  }, [setChannel, loadSessions]);

  return (
    <div style={{ display: "flex", flexDirection: "row", gap: 16 }}>
      <DocumentQaHistoryPanel
        sessions={sessions}
        currentSessionId={currentSessionId}
        loading={sessionsLoading}
        onSelect={(sid) => void loadSessionMessages(sid)}
        onNew={resetSession}
      />
      <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
        <DocumentQaMessageList messages={messages} loading={loading} />
        <DocumentQaInput
          sessionId={currentSessionId ?? ""}
          loading={loading}
          onSend={(q, f: DocQaFilters) => void sendDocQa(q, f)}
        />
      </div>
    </div>
  );
}
```

- [ ] **Step 4: DocumentsPage 加 Tab 4**

修改 `frontend/src/pages/DocumentsPage.tsx`，在 `<Tabs>` 内追加：
```tsx
<TabPane tab="知识问答" key="qa">
  <DocumentQaPanel />
</TabPane>
```

并在 imports 追加 `import { DocumentQaPanel } from "../components/documents/DocumentQaPanel";`

- [ ] **Step 5: 跑测试，验证 PASS**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend && npm test -- DocumentQaPanel.test.tsx`
Expected: PASS

- [ ] **Step 6: 跑全量前端 vitest，确认无回归**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/frontend && npm test`
Expected: 既有测试全过 + 新增 5 个 doc_qa 测试全过

- [ ] **Step 7: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/components/documents/DocumentQaPanel.tsx \
        frontend/src/pages/DocumentsPage.tsx \
        frontend/src/tests/DocumentQaPanel.test.tsx
git commit -m "feat(frontend): DocumentQaPanel + DocumentsPage Tab 4

DocumentQaPanel 组合 MessageList + Input + HistoryPanel，
挂载时锁 channel='doc_qa' 拉历史；
DocumentsPage 第 4 个 Tab「知识问答」接入。"
```

---

## Task 12: 端到端 smoke + 提交 spec 联动更新

**Files:**
- Modify: `backend/app/tests/integration/test_doc_qa_api.py`（追加全链路 smoke 用例）
- Modify: `docs/superpowers/specs/2026-09-08-documents-knowledge-qa-design.md`（追加"实施落地"小节，链接到 plan + commits）

**Interfaces:**
- Consumes: 既有 PG fixture / auth fixtures
- Produces: 完整 E2E 用例 — 上传文档 → 提问 → 收到 SSE → 数据库有 user + assistant 行 → 历史列表含该 session

- [ ] **Step 1: 写失败测试 — E2E smoke**

修改 `backend/app/tests/integration/test_doc_qa_api.py`，追加：
```python
@pytest.mark.asyncio
async def test_doc_qa_e2e_full_flow(db_session, seed_doc, auth_headers) -> None:
    """完整流程：上传文档 → 提问 → SSE 收到 events → DB 落库 → 历史可见。"""
    # 1. 上传文档（用既有 /upload 端点）
    ...

    # 2. 调用 /qa 流式
    events = []
    async with AsyncClient(...) as client:
        async with client.stream("POST", "/api/v1/documents/qa", json={...}, headers=auth_headers("u-e2e")) as resp:
            async for line in resp.aiter_lines():
                # 解析 SSE
                events.append(parse_sse_line(line))

    # 3. 断言事件序列
    assert any(e["type"] == "qa_meta" for e in events)
    assert any(e["type"] == "qa_citations" for e in events)
    assert any(e["type"] == "qa_done" for e in events)

    # 4. DB 中有 2 行 session_message
    rows = await db_session.execute(
        select(SessionMessage).where(
            SessionMessage.session_id == "e2e-sess",
            SessionMessage.channel == "doc_qa",
            SessionMessage.user_id == "u-e2e",
        )
    )
    assert len(rows.scalars().all()) == 2

    # 5. 历史 API 可见
    resp = await client.get("/api/v1/sessions/chat-history?channel=doc_qa", headers=auth_headers("u-e2e"))
    assert any(s["sessionId"] == "e2e-sess" for s in resp.json())
```

- [ ] **Step 2: 跑测试，验证 FAIL（端到端未跑通）**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest app/tests/integration/test_doc_qa_api.py -v`
Expected: 至少 1 个 FAIL（端到端链路）

- [ ] **Step 3: 实施 debug（按需迭代）**

实施过程中遇到具体问题（如 fixtures 缺失、auth setup、DB 提交时序）逐个修；每修一处跑测试直到全过。

- [ ] **Step 4: 跑全量后端 pytest，确认无回归**

Run: `cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend && .venv/bin/pytest -v`
Expected: 既有测试全过 + 新增 doc_qa 测试全过（覆盖率 ≥ 80%）

- [ ] **Step 5: 更新 spec 文档落地链接**

修改 `docs/superpowers/specs/2026-09-08-documents-knowledge-qa-design.md`，在 § 11 之前追加：

```markdown
## 12. 实施落地

- 实施计划：`docs/superpowers/plans/2026-09-08-documents-knowledge-qa.md`
- Commits（按任务顺序）：
  - Alembic 0047：`feat(db): session_message + channel/citations/user_id`
  - DTO + 事件 + Prompt：`feat(rag-qa): DTO + SSE events + prompt module`
  - RagQaService skeleton：`feat(rag-qa): RagQaService skeleton + history + no-chunks short circuit`
  - RagQaService LLM 流式：`feat(rag-qa): LLM streaming + persist in RagQaService`
  - API endpoint：`feat(api): POST /api/v1/documents/qa SSE endpoint`
  - 历史 channel 过滤：`feat(api): chat-history endpoint accepts channel filter`
  - 前端三件套：`feat(frontend): DocQa types + SSE client + chatStore channel`
  - CitationList：`feat(frontend): DocumentQaCitationList renders [n] cards`
  - MessageList + 解析：`feat(frontend): DocumentQaMessageList with [n] citation parser`
  - Input + History：`feat(frontend): DocumentQaInput + DocumentQaHistoryPanel`
  - Panel + Tab 4：`feat(frontend): DocumentQaPanel + DocumentsPage Tab 4`
- 落地日期：2026-09-XX（实施时填）
```

- [ ] **Step 6: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/tests/integration/test_doc_qa_api.py \
        docs/superpowers/specs/2026-09-08-documents-knowledge-qa-design.md
git commit -m "test(doc-qa): E2E smoke + spec link to plan

端到端：上传 → 提问 → SSE → DB 落库 → 历史可见；
spec 追加「实施落地」小节链接 plan + 任务 commits。"
```

---

## 自审结果

**1. Spec 覆盖检查：**
- §1 目标 → Task 11（Tab 4 接入）
- §2 决策表（10 项）→ Task 1-7 全部覆盖
- §3 架构 → Task 3-5 服务/API
- §4 数据模型 → Task 1 Alembic 0047
- §5 Prompt → Task 2 rag_qa_prompt
- §6 后端流程 → Task 3-5
- §7 前端流程 → Task 7-11
- §8 错误处理 → Task 4（无 chunks）+ Task 5（auth/ownership）+ Task 5（RagError 转 SSE）
- §9 实施风险 → Task 7 验证 `completeStream` 存在；Task 1-4 解决 user_id；Task 7-11 chatStore 泛化
- §10 测试 → Task 1（DB 列）+ Task 2（prompt）+ Task 4（service）+ Task 5（API）+ Task 6（history）+ Task 7-11（前端）+ Task 12（E2E）

**2. Placeholder scan：** 无 TBD/TODO/"implement later"

**3. 类型一致性：** `DocQaRequest`（Task 2）↔ `searchDocumentsQa(payload: DocQaRequestPayload)`（Task 7）↔ `DocumentQaInput` props（Task 10）三方字段对齐（sessionId/question/topK/securityLevel/documentType/modelId camelCase）

---

## 实施后清单

- [ ] 全量后端测试 PASS
- [ ] 全量前端测试 PASS
- [ ] Docker 镜像重建（backend + frontend）
- [ ] 浏览器手动验证：登录 → DocumentsPage → Tab 4 → 输入问题 → 收到 SSE 流式回答 + 引用 [n] + 卡片
- [ ] 跨渠道隔离验证：chat 的 session 不出现在 doc_qa 历史
- [ ] ownership 守卫验证：用 userA 的 sessionId 给 userB 提交 → 422
- [ ] 模型路由验证：不传 modelId 时，selectModel(purpose='doc_qa') 被调（看日志）
- [ ] Token 计量验证：token_usage 表新增 purpose='doc_qa' 行
- [ ] 提交 PR 到主分支