# 变更：智能问答界面右侧问答历史记录面板

- **日期**：2026-08-16
- **作者**：AI 助手
- **Phase**：Phase 5（生产增强）
- **状态**：done

## 1. 需求

智能问答界面右侧增加问答历史记录列表：列表可点击或删除，点击某个历史问题可加载当时的完整 Q&A 到当前聊天区。

**用户决策**：
- 布局：可折叠右侧面板（默认折叠，浮起小竖条）；点击展开 280px，主区相应收窄。
- 点击行为：替换当前聊天视图（loadSessionMessages 覆盖 messages 数组）。
- 持久化：`currentSessionId` + `historyPanelOpen` 写 localStorage，刷新能恢复。

## 2. 设计评审

- **后端**：并入现有 `backend/app/api/v1/session.py`（74 行 + 3 endpoint）。新建独立 `session_history_service.py`（不污染 2206 行的 `chat_service.py`，符合"小文件"约束）。
- **路由顺序**：`/chat-history` 字面量段必须在 `/{sessionId}/...` 之前，避免被路径参数吞（FastAPI 路径匹配规则）。
- **SQL 聚合**：列表接口用一次聚合 + 两次批量取最后 user/assistant，避免 N+1。每 session 最后一条用 `MAX(id)` 决胜（id 主键唯一单调），不用 `MAX(created_time)`（同时间戳并列会返回多行或丢行）。
- **删除硬删除**：三表 `session_message + session_token_usage + session_query_state` 单事务 `DELETE`，按 `RETURNING(id)` 取真实行数（asyncpg `rowcount` 在某些版本上 DELETE 返回 -1 会误判 404）。不引入软删除字段（无合规/审计需求）。
- **前端 store**：zustand 扩展 4 个 state（sessions/sessionsLoading/sessionsError/historyPanelOpen）+ 5 个 action。不引入 `zustand/middleware/persist`，手写 `persistChatUiState.ts`（< 50 行）只持久化 2 个键。
- **持久化键名**：`qa:chat:lastSessionId`、`qa:chat:historyPanelOpen`。
- **历史回放限制**：chartOption/data 未持久化（图表对象 >1KB/行）；历史回放仅展示 content + sql + 时间戳。

## 3. 数据模型变更

**无 Alembic 迁移**。理由：
- 三表之间无 FK 约束（`SessionMessage.session_id`、`SessionTokenUsage.session_id`、`SessionQueryState.session_id` 均普通 `String(64)` 无 `ForeignKey`）。
- 删除由 service 层显式执行，schema 与 service 解耦更干净。
- 未来如需 tenant 隔离，在三表加 `tenant_id` 列 + 单独迁移（与本期解耦）。

## 4. 接口契约变更

新增 3 个端点（并入 `app/api/v1/session.py`）：

