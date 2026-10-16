# aichat P2 后续工作项 SSOT（2026-10-16）

## 范围

承接 P2-1 security review + aichat-priority plan Task 12 verification gap。
本分支 `chore/aichat-p2-followups` 落地 M1；其余项需独立 PR 派发。

## 已落地（本批）

### M1 — SSE error + HTTP DomainError 脱敏 ✅ commit `52b32928`

**问题**：LlmClientError / Nl2SqlError / DataSourceError 的 `exc.message`
直传 SSE error 事件 + HTTP response body，泄露 provider 名 / status code /
API key hint / endpoint 信息。

**修法**：
- `app/services/error_classification.py` 新增 `sanitizeErrorMessage(exc, errorType)`
- 4 类 passthrough（DOMAIN / SCHEMA_REFERENCE / SQL_SYNTAX / PERMISSION）保留 exc.message
- 6 类脱敏（PROVIDER_* / LLM / DB_CONNECTIVITY / QUERY_TIMEOUT / INTERNAL）
- 5 个 MSG_*_PROVIDER / _DATASOURCE / _QUERY_TIMEOUT / _LLM 常量入 `messages_zh.py`
- `app/main.py` handleDomainError 走 sanitize
- `app/services/chat_stream.py` 4 处 error 事件全部走 sanitize

**验证**：
- 19 个新 test_sanitize_error_message 用例
- 48/48 M1 相关测试绿
- 10 个 chat_service_stream 失败是 main 已有 pre-existing rot（git stash 验证 baseline 一致），与 M1 无关

**反向断言**：
- 5 个 parametrize 用例验证 provider / port / status code / "unauthorized" / "unavailable" 等敏感词不进 user 文案

## 后续 PR（本批未做）

### M4 — SessionQueryState NULL legacy 数据迁移 ⏳ 需数据决策

**问题**：P0-3 之前写的 `(NULL, NULL, session_id)` 旧行可被任何用户读（P0-3
三元过滤在 NULL 行不命中，落到 session_id-only 路径）。

**候选修法**（待用户定）：
- A：migration 显式 `UPDATE session_query_state SET user_id = -1 WHERE user_id IS NULL`，与 `models.py:881` 的 NULL 兼容注释一致；loadSessionState 加 `WHERE user_id != -1`
- B：同 `SessionMessage` 加 `user_name` 字符串列，让 session_guard 跨表 join
- C：1 周后清理（**当前推荐**）—— 等 P0-3 落地 1 周，无活跃 NULL 行后批量 stamp

**所需**：1 测 + 数据迁移审批 + alembic 0128

### M5 — stub 模式 dbUserId=None 半 scope 行 ⏳ 4h

**问题**：AUTH_STUB_ENABLED=1 时，anonymous 用户的 `dbUserId=None` 写入
SessionQueryState 形成 `(NULL, datasource, session)` 半 scope 行。
这些行被跨用户/跨会话错读。

**候选修法**：
- 路由层拦截 stub 模式 chat 请求 → 拒绝 SessionQueryState 写入
- 或：`_saveQueryState` 收到 `userId=None` 时**不写库**（in-memory only）
- 或：写库但加 `is_stub=True` 标记，load 时排除

**所需**：1 测 + 1 回填脚本（清理既有半 scope 行）

### L1 — LLM prompt 注入防御 ⏳ 1d+

**问题**：user-controlled content（数据源返回的字符串、agent 任务描述、ontology
class label）注入 LLM prompt → 模型被诱导越权或泄露 system prompt。

**候选修法**：
- 边界显式 XML/JSON 包装 + system prompt 强约束
- 输出 schema 严格校验 + 拒绝未匹配
- agent 工具调用白名单

**所需**：独立 plan（含 red team 评估）

### L4 — `__non_finite__` 哨兵 ⏳ 30m 实际非 dead

**复核结论**：标记 `{"__non_finite__": "..."}` 是 load-bearing（让
`allow_nan=False` 不抛错且符合 RFC 7159），不是 dead code。前端缺消费者
但**这不影响安全性**（前端遇到 dict 类型做兜底渲染即可）。

**判定**：skip，不修。`__non_finite__` 是 JSON 序列化正确性必需，UI 渲染
是前端降级问题，独立的 UX 项。

