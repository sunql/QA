# DocumentsPage 知识问答 — 设计文档

- 日期：2026-09-08
- 分支：feat/business-object-registry
- 状态：已获用户批准的设计（"就这样" 2026-09-08）

## 1. 目标

在 `DocumentsPage`（文档中心）新增第 4 个 Tab **「知识问答」**，把已上传文档（Milvus `document_embeddings` + PostgreSQL `document_catalog`）从「只检索」升级为「检索 + LLM 合成答案 + 引用回标」。用户可对已上传文档进行多轮自然语言问答，系统从文档库中检索相关内容片段（top-K chunks），由 LLM 基于引用片段生成带 `[n]` 编号的回答，每条引用都可下钻到原文档。

不在本期范围：跨文档多跳推理、自动改写查询、可学习的引用排序。

## 2. 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 形态 | DocumentsPage Tab 4「知识问答」（多轮 + 历史侧栏） |
| 持久化 | 复用 `session_message` 表 |
| 会话隔离 | `session_message` 加 `channel` 列（`chat` / `doc_qa`） |
| 引用存储 | `session_message` 加 `citations` JSONB 列 |
| 检索范围 | 默认全量 + 可选 `security_level` / `document_type` 过滤 |
| 响应方式 | SSE 流式 |
| 模型选择 | 复用 chat 的 `ModelRouterService`（`purpose="doc_qa"`） |
| 多轮上下文 | 每轮重检索 + 上 5 轮对话为 LLM 上下文（不含 citations JSON） |
| 引用呈现 | 回答正文 `[1]/[2]` 编号 + 下方引用卡片列表 |
| 限流 | 复用 chat 的 `@limiter.limit(rateLimitValue)` |

## 3. 架构与数据流

```
用户（DocumentsPage Tab 4）
  │
  │ ① 输入问题 + 可选 security_level / document_type 过滤
  │ ②（多轮）选历史会话 / 新对话
  ▼
DocumentQaPanel（前端）
  │
  │ ③ POST /api/v1/documents/qa（SSE）
  ▼
RagQaService.answer_stream（后端编排）
  │  ├─ 加载上 5 轮 doc_qa 会话消息（纯文本，无 citations JSON）
  │  ├─ RagService.searchDocuments(question, topK=8, security_level, session)
  │  │    └─ Milvus searchDocumentChunks → JOIN document_catalog → document_name 回填
  │  ├─ 若 top1 score < 0.3 → 走固定模板："未在已上传文档中找到相关依据"
  │  │    └─ SSE: meta → citations=[] → token(template) → done（零 LLM 调用）
  │  ├─ ModelRouterService.selectModel(purpose="doc_qa") → 选 cfg
  │  ├─ llm_factory(cfg).complete_stream(
  │  │      messages=[system=DOC_QA_SYSTEM_PROMPT+chunks_json,
  │  │               history_block,
  │  │               user=current_question],
  │  │      model=cfg.model_name,
  │  │    )
  │  └─ SSE 事件序列：
  │       - EVENT_QA_META {intent:"doc_qa"}
  │       - EVENT_QA_CITATIONS {citations:[chunk dicts]}
  │       - EVENT_TOKEN × N
  │       - EVENT_QA_DONE {tokensUsed, cost, modelName}
  │
  │ ④ 流结束 → 落库 session_message
  │    ├─ user 行（content=question, channel="doc_qa"）
  │    └─ assistant 行（content=answer, citations=chunks_json, channel="doc_qa"）
  │
  ▼
DocumentQaMessageList（前端渲染）
  └─ 解析 assistant 文本中的 [n] 标记 → 高亮 + 点击定位下方引用卡片
DocumentQaHistoryPanel（前端）
  └─ GET /api/v1/sessions/chat-history?channel=doc_qa → 列表 + 加载历史
```

### 新增 / 修改组件

