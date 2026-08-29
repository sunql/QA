# 变更：会话问答导出 PDF

- **日期**：2026-08-16
- **作者**：AI 助手
- **Phase**：Phase 5（生产增强）
- **状态**：done

## 1. 需求

智能问答界面增加「导出 PDF」能力，不影响既有功能。

**用户决策**：
- 实现方案：后端生成（reportlab + python-markdown）
- 入口：全局「导出当前会话」+ 单条 assistant「导出此条」按钮（默认隐藏，依赖 message.dbMessageId 已回填）
- 内容：Markdown 问答文本 + 元信息（时间/模型/Tokens/成本）+ 图表占位（chart_option 未持久化）

**验收**：
- 不修改任何既有端点行为（chat / messages / delete / list 等）
- 后端 endpoint 在既有 `/api/v1/sessions` 路由组内，限流 / 鉴权与既有 session 端点一致
- PDF 文件名包含 sessionId 或 messageId，浏览器 attachment 触发下载

## 2. 设计评审

- **依赖选型**：`reportlab>=4.2.0` + `markdown>=3.6` 两个纯 Python 包，无系统依赖。
  reportlab 提供 CIDFont（STSong-Light）原生支持中文，零字体文件。
- **图表占位策略**（关键决策）：`SessionMessage` 当前未持久化 `chart_option / data`，
  服务端无法重渲染图表。本期采用「灰色占位框 + 类型标签」，避免：
  1) 重新跑 SQL 带来的性能 + 权限风险（用户当前数据源可能无访问权限）
  2) 引入 Pillow 等图像库依赖
  后续单独 PR 增加 `SessionMessage.chart_option JSONB + chart_data JSONB` 列后，
  可改为真图表渲染（前端或后端）。
- **Markdown → ReportLab**：用 `python-markdown` 转 HTML，再用 `html.parser` 拆 block
  节点（标题/段落/列表/引用/代码块/表格/水平线），每个 block 转 platypus Flowable。
  不直接走 HTML→Paragraph（不支持表格/列表嵌套）。
- **元信息精度**：model_name/tokens/cost 来自 `session_token_usage`，
  按 `(session_id, request_time <= message.created_time)` 取最近 1 条；与 message
  无 FK，时序匹配启发式。失败时元信息留空（不阻塞导出）。
- **HTML 转义**：用户/助手 content 中的 `< > &` 在 `_BlockExtractor.handle_data`
  中 `html.escape(quote=False)`，避免注入额外样式。
- **限流**：复用 `rateLimitValue`（默认 30/min），与既有 session 端点一致。
- **文件名模板**：`qa-session-{sessionId}.pdf` / `qa-message-{messageId}.pdf`，
  sessionId 走 FastAPI path 校验（`String(64)`）+ `messageId` 走 `Query(ge=1)`，
  无注入面。
- **数据库**：无迁移，`SessionMessage` 列未变，`session_token_usage` 仍按既有 schema。
- **前端入口**：全局按钮放标题右侧 Space，单条按钮放 assistant 卡片底部 actions
  行；均通过 antd Space/Button 标准组件，不影响既有布局。
- **前端下载**：用原生 `URL.createObjectURL + <a download>` + `setTimeout 1000ms revoke`，
  与 `ChartRenderer` 既有 `downloadBlob` 复用同一 helper。

## 3. 数据模型变更

**无 Alembic 迁移**。仅修改 `app/services/session_history_service.py` 的读路径，
未触碰三表 schema。

## 4. 接口契约变更

新增 1 个端点：