| 端点 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/sessions/chat-history?limit=&offset=` | 聊天语义会话列表（按 last_time DESC） |
| GET | `/api/v1/sessions/{sessionId}/messages?limit=&before_id=` | 单会话消息流（按 id ASC） |
| DELETE | `/api/v1/sessions/{sessionId}` | 硬删除（三表 + 单事务） |

新增 3 个 Pydantic schema（snake_case 字段 + `CamelModel` alias generator）：

- `ChatSessionListItem`：`session_id` / `first_time` / `last_time` / `message_count` / `last_question` / `last_answer_preview`。
- `ChatMessageRead`：`id` / `role` / `content` / `question` / `sql` / `created_time`。
- `SessionMessagesResponse`：`session_id` + `messages: list[ChatMessageRead]`。

`session_message` 表按 `id ASC` 即时间正序（id 序列与 `created_time` 单调一致）；分页用 `before_id` cursor 保持追加语义稳定。

## 5. 前端变更

**新增文件**：
- `frontend/src/types/chatHistory.ts`：`ChatSession` / `ChatMessageRead` / `SessionMessagesResponse`（避免污染 `types/chat.ts`）。
- `frontend/src/api/chatHistory.ts`：`listChatSessions` / `loadSessionMessages` / `deleteSessionHistory`（复用既有 `httpClient`）。
- `frontend/src/stores/persistChatUiState.ts`：localStorage read/write helper（手写 try/catch，SSR/隐私模式静默）。
- `frontend/src/components/chat/ChatHistoryPanel.tsx`：右侧面板组件（antd List + Popconfirm + dayjs fromNow）。
- `frontend/src/components/chat/HistoryToggleButton.tsx`：折叠态浮起按钮。

**修改文件**：
- `frontend/src/stores/chatStore.ts`：
  - `ChatState` 扩展 4 个字段 + 5 个 action；
  - 初始化时一次性 `read()` 读取持久化（hydration）；
  - `resetSession` / `loadSessionMessages` / `deleteSession`（删除当前会话时）/ `toggleHistoryPanel` / `setHistoryPanelOpen` 触发 `write()`；
  - 新增 `toChatMessage(read: ChatMessageRead): ChatMessage` 转换函数（chartOption/data 不持久化）。
- `frontend/src/pages/ChatPage.tsx`：外层 `flex-direction: row`，新增左侧 `leftMain`（flex: 1, min-width: 0）+ 右侧根据 `historyPanelOpen` 切换 `ChatHistoryPanel` 与 `HistoryToggleButton`。
- `frontend/src/i18n/zh-CN.ts` / `en-US.ts`：新增 `chat.history.*` 11 个键位（title/newChat/deleteConfirm/empty/loadError/deleteError/collapse/expand/messageCount/currentBadge/untitledQuestion）。

**store action 行为边界**：
- `loadSessions`：成功填充 `sessions`，失败写 `sessionsError`（不污染 `error`，避免影响 chat 区错误显示）。
- `loadSessionMessages(sessionId)`：替换 `messages` + 更新 `sessionId` + 持久化 `lastSessionId`；不影响 `datasourceId`/`selectedModelId`。
- `deleteSession(sessionId)`：不可变移除 sessions 中对应项；**若是当前会话**：清空 messages、重置 sessionId、持久化新 id。
- `toggleHistoryPanel` / `setHistoryPanelOpen`：翻转 state + 同步 localStorage。

## 6. 测试

**后端**（真实 PG + 完整 API 链路）：
- 新建 `backend/app/tests/integration/test_chat_history_api.py`：13 用例（5 列表 + 4 消息流 + 3 删除 + 1 鉴权）。
- 回归 `test_session_api.py`：5 用例全绿。

**前端**（vitest + jsdom）：
- `tests/chatStore.test.ts`：原 17 + 新 12 = 29 用例。
- 新建 `tests/ChatHistoryPanel.test.tsx`：8 用例（渲染列表/空状态/loading/error/点击/Popconfirm/选中态/新对话按钮）。
- `tests/ChatPage.test.tsx`：原 9 + 新 3 = 12 用例（默认折叠渲染、展开渲染、toggle 切换）。
- 全量：258 passed（27 个 test file，10.53s）。

**覆盖率**：vitest thresholds 80% lines/functions/branches/statements 通过。

### 6.1 code-reviewer 审查后的改进

| 级别 | 缺陷 | 处理 |
|---|---|---|
| HIGH-1 | service 层 commit | **未修**：所有 service 均在内部 commit（chat_service.py:2065 等），项目既定模式 |
| HIGH-2 | loadSessionMessages 静默吞错，UX 缺陷 | **已修**：失败写 `sessionsError`（与 chat 区 `error` 解耦），面板 Alert 可见；同步更新 chatStore 测试 |
| HIGH-3 | listChatSessions 107 行超限 | **已修**：提取 `_buildLastMessageContentMap` 私有 helper，消除 user/assistant 重复结构 |
| MEDIUM-1 | 截断无 None 防御 | **已修**：helper 内 `if r.content is not None` 过滤 |
| MEDIUM-2 | sendMessage 200 行 | **未修**：预存在代码，本期未触碰 sendMessage |
| LOW-1 | eslint-disable | 保留（注释合理） |

修复后回归：后端 13/13 + 前端 258/258 通过，TS 干净。

## 7. 安全审查

- 鉴权：3 个新端点继承既有 `router = APIRouter(dependencies=[Depends(getCurrentUser)])`，与既有 `/sessions/*` 一致。
- 删除硬删除但 UI 强制 `Popconfirm` 二次确认，避免误删。
- 无 SQL 注入面：所有 SQL 走 SQLAlchemy 参数化（`session_id` 经 FastAPI path 验证）。
- 无 PII 泄露：响应仅含 `last_question`/`last_answer_preview`（后端截断 30/100 字）。
- localStorage 仅存非敏感元数据（sessionId + boolean），无 token/密码。
- Tenant 隔离：本期未强制（与 `/api/v1/chat` 行为一致）；endpoint docstring 已标注多租户部署需在 service 层加 `tenant_id` 过滤。

**已交 security-reviewer 审查（10 项发现）**：
- CRITICAL（3）：无 ownership / tenant 隔离 → 与现有 `/api/v1/chat`、`/sessions/{id}/usage` 一致，plan §1.4/§8.4 显式标注本期不动，endpoint docstring 已注明多租户部署扩展位。
- HIGH（2）：无 rate limit / sessionId 无格式校验 → 与既有 `/sessions/*` 端点保持一致（slowapi middleware 兜底）；格式校验可后续 PR。
- MEDIUM（3）：404 detail 暴露 sessionId、DELETE 404、截断无省略号 → 均与计划 §1.2/§8.3 显式约定一致。
- LOW（2）：CSRF / localStorage → 现有 auth 走 header 非 cookie，sessionId 非敏感。

无 CRITICAL 阻塞项；所有发现均为已文档化的已知 trade-off。

## 8. 部署验证

```bash
# 后端
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5432/qa_metadata_test \
  uv run pytest app/tests/integration/test_chat_history_api.py app/tests/integration/test_session_api.py -v
# → 18 passed

# 前端
cd frontend
npx vitest run                           # 258 passed
npx tsc --noEmit                         # clean
```

## 9. 已知缺口（待办）

**已修复（本轮）**：
- asyncpg `DELETE rowcount` 在某些版本返回 -1 导致 404 误判 → 改用 `.returning(id)` 取真实删除行数。
- `before_id` query 参数与 Python `beforeId` 参数 FastAPI 不自动转换 → 显式 `alias="before_id"` + 重命名为 `before_id`。

**遗留（非本次引入，记录在案）**：
- `chartOption` / `data` / `extractedEntities` / `queryPlan` / `intent` / `affinityStatus` 等字段未持久化，历史回放仅展示文本 + SQL。如需回放图表，单独 PR 增加 `SessionMessage.chart_option JSONB` + `data JSONB` 列 + Alembic 迁移。
- Tenant 隔离未实现：本期依赖单租户环境，与 `/api/v1/chat` 行为一致。
- 持久化仅 2 个键；如未来需持久化更多 UI 状态（如 datasourceId/modelId），可扩展 `persistChatUiState.ts` 的 `read/write` 接口。

## 10. 关联

- Wiki：本 summary.md
- 计划：`/Users/sunql/.claude/plans/mighty-snacking-storm.md`
- 规则：`Harness/rules/开发流程规范.md`（TDD、code review）
- 既有相关：后端 `chat_service.py`（2206 行，新增功能不并入）、`api/v1/session.py`（既有 token 用量端点，本次扩展并入）。