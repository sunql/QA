# aichat P2 后续工作项 SSOT（2026-10-16）

> **2026-10-17 全面收尾**：所有 11 个 PR 已合入 main。Main HEAD: `433b6355`。
> - `7b43d428` P2-1 M1 落地 + chatStore backport (#31)
> - `29f481bd` chat PLAN_EMPTY fixture (#29)
> - `c025c04a` model_config_api fixture (#33)
> - `7611fbeb` Task 12.5 Critical user= (#32)
> - `270df6f4` Task 12.5 闭环 1 Important+4 Minor (#44)
> - `3c7ea35f` X4 stream parser (#47)
> - `85d94fa1` X2 RequireAuth (#46)
> - `d5f108df` X1 model-config (#45)
> - `2c037d72` X5 session_claim (#41)
> - `5c12b2c3` X3 checkpoint (#39)
> - `6dce0f69` M5 stub-scope (#42)
> - `aeed0633` Frontend 13 红恢复 (#36)
> - `8bc15b57` L1 prompt 注入防御 (#43)
> - `433b6355` M4 NULL user_id 墓碑化 (#35)
>
> **关闭**：`#30`（被 #31 取代）、`#34`（被 #44 取代）。
>
> **全部 PR 已合入 main**。剩余 N4 qwen 知识保留需独立 plan。

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

### chatStore 合并残留 backport ✅ commit `95a3aaa6`

**问题**：P1-3 commit b958b855 复制粘贴 stream/non-stream 分支时未删原
`} else {`，导致 esbuild 编译失败 → 9 个测试文件 2 天阻塞（仓无 CI）。

**修法**：删 line 748 孤立 `} else {`，保留 line 749 起新分支。
**注意**：这是把 `0bfbfd53`（chore/aichat-priority-remaining 已修，但未合 main）
backport 到当前 worktree（基于 main）。两个 PR 合先后会冲突，需 rebase。

**验证**：
- 1 编译错 → 8 测试红
- 8 红都是 0107 alembic 引入的字段未填充（onChart/onStepResult handler
  未回填 `tableOption` / `visualRationale` 到 message），属真实实施 gap
- 需 0107 上下文（原始实现者）独立 PR 修

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

### Task 12.5 — code-reviewer on P0-P1 changed files ✅ 已派发

**派发**：dispatch code-reviewer subagent 对 P0-P2 9 文件 + error_classification.py
跑独立审查。报告：`/tmp/review-package-aichat-p0p2/task-12.5-review-report.md`
（1690 行 diff = 19 commit d23d091d..b632d945）。

#### 1 Critical ✅ 已修 PR #32

**Finding**：`chat_stream.py:641` `_tryFeatureResponse(session, dto, pc, outcome)`
漏掉 `user=`。内部 `_saveQueryState` 写 `userId=None` ⇒ 写 `(NULL, ds, session)`
行，P0-3 三元 scope 过滤读不到，状态被全局复用（与非流式入口
`chat_service.py:609` 口径不一致）。

**修法**（worktree `/tmp/qa-fix-feature-user` / branch `fix/chat-stream-feature-user-arg`）：
- 一行 fix：`self._tryFeatureResponse(session, dto, pc, outcome, user=user)`
- 配套 `TestTryFeatureResponseArg::test_streamQuery_call_passes_user_keyword`：
  源码静态断言（regex 扫描 `_tryFeatureResponse(...)` 调用必须带
  `user=user` 关键字参数）。
- 现有流式测试基线（`test_query_stream_emits_full_event_sequence` 等）
  受 pre-existing rot 影响（NL2SQL 计划解析落 PLAN_EMPTY），与本契约
  正交；用源码级守卫把这条 P0-3 写侧硬约束钉死。
- **TDD**：先跑 RED（chat_stream.py 调用点缺 user=user 报 assert 失败）→
  应用一行 fix → GREEN。`uv run pytest app/tests/unit/test_chat_service_stream.py::TestTryFeatureResponseArg -xvs` 1 passed in 0.82s。
- PR **#32**（2026-10-16，base=main）。

#### 1 Important ⏳ 后续小批

**Finding**：`chat_stream.py:626-627` 在 `if featureResp is not None` 块**之前**
计算 `totalTokens` / `totalCost` / `affinityConfig` / `affinityPayload`（含
async DB call `_buildAffinityStatus`），但这些值仅在 `featureResp is not None`
分支内被消费——既浪费又触发不必要的 DB round-trip。

**修法**（未做）：把 4 行 + `_buildAffinityStatus` 调用移到 `if featureResp is None`
分支内（同时把 SQL/cost 聚合下移到真正需要的位置）。

**优先级**：低——浪费一次 `_buildAffinityStatus` 不是 correctness 问题。
下一批 P2 followup PR 顺手修。

#### 4 Minor ⏳ 后续小批

- `chatStore.ts:1584` `err.message.includes("interrupted")` magic string →
  抽 `STREAM_INTERRUPTED` 常量。
- `error_classification.py:866-868` `Nl2SqlError → PROVIDER_PROTOCOL` 分类口径
  可商榷（与"重试耗尽" 注释割裂）；加澄清注释。
- `chat_context.py:246` ORM 原地修改注释里加 `session.add(existing)` 说明
  （实际上没漏，只是 reviewer 想看注释更清楚）。
- `intent_service.py:983-984` 短追问 CLARIFY 分支无 `hasPriorState` 守卫 —
  comment 没说清双路径语义。

**范围（原始）**：
- backend/app/services/chat_service.py（1986 行）
- backend/app/services/chat_stream.py
- backend/app/services/chat_context.py
- backend/app/services/intent_service.py
- backend/app/api/v1/chat.py
- backend/app/services/stream_events.py
- backend/app/domain/models.py
- frontend/src/stores/chatStore.ts
- frontend/src/stores/stream_chunk_parser.ts
- + backend/app/services/error_classification.py

**焦点**：
- chat_service.py 1986 行（豁免内但需 reviewer 确认 mixin 顺序）
- immutability（不原地修改 ORM）
- 异常 handler 错误码 + 状态码
- 前端 store 改动 wire（prop / store flag / 调用点三处同步）

**已确认 concern (reviewer 提的) 现状**：
- `KpiCatalog.datasource_id` Alembic：0125 已存在（reviewer 当时没看到 diff 外的
  迁移文件），concern 撤销。
- `_selectStateRow` 4 类 NULL/non-NULL 组合测试未在本次补（pre-existing
  rot 影响整 chat_service_stream 文件，与 Critical 修复合并风险 > 收益）。

### Frontend 7 red test files ⏳ 4h+

**已知红文件**（per coverage-gate-report.md）：
- chatStore.test.ts 8 红（P0-3/0107 行为漂移）— 1 编译错已修（commit 95a3aaa6），
  8 测试红是 0107 实施 gap，**需 0107 上下文独立 PR**
- chatKpiWarning.test.ts 2 红
- chatApi.test.ts 2 红
- ChatPage.test.tsx 5 红
- streamChunkParser branches 75→80%

**意义**：80% coverage 闸门当前不可达；修完后才能跑 step 12.1 覆盖率基线。

**chatStore 8 红根因**（实测）：
- onChart handler 收到 `tableOption` / `visualRationale` 但未回填到 message
- onStepResult handler 同上
- 历史回放（toChatMessage）路径正常
- 修法需读 0107 alembic 引入新字段的 store 实施，补上 2 个 handler 的回填

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

## 优先级排序（实际状态 2026-10-17）

| 序 | 项 | 状态 | Main SHA | 备注 |
|---|---|---|---|---|
| 1 | M1 SSE 脱敏 | ✅ | `7b43d428` (#31) | sanitizeErrorMessage + 19 tests + 5 反向断言 |
| 1' | chatStore 合并残留 backport | ✅ | `7b43d428` (#31) | 同上 PR；删 line 748 孤立 `} else {` |
| 2 | Task 12.5 Critical user= | ✅ | `7611fbeb` (#32) | `_streamQuery` → `_tryFeatureResponse` 漏 user 关键字 |
| 2' | Task 12.5 1 Important+4 Minor 闭环 | ✅ | `270df6f4` (#44) | affinity 移到 else / chatStore 魔串 / error_classification 注释 / intent 双路径 |
| 3 | X1 model-config admin 守卫 | ✅ | `d5f108df` (#45) | service guard + API 显式传 actor |
| 3 | X2 RequireAuth export | ✅ | `85d94fa1` (#46) | isAllowedWhenMustChange + 边界钉死 |
| 3 | X3 checkpoint research question | ✅ | `5c12b2c3` (#39) | alembic 0128→0130 renumber |
| 3 | X4 research SSE 共享 parser | ✅ | `3c7ea35f` (#47) | additive parser 改动 |
| 3 | X5 session_claim invariant | ✅ | `2c037d72` (#41) | abort→answer 200→409 契约 |
| 4 | M5 stub-scope marker | ✅ | `6dce0f69` (#42) | is_stub 列 + 读侧 WHERE is_stub=false |
| 5 | Frontend 13 红 | ✅ | `aeed0633` (#36) | 4 文件 111/111 全绿（chatStore + ChatPage + chatApi + chatKpiWarning） |
| 6 | L1 prompt 注入 | ✅ | `8bc15b57` (#43) | 78/78 L1 测试；6 LLM 入口 + 11 label 白名单 + U+200B |
| 7 | M4 NULL 墓碑 | ✅ | `433b6355` (#35) | 0128→0131 renumber + chat_context user_id!=-1 + 29 个 legacy 调用补 user/datasource |
| 8 | N4 qwen 知识保留 | ⏳ | — | 独立 plan |
| ─ | chat PLAN_EMPTY fixture | ✅ | `29f481bd` (#29) | 4 chat test files 38 reds |
| ─ | model_config_api apiKey fixture | ✅ | `c025c04a` (#33) | 422 校验对齐 P1-2 |

## Why 此 SSOT 单独存在

- aichat-priority plan Task 12 verification 缺项 + P2-1 review 后续 finding
  散落 4 份报告（Harness/changes/aichat-priority-remaining-2026-10-16/ +
  docs/superpowers/specs/2026-10-13-1012-code-review-cross-refs.md +
  coverage-gate-report.md + 本 SSOT）
- 现在落地 = 后续 7 个独立 PR 的 SSOT，每项的"已做 / 候选修法 / 估时 / 阻塞"齐了

## 链接

- 已落地 commit (main 2026-10-17 顺序)：
  - `7b43d428` P2-1 M1 + chatStore backport (#31)
  - `29f481bd` chat PLAN_EMPTY fixture (#29)
  - `c025c04a` model_config_api fixture (#33)
  - `7611fbeb` Task 12.5 Critical user= (#32)
  - `270df6f4` Task 12.5 闭环 (#44)
  - `3c7ea35f` X4 (#47), `85d94fa1` X2 (#46), `d5f108df` X1 (#45)
  - `2c037d72` X5 (#41), `5c12b2c3` X3 (#39), `6dce0f69` M5 (#42)
  - `aeed0633` Frontend 13 红 (#36), `8bc15b57` L1 prompt 注入 (#43), `433b6355` M4 NULL 墓碑 (#35)
- 报告：`/tmp/qa-aichat-priority-remaining/Harness/changes/aichat-priority-remaining-2026-10-16/security-review-report.md`
- Cross-refs spec：`docs/superpowers/specs/2026-10-13-1012-code-review-cross-refs.md`
- Coverage gate：`/tmp/qa-aichat-priority-remaining/Harness/changes/aichat-priority-remaining-2026-10-16/coverage-gate-report.md`
- Plan：`docs/superpowers/plans/2026-10-13-aichat-service-priority.md`
- Task 12.5 review 报告：`/tmp/review-package-aichat-p0p2/task-12.5-review-report.md`
- Task 12.5 Critical 修复：**PR #32** 合入 main `7611fbeb`
  - 原始 commit：`efc5cfe5` fix(chat): _streamQuery passes user=user to _tryFeatureResponse (P0-3 scope)
  - 原始 worktree：`/tmp/qa-fix-feature-user`