| 端点 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/sessions/{sessionId}/export.pdf?message_id=N` | 导出 PDF（messageId 缺省=全会话） |

返回：`application/pdf`（attachment），文件名：
- `qa-session-{sessionId}.pdf`（全会话）
- `qa-message-{messageId}.pdf`（单条）

错误码：
- 404：session 无任何消息 / messageId 不属于该 session
- 401：未携带 `X-User-Id` / `X-Tenant-Id`（依赖既有 `getCurrentUser`）
- 429：超出 `rateLimitValue`（默认 30/min）

新增 4 个 Pydantic / message 常量（`error_messages.py`）：
- `MSG_EXPORT_SESSION_EMPTY`
- `MSG_EXPORT_MESSAGE_NOT_FOUND`
- `MSG_HISTORY_EXPORT_FILENAME` / `MSG_HISTORY_EXPORT_MESSAGE_FILENAME`
- `MSG_HISTORY_EXPORT_CONTENT_DISPOSITION`

## 5. 实现要点

**新增文件**：
- `backend/app/services/pdf_export_service.py`（~340 行）：
  - `ChatExportPayload` / `ChatExportTurn`（frozen dataclass）
  - `_BlockExtractor`（HTMLParser 子类，按 `_BLOCK_TAGS` 切分）
  - `ChatExportPdfBuilder.build(payload) -> bytes`
  - `_register_cjk_font_once()`：CIDFont 注册幂等
  - 字符转义在 `handle_data` 内 `html.escape(quote=False)`，未知 inline 标签降级为文本
  - 图表占位 Table 流式对象（灰色边框 + 类型标签）

**修改文件**：
- `backend/pyproject.toml`：+`reportlab>=4.2.0`、+`markdown>=3.6`
- `backend/app/services/session_history_service.py`：
  - 新增 `buildExportPayload(sessionId, *, messageId=None) -> ChatExportPayload | None`
  - 私有 helper：`_buildFullSessionPayload` / `_buildSingleTurnPayload` /
    `_pairMessagesToTurns` / `_fetchUsageMetaByMessage` / `_deriveTitle`
  - 不修改既有 3 个方法（listChatSessions / loadFullMessages / deleteSessionHistory）
- `backend/app/api/v1/session.py`：+`exportSessionPdf` endpoint（含 `@limiter.limit`）
- `backend/app/domain/error_messages.py`：+5 个常量

**前端**：
- `frontend/src/api/chatHistory.ts`：+`exportSessionPdf(sessionId, messageId?)`
  用原生 `axios.get(..., { responseType: "blob" })` 绕过 ApiResponse 信封解包
- `frontend/src/types/chat.ts`：+`dbMessageId?: number`（MessageItem 单条按钮 gating）
- `frontend/src/pages/ChatPage.tsx`：`handleExportSession(dbMessageId?)` 全局入口
- `frontend/src/components/chat/MessageList.tsx`：透传 `exporting` + `onExportSingleTurn`
- `frontend/src/components/chat/MessageItem.tsx`：assistant 卡片底部「导出此条」link 按钮
- `frontend/src/i18n/{zh-CN,en-US}.ts`：+`chat.exportPdf.{fullButton,singleButton,fullAriaLabel,singleAriaLabel,downloading,failed,emptySession}`

## 6. 测试

**后端**（真实 PG + 完整 API 链路）：
- 新建 `test_export_pdf_api.py`：6 用例（全会话 / 空 session 404 / 跨 session messageId 404 / 单条 / 长 SQL+多 markdown / 文件名格式）
- 回归 `test_session_api.py` + `test_chat_history_api.py`：18 用例全绿
- 合并 24/24 通过

**前端**（vitest + jsdom）：
- `tests/ChatPage.test.tsx`：+4 用例（空 messages warning / 全局按钮调用 / dbMessageId 已回填显示单条按钮 / 缺省不显示）
- 全量：262 passed（27 个 test file，12.46s）
- TypeScript `tsc --noEmit`：clean

**覆盖率**：vitest thresholds 80% 通过；pytest --cov fail_under 80 待 CI 跑。

## 7. 安全审查

**已交 code-reviewer + security-reviewer 双审查**：

### 7.1 code-reviewer 结论：APPROVE（0 CRITICAL / 0 HIGH / 2 MEDIUM / 2 LOW）

- 类型/Pydantic：snake_case + CamelModel alias 对齐，ChatMessage.dbMessageId 与 ChatMessageRead.id 语义一致
- HTML 转义：`_BlockExtractor` handle_data 全量 escape，SQL 内容额外 escape 入 Preformatted，无 XSS
- 资源管理：io.BytesIO 局部 + 无状态 builder，无泄漏
- 既有功能零副作用：三表只读，无 UPDATE/DELETE

MEDIUM/LOW 均为行数 / best-effort 降级日志边界，无实质缺陷。

### 7.2 security-reviewer 10 项发现 + 决议

| # | 级别 | 缺陷 | 处理 |
|---|------|------|------|
| 1 | CRITICAL | 无 ownership/tenant 校验 | **不修**：与既有 `/sessions/*` 端点行为一致（line 109-110 注释已注明），多租户部署后续 PR |
| 2 | HIGH | PDF 大小无上限（DoS） | **已修**：`_MAX_TURNS_PER_EXPORT=500` + `_MAX_CONTENT_BYTES=50_000` 双护栏（service 层截断） |
| 3 | HIGH | 错误 detail 泄露 sessionId/messageId | **已修**：controller `from None` 抹除 exc detail，测试新增「404 body 不含 ID」断言 |
| 4 | MEDIUM | HTML 注入 ReportLab inline 标签 | **已修**：新增 `_sanitize_inline_html` 白名单（block 全集 + inline {b,i,br,font}，font 仅 color/face），剥除 `<script>/<a>` 等危险标签 |
| 5 | MEDIUM | 前端文件名未 sanitize | **已修**：新增 `safeFilenamePart` 仅允许 `[A-Za-z0-9_-]` + 64 字截断 |
| 6-10 | LOW | SQL 注入 / messageId 校验 / CRLF / URL revoke / 限流 | 全部 SAFE（已确认） |

修复后回归：后端 24/24 + 前端 262/262 通过，TS clean。

### 7.3 既有 security 防线

- 鉴权：`Depends(getCurrentUser)`（既有 router，未引入新 bypass）
- SQL：SQLAlchemy 参数化，无原始字符串拼接
- 注入面：filename 走 FastAPI path validation（String(64)）+ Query(ge=1)
- HTML 转义：`_sanitize_inline_html` 双层（block 标签白名单 + inline 标签白名单 + font 属性白名单）
- Tenant 隔离：本期不动（与 `/api/v1/chat` 既有行为一致）

## 8. 部署验证

```bash
# 后端
cd backend
uv pip install -e ".[dev]"  # 含 reportlab + markdown
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5432/qa_metadata_test \
  uv run pytest app/tests/integration/test_export_pdf_api.py \
                  app/tests/integration/test_chat_history_api.py \
                  app/tests/integration/test_session_api.py -v
# → 24 passed

# 前端
cd frontend
npx vitest run                            # 262 passed
npx tsc --noEmit                          # clean
```

## 9. 已知缺口（待办）

**已修复（本轮）**：见 §2/§5 设计评审。

**遗留**：
- `chart_option` / `data` 未持久化 → PDF 图表区为占位框。后续 PR 增加
  `SessionMessage.chart_option JSONB + chart_data JSONB` 列 + Alembic 迁移，
  再补真图表渲染路径。
- `dbMessageId` 未在 `sendMessage` 完成后回填 → 当前会话的单条按钮默认隐藏；
  全局导出按钮正常工作。回填需要后端 sendMessage 响应新增字段（流式 done 事件
  也要补），独立 PR。
- Tenant 隔离：与既有 session 端点一致未强制，多租户部署需在 service 层加
  `tenant_id` 过滤。
- 元信息时序匹配启发式：usage 行无 message FK，按 `request_time <= created_time`
  取最近一条。若 usage 行时间与 message 时间错位，元信息可能错配到上一轮；
  PDF 不显示错配警告（best-effort 标注）。
- 单条按钮 UI 提示：当前仅按 `dbMessageId` 是否 `undefined` 显隐，无 Tooltip
  解释为什么隐藏。后续 PR 加 disabled + tooltip「暂未启用单条导出」。

## 10. 关联

- Wiki：本 summary.md
- 既有相关：`feat-chat-history-panel`（右侧历史面板，共用 session router）
- 规则：`Harness/rules/开发流程规范.md`（TDD、code review）
- 后端既有文件：`backend/app/services/session_history_service.py`（不污染）、
  `backend/app/api/v1/session.py`（既有 token 用量端点，本次扩展并入）