| 组件 | 位置 | 职责 |
|---|---|---|
| `RagQaService` | `backend/app/services/rag_qa_service.py`（新） | 一轮 doc_qa 编排：检索 → 拼 prompt → 流式 LLM → 落库；<300 行 |
| `DocQaRequest` / `DocQaPrompt` | `backend/app/domain/schemas.py` / `backend/app/services/rag_qa_prompt.py`（新） | DTO + 纯模块 prompt 模板 |
| `documents.py` 新端点 | `backend/app/api/v1/documents.py` | `POST /api/v1/documents/qa`（SSE） |
| `stream_events.py` 加事件 | `backend/app/services/stream_events.py` | `EVENT_QA_META` / `EVENT_QA_CITATIONS` / `EVENT_QA_DONE` |
| `SessionMessage` ORM | `backend/app/domain/models.py`（改） | 加 `channel` + `citations` 两列 |
| Alembic 0047 | `backend/alembic/versions/0047_*.py`（新） | 加列 + 索引 |
| `session_history_service.listChatSessions` | `backend/app/services/session_history_service.py`（改） | 加 `channel` 参数 + WHERE 过滤 |
| `session_messages` API（若新建）/ `messages.py` | `backend/app/api/v1/session.py`（改） | 加载历史消息时按 channel 过滤 |
| `DocumentQaPanel` | `frontend/src/components/documents/DocumentQaPanel.tsx`（新） | Tab 4 主面板，组合 history + message list + input |
| `DocumentQaMessageList` | `frontend/src/components/documents/DocumentQaMessageList.tsx`（新） | 渲染消息列表 + 解析 `[n]` 引用 |
| `DocumentQaCitationList` | `frontend/src/components/documents/DocumentQaCitationList.tsx`（新） | 引用卡片列表（document_name + score + chunk_text） |
| `DocumentQaHistoryPanel` | `frontend/src/components/documents/DocumentQaHistoryPanel.tsx`（新） | 历史会话侧栏 |
| `DocumentQaInput` | `frontend/src/components/documents/DocumentQaInput.tsx`（新） | 输入框 + document scope filter |
| `chatStore` | `frontend/src/stores/chatStore.ts`（改） | 加 `channel: "chat" \| "doc_qa"` 字段 |
| `DocumentsPage` | `frontend/src/pages/DocumentsPage.tsx`（改） | 加 Tab 4 |
| `document.ts` types | `frontend/src/types/document.ts`（改） | 加 DocQa 事件 / 消息 / 引用类型 |

## 4. 数据模型（Alembic 0047，单次迁移）

### session_message 新增 3 列（全部 nullable / 带 default，存量零破坏）

- `channel VARCHAR(16) NOT NULL DEFAULT 'chat'` — 取值：`chat` / `doc_qa`
- `citations JSONB NULL` — 仅 assistant 行填写，doc_qa 轮填引用 chunk 列表，chat 轮恒 NULL
- `user_id VARCHAR(64) NULL` — 落库时由 API 层透传当前用户 userId；chat 存量行回填 NULL（ownership 守卫见 §6.4）；doc_qa 轮必填（用户隔离守卫）

### 新增索引

- `idx_session_msg_channel_time` ON `session_message(channel, session_id, created_time)` — 支撑 `channel='doc_qa'` 历史会话按时间拉取
- `idx_session_msg_user_channel_time` ON `session_message(user_id, channel, created_time)` — 支撑按用户的历史会话聚合

### ORM 变更

```python
class SessionMessage(Base, TimestampMixin):
    # ... 既有字段 ...
    channel: Mapped[str] = mapped_column(String(16), default="chat", nullable=False)
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True,
    )
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (
        Index("idx_session_msg_time", "session_id", "created_time"),
        Index("idx_session_msg_channel_time", "channel", "session_id", "created_time"),
        Index("idx_session_msg_user_channel_time", "user_id", "channel", "created_time"),
    )
```

## 5. Prompt 设计（`rag_qa_prompt.py`，纯模块）

```python
DOC_QA_SYSTEM_PROMPT = """你是文档问答助手。基于以下引用的文档内容回答用户问题。

规则：
1. 仅基于提供的文档内容回答，不要编造。
2. 引用处用 [1]、[2] 等编号标注，对应下方引用列表。
3. 文档无相关信息时，明确说明「未在已上传文档中找到相关依据」。
4. 用中文回答，简洁准确。

引用文档：
{chunks_json}"""

DOC_QA_USER_TEMPLATE = "{history_block}\n\n当前问题：{question}"
```

历史块拼接：上 5 轮 `role: content` 纯文本（不含 citations JSON，避免 prompt 膨胀）。

`chunks_json` 形如：
```json
[
  {"id": 1, "document_name": "供应商合同 V2.0", "chunk_text": "..."},
  {"id": 2, "document_name": "49.仓储数字化和智能化管理能力.docx", "chunk_text": "..."}
]
```