### Task 12.5 — code-reviewer on P0-P1 changed files ⏳ 1 派发

**范围**：
- backend/app/services/chat_service.py（1986 行）
- backend/app/services/chat_stream.py
- backend/app/services/chat_context.py
- backend/app/services/intent_service.py
- backend/app/api/v1/chat.py
- backend/app/services/stream_events.py
- backend/app/domain/models.py
- frontend/src/stores/chatStore.ts
- frontend/src/stores/stream_chunk_parser.ts

**焦点**：
- chat_service.py 1986 行（豁免内但需 reviewer 确认 mixin 顺序）
- immutability（不原地修改 ORM）
- 异常 handler 错误码 + 状态码
- 前端 store 改动 wire（prop / store flag / 调用点三处同步）

### Frontend 7 red test files ⏳ 4h+

**已知红文件**（per coverage-gate-report.md）：
- chatStore.test.ts 8 红（P0-3/P1-3 行为漂移）
- chatKpiWarning.test.ts 2 红
- chatApi.test.ts 2 红
- ChatPage.test.tsx 5 红
- streamChunkParser branches 75→80%

**意义**：80% coverage 闸门当前不可达；修完后才能跑 step 12.1 覆盖率基线。

### X1-X5 — 三份 1012 报告 cross-refs 统一 P0 ⏳ 5 独立 branch + PR

per `docs/superpowers/specs/2026-10-13-1012-code-review-cross-refs.md`：
- X1：模型配置管理权限
- X2：must_change_password 服务端强制
- X3：检查点恢复保留研究问题
- X4：流式 CRLF + EOF 守门（chat + research 共用）—— **P1-3 已部分覆盖，需确认**
- X5：会话归属 + 状态互斥（chat + research 联合）

**所需**：5 个独立 worktree + 5 PR（X4 可能 P1-3 已覆盖，需核对后决定保留/合并）

### N4 — qwen-uplift 知识保留 ⏳ 独立 plan

per `qa-system-qwen-multistep-uplift` 记忆，qwen-uplift 回滚后
`pc.selected` frozen + 路由 hook 不可达的知识未沉淀。

**所需**：独立 plan（"按任务匹配模型"的更激进方案）

## 优先级排序（建议）

| 序 | 项 | 估时 | ROI | 阻塞 |
|---|---|---|---|---|
| 1 | Frontend 7 red（先 chatStore 8 红） | 4h | 高 | coverage 闸门 |
| 2 | Task 12.5 code-reviewer | 1h | 中 | 验证 P0-P1 整体质量 |
| 3 | X1-X5 dispatch（5 PR） | 2h/个 | 中 | 后续各独立闭环 |
| 4 | M5（stub 模式回填） | 4h | 中 | OWASP A01 残留 |
| 5 | M4（数据迁移） | 1h + 审批 | 中 | OWASP A01 残留 |
| 6 | L1（prompt 注入） | 1d+ | 高 | 需独立 plan |
| 7 | N4（qwen 知识保留） | 独立 | 低 | 不影响生产 |

## Why 此 SSOT 单独存在

- aichat-priority plan Task 12 verification 缺项 + P2-1 review 后续 finding
  散落 4 份报告（Harness/changes/aichat-priority-remaining-2026-10-16/ +
  docs/superpowers/specs/2026-10-13-1012-code-review-cross-refs.md +
  coverage-gate-report.md + 本 SSOT）
- 现在落地 = 后续 7 个独立 PR 的 SSOT，每项的"已做 / 候选修法 / 估时 / 阻塞"齐了

## 链接

- 已落地 commit：`52b32928` (M1)
- 报告：`/tmp/qa-aichat-priority-remaining/Harness/changes/aichat-priority-remaining-2026-10-16/security-review-report.md`
- Cross-refs spec：`docs/superpowers/specs/2026-10-13-1012-code-review-cross-refs.md`
- Coverage gate：`/tmp/qa-aichat-priority-remaining/Harness/changes/aichat-priority-remaining-2026-10-16/coverage-gate-report.md`
- Plan：`docs/superpowers/plans/2026-10-13-aichat-service-priority.md`