LLM 按 chunks_json 中元素的 `id` 字段在回答中插 `[1]` `[2]`。

## 6. 后端流程

### 6.1 DocQaRequest / DocQaResponse

```python
class DocQaRequest(CamelModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    question: str = Field(..., min_length=1)
    top_k: int = Field(default=8, ge=1, le=20)
    security_level: str | None = None
    document_type: str | None = None
    model_id: int | None = None  # 用户明确指定模型时跳过 router
```

SSE 响应事件流（`text/event-stream`）：
```
event: qa_meta
data: {"intent":"doc_qa"}

event: qa_citations
data: {"citations":[{"id":1,"document_id":"DOC-...","document_name":"...","chunk_text":"...","score":0.85},...]}

event: token
data: {"content":"根据合同"}

event: token
data: {"content":"条款"}

event: qa_done
data: {"tokensUsed":1234,"cost":0.012,"modelName":"claude-sonnet"}
```

### 6.2 RagQaService.answer_stream

```python
async def answer_stream(
    self, session: AsyncSession, dto: DocQaRequest,
    *, actor: CurrentUser, configs: list[LlmConfig],
) -> AsyncIterator[StreamEvent]:
    """一轮问答：检索 → 拼 prompt → 流式 LLM → 落库。

    输入：用户问题 + 上一轮历史（已加载）。
    输出：SSE 事件序列（meta / citations / token×N / done）。
    副作用：流结束后落库 session_message(user, assistant)；记录 token_usage。
    """
    # 1. 加载上 5 轮 doc_qa 消息（plain text，无 citations JSON）
    history_block = await self._load_history_block(session, dto.session_id)

    # 2. 检索
    chunks = await self._rag_svc.searchDocuments(
        dto.question, top_k=dto.top_k,
        security_level=dto.security_level, session=session,
    )
    citations = [
        {"id": idx + 1, **c} for idx, c in enumerate(chunks)
    ]  # id 用于 LLM 引用编号

    yield StreamEvent(EVENT_QA_META, {"intent": "doc_qa"})
    yield StreamEvent(EVENT_QA_CITATIONS, {"citations": citations})

    # 3. 无相关 chunks → 走固定模板，不调 LLM
    if not chunks or chunks[0]["score"] < 0.3:
        answer = "未在已上传文档中找到相关依据。"
        yield StreamEvent(EVENT_TOKEN, {"content": answer})
        yield StreamEvent(EVENT_QA_DONE, {"tokensUsed": 0, "cost": 0.0, "modelName": None})
        await self._persist(session, dto, answer, citations)
        return

    # 4. 选模型
    selected = await self._select_model(session, dto, configs)
    client = self._llm_factory(selected)

    # 5. 拼 prompt
    messages = [
        LlmMessage(role="system", content=DOC_QA_SYSTEM_PROMPT.format(chunks_json=...)),
        LlmMessage(role="user", content=DOC_QA_USER_TEMPLATE.format(history_block=history_block, question=dto.question)),
    ]

    # 6. 流式 LLM
    full_answer = ""
    async for delta in client.complete_stream(messages, model=selected.model_name):
        full_answer += delta
        yield StreamEvent(EVENT_TOKEN, {"content": delta})

    # 7. 记录 token + cost
    tokens_used, cost = await self._record_usage(session, dto, selected, full_answer)

    # 8. 落库
    await self._persist(session, dto, full_answer, citations)

    yield StreamEvent(EVENT_QA_DONE, {"tokensUsed": tokens_used, "cost": cost, "modelName": selected.model_name})
```

### 6.3 复用组件

- `BaseLlmClient.complete_stream(messages, model)` — 若未实现流式，本期需扩展（详见 § 9 实施风险）
- `ModelRouterService.selectModel(purpose="doc_qa")` — 加 `purpose` 参数区分降级策略（doc_qa 触发更激进的降级到最便宜模型，因仅需摘要）
- `TokenUsageService.recordUsage(purpose="doc_qa")` — 独立聚合
- `stream_events.py` — 加 3 个新事件常量，复用现有 `toSse()`

### 6.4 session_id 守卫

doc_qa 流式前查 `session_message` 中 `channel='doc_qa' AND session_id=? AND user_id=?`：命中才允许继续；不命中 → 422（沿用 chat 的 `MSG_SESSION_NOT_OWNED` 风格文案）。

落库时同样写入 `user_id`（来自 `_user.userId`）保证 ownership 一致性。

**作用域：** 此守卫仅适用于 doc_qa channel；chat channel 维持现有 ownership 模式（chat 历史按创建时间聚合，跨用户可见不在本期范围——chat 当前无强制 ownership 守卫）。Alembic 0047 同时给 chat 行写入 user_id（API 层透传 `_user.userId`）以便未来扩展，但本期 chat 不做强制守卫。

## 7. 前端流程

### 7.1 DocumentsPage 改造

```tsx
<Tabs activeKey={activeTab} onChange={setActiveTab}>
  <TabPane tab="文档目录" key="documents">...</TabPane>
  <TabPane tab="文档关联" key="relations">...</TabPane>
  <TabPane tab="语义检索" key="search">...</TabPane>
  <TabPane tab="知识问答" key="qa">
    <DocumentQaPanel />
  </TabPane>
</Tabs>
```

### 7.2 DocumentQaPanel

```tsx
function DocumentQaPanel() {
  const channel = "doc_qa";  // 锁死，不复用 chat 的 datasource
  const messages = useChatStore((s) => s.messages);
  const sessionId = useChatStore((s) => s.sessionId);
  const loading = useChatStore((s) => s.loading);
  const send = useChatStore((s) => s.sendMessage);
  const resetSession = useChatStore((s) => s.resetSession);

  return (
    <div className="doc-qa-layout">
      <DocumentQaMessageList messages={messages} loading={loading} />
      <DocumentQaInput
        sessionId={sessionId}
        onSend={(question, filters) => sendDocQa(question, filters, sessionId)}
        loading={loading}
      />
      <DocumentQaHistoryPanel
        channel={channel}
        currentSessionId={sessionId}
        onSelect={loadDocQaSession}
        onNew={resetSession}
      />
    </div>
  );
}
```

### 7.3 引用渲染

assistant 文本先用正则 `\[(\d+)\]` 拆成 segments：
- 普通 segment → 直接渲染
- `[n]` 匹配 → 渲染 `<sup className="doc-qa-citation" data-cite={n}>[{n}]</sup>`，点击滚动定位下方对应引用卡片

`DocumentQaCitationList`：每条 assistant 消息下方渲染 chunk 卡片（与现有 `RagSearchPanel` 卡片同构：`document_name` + `score%` + `chunk_text` 摘要 + `expandable`）。

### 7.4 chatStore 改造

加 `channel: "chat" | "doc_qa"` 字段：
- `resetSession()` 根据当前 channel 决定 sessionId 前缀（`doc_qa_xxx` 或 `chat_xxx`），避免两渠道 sessionId 冲突
- `sendMessage()` 根据 channel 决定走 chat 流式或 doc_qa 流式端点
- `loadSessions(channel)` 拉历史时按 channel 过滤

历史会话 API：`GET /api/v1/sessions/chat-history?channel=doc_qa`，复用现有端点加 query 参数。

历史消息加载：`GET /api/v1/sessions/{sessionId}/messages?channel=doc_qa`，复用现有端点加 query 参数。

## 8. 错误处理

| 场景 | 行为 |
|---|---|
| Milvus 检索失败 | 流式首事件 `EVENT_QA_CITATIONS` 推空数组 + 注入 prompt 提示；LLM 答"暂无法访问文档库" |
| 无相关 chunks（top1 score < 0.3） | 走固定模板"SSE 一条 token + done"，不调 LLM（零消耗） |
| LLM 流式中断（`LlmClientError`） | 持久化 user + 错误 assistant（`content="[回答生成失败]"`），发出 `EVENT_ERROR` 事件 |
| 流式超时（>60s，`asyncio.wait_for` 兜底） | 关闭连接，已落库的 user + 部分 assistant 保留 |
| sessionId 不属于当前用户 | 422（沿用 chat 的 `_ensureOwnedSession` 守卫） |
| 用户模型选择 `modelId` 不存在 | 422 NotFoundError（沿用 chat 的 `MSG_MODEL_CONFIG_UNAVAILABLE`） |
| 检索返回空 + 用户硬传 modelId | 仍走固定模板，modelId 仅在需要 LLM 时才生效 |

## 9. 实施风险与依赖

| 风险 | 应对 |
|---|---|
| `BaseLlmClient.complete_stream` 未实现 | 实施时先核查；缺失则需扩展 base_client 接口加流式 API（参考 OpenAI `stream=True`） |
| `session_message` 无 `user_id` 列，无法做 session ownership 守卫 | 已纳入 Alembic 0047 同次迁移（见 §4），加 `user_id VARCHAR(64) NULL` + 复合索引；存量 chat 消息 user_id 保持 NULL（新行写入时填），doc_qa 必填 |
| `ChatRequest.datasource_id` 强契约被破坏 | 走新端点 `/api/v1/documents/qa`，不混入 `/api/v1/chat`；`ChatRequest` 不动 |
| chatStore 复用是否合适 | 评估：chatStore 含 datasource 概念需泛化；本期做最小改动，doc_qa 路径里 datasource 传 None |
| sessionId 前缀是否必要 | 已采用 user_id 守卫后前缀主要起"避免 chat/doc_qa ID 碰撞"的辅助作用；保留前缀（`docqa-` vs `chat-`）作为额外隔离带，减少日志混淆 |
| 引用 [n] 标记 LLM 输出不可靠 | prompt 强约束 + 解析失败降级为「下方引用卡片列表无高亮」 |
| 大量文档时的 retrieval 性能 | Milvus 已支持向量索引；topK=8 + security_level 过滤在 catalog（PG，索引）足够快 |
| Milvus 与 PG JOIN 失败（catalog 删除的孤儿 chunks） | 已在 RAG hydration 中降级用 document_id 兜底（commit c30b529 后的现状） |

## 10. 测试

| 测试 | 断言 |
|---|---|
| `test_rag_qa_no_chunks_returns_template_answer` | top1 score < 0.3 → 直接返回固定模板，不调 LLM（mock llm_factory 不被调用） |
| `test_rag_qa_streams_meta_citations_tokens_done` | SSE 事件序列正确：1×meta + 1×citations + N×token + 1×done |
| `test_rag_qa_persists_user_and_assistant_with_citations` | 流结束后 DB 中有 2 行 session_message（user + assistant），assistant 的 `citations` 列 = chunks JSON，`channel='doc_qa'` |
| `test_rag_qa_history_injected_as_plain_text` | 第 2 轮 LLM 调用 messages 中含第 1 轮 user+assistant 纯文本（不含 citations JSON） |
| `test_rag_qa_uses_router_for_model_selection` | 不传 modelId 时 `selectModel(purpose="doc_qa")` 被调 |
| `test_rag_qa_filters_by_security_level` | dto.security_level="L2" → `searchDocuments(security_level="L2")` 透传 |
| `test_rag_qa_uses_user_model_id_when_provided` | dto.model_id=123 → router 跳过，直接用 id=123 的 LlmConfig |
| `test_rag_qa_session_ownership_guard_422` | sessionId 不属于当前用户 → 422 |
| `test_rag_qa_llm_failure_persists_error_assistant` | LLM 抛 `LlmClientError` → 流结束前 user + 错误 assistant 行已落库，`EVENT_ERROR` 发出 |
| `test_session_history_filters_by_channel` | `listChatSessions(channel="doc_qa")` 只返回 channel='doc_qa' 的会话 |
| `test_messages_load_filters_by_channel` | session 加载历史消息时 WHERE channel='doc_qa' |
| 前端 vitest：`test_DocumentQaPanel_renders_citation_markers` | assistant 文本 `[1][2]` 被解析为 `<sup data-cite>` 高亮 |
| 前端 vitest：`test_DocumentQaPanel_sends_doc_scope_filter` | UI 选 documentType="CONTRACT" → POST body 含 `documentType: "CONTRACT"` |
| 前端 vitest：`test_DocumentQaPanel_separates_chat_and_doc_qa_sessions` | chatStore channel 切换时 sessionId 前缀不同，ChatHistoryPanel 与 DocumentQaHistoryPanel 互不干扰 |

**红线：** 真实 PG + 完整 API 链路 + 真实数据库对象（禁止 sqlite 内存库 + 直接调 service）。

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
- 落地日期：2026-09-08

## 11. 不在本期范围

- 跨文档多跳推理（多步 RAG / Agent 编排）
- 自动改写用户查询 / HyDE / Multi-Query
- 用户反馈学习（点赞 / 点踩影响排序）
- ACL 文档级权限（security_level L3 当前全开，未来按部门 ACL 收敛）
- 文档级 token 用量明细（当前按 purpose="doc_qa" 聚合）
- 异步检索 / 后台索引（已是同步）
- 移动端 / 暗色主题适配（沿用现有主题）