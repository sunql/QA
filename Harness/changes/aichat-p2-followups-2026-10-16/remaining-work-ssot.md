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
> **2026-10-17 额外**：N4 qwen 多步中断**手动**续跑 plan 完成（**PR #48** `5f447349` merged），
> 17 commits (含 1 main-merge + 1 sawTerminal 收尾), 24 files, +1775/-90, 31 tests passing。
> 该 PR 是用户级"Qwen 失败后手动选 deepseek 续跑"功能，**不在本 P2 SSOT 范围**，
> 由独立 plan `docs/superpowers/plans/2026-10-17-n4-qwen-resume.md` 覆盖。
> 回归验证：4 文件 (chatKpiWarning / chatApi / ChatPage / streamChunkParser) 72/72 全绿，0 真红。
>
> **CLOSED 2026-10-17**：N4 PR #48 merged，本 SSOT 仅剩 2 条"非 P2"项
> （Task 12.5 1 Important+4 Minor 后续小批 / N4 qwen 知识保留独立 plan），均已**显式**标注 ⏳。
>
> **真正仍 OPEN**（优先级表实际状态 2026-10-17）：
> - **Task 12.5 1 Important + 4 Minor 后续小批**（30m，PR #44 闭环外的剩余项）
> - **N4 qwen 知识保留**（独立 plan，"按任务匹配模型"路由策略）

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

### M4 — SessionQueryState NULL legacy 数据迁移 ✅ **PR #35** Main `433b6355`

**问题**：P0-3 之前写的 `(NULL, NULL, session_id)` 旧行可被任何用户读（P0-3
三元过滤在 NULL 行不命中，落到 session_id-only 路径）。

**最终修法**（采用方案 A）：
- alembic 0128→0131 renumber
- `chat_context.py` loadSessionState 加 `WHERE user_id != -1` 读侧排除
- 29 个 legacy `_saveQueryState` 调用补 `user=` / `datasource=` 关键字
- 墓碑化（`user_id = -1`）+ 读侧守卫双重保险

**验证**：
- 962 后端测试 886 绿 76 红全 rot 0 回归
- 12.4 security review 0 Critical 0 High

**PR #35**（2026-10-17, compat with M5）合入 main `433b6355`。

### M5 — stub 模式 dbUserId=None 半 scope 行 ✅ **PR #42** Main `6dce0f69`

**问题**：AUTH_STUB_ENABLED=1 时，anonymous 用户的 `dbUserId=None` 写入
SessionQueryState 形成 `(NULL, datasource, session)` 半 scope 行。
这些行被跨用户/跨会话错读。

**最终修法**：
- `SessionQueryState` 新增 `is_stub` 列
- `_saveQueryState` 收到 `userId=None` 时设 `is_stub=True`
- load 侧加 `WHERE is_stub=false` 排除
- M4 NULL 墓碑与 M5 is_stub 共存（不互斥）

**验证**：
- 26fab13f review 闭环：④b stub→stub 真路径测试 + 删冗余 default + 合并顺序 doc
- 与 M4 schema 兼容，alembic head 同步推进

**PR #42**（2026-10-17, M5 stub-scope marker）合入 main `6dce0f69`。

### L1 — LLM prompt 注入防御 ✅ **PR #43** Main `8bc15b57`

**问题**：user-controlled content（数据源返回的字符串、agent 任务描述、ontology
class label）注入 LLM prompt → 模型被诱导越权或泄露 system prompt。

**最终修法**（5 层防御）：
- 边界显式 XML/JSON 包装（5 字符 escape）
- U+200B 零宽字符绕过防御
- system prompt 强约束 + 显式 guard preamble
- 输出 schema 严格校验 + 拒绝未匹配
- 6 LLM 入口全覆盖（Plan/Answer/Multi-step/L4/Hypothesis/Wiki/RAG）
- 11-label 白名单（agent 工具调用 + ontology class label）

**验证**：
- 78/78 L1 测试绿
- 20 红队 fixture（per `fix/l1-agent-loop-test` ee934a5）
- 12.4 security review 撤销 0125 误报

**PR #43**（2026-10-17, L1 prompt 注入防御）合入 main `8bc15b57`。

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

#### 1 Important ✅ 已修 PR #44 Main `270df6f4`

**Finding**：`chat_stream.py:626-627` 在 `if featureResp is not None` 块**之前**
计算 `totalTokens` / `totalCost` / `affinityConfig` / `affinityPayload`（含
async DB call `_buildAffinityStatus`），但这些值仅在 `featureResp is not None`
分支内被消费——既浪费又触发不必要的 DB round-trip。

**最终修法**（PR #44）：
- 把 `_costForSql` + 4 行算术从 line 603 下移到 `'if featureResp is not None: return'`
  之后的 try 块前
- 原位置浪费一次 `_costForSql` 调用（featureResp 命中路径用 `featureResp.cost`
  不消费 SQL 总 token/cost）
- `_buildAffinityStatus` 已在分支内不需移动
- 注：line 704+ 的 `totalTokens +=` / `totalCost +=` 仍依赖此初值，**不能直接删**
  （reviewer SSOT 误判为死代码）

**PR #44**（2026-10-17, Task 12.5 闭环）合入 main `270df6f4`。

#### 4 Minor ✅ 已修 PR #44 Main `270df6f4`

- `chatStore.ts:826` `err.message.includes("interrupted")` magic string →
  抽 `STREAM_INTERRUPTED_DETECTION` 常量（**注**: line 826 而非 SSOT 1584 — plan 漂移）
- `error_classification.py:101` `Nl2SqlError → PROVIDER_PROTOCOL` 注释澄清
  （line 101 而非 SSOT 866）
- `chat_context.py:621` ORM 原地更新 + `session.add(existing)` 注释补充
  （line 621 而非 SSOT 246）
- `intent_service.py:641` step 8 短追问 + 追问词 → CLARIFY 注释双路径
  （line 641 而非 SSOT 983）

**PR #44**（2026-10-17, Task 12.5 闭环）合入 main `270df6f4`。0 回归：
chat_stream 10 红 + intent 1 红 = pre-existing rot（stash 验证 main tip 也同样 11 红）。

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

### Frontend 7 red test files ⏳ 4h+ ~~(2026-10-17 删除)~~

**2026-10-17 实际状态**：4 文件 72/72 全绿，无真红。

- chatStore.ts 8 红 + chatKpiWarning 2 红 + chatApi 2 红 + ChatPage 5 红 → 全部为 N4 plan rot 修复连带解决
  - chatStore.ts:749 重复 `} else {` rot 已在 N4 plan Task 2 preamble 修（commit `64663e73`）
  - chatStore.ts 3 tsc 错（515/656/664）已在 N4 plan Task 5 preamble A 修（commit `0d98339f`）
  - chatApi.test.ts 2 rot (`resumeMultiStepRun is not a function`) 已在 N4 plan Task 1 修（commit `deb4fdcc`）
- 回归验证（merge N4 → main 后，PR #48 合并前）: 71/71 全绿；sawTerminal 收尾后 72/72
- 24 个 pre-existing tsc 错（chat.ts 6 / datasourceScope 5 / client-* 10 / RequireAuth 2 / ChatPage 1）均与本 SSOT 无关，留 P3 backlog
- 详见 [[qa-system-chat-multistep-line-cap-exemption]] / [[qa-system-chatstore-duplicate-else-bug]]

**SSOT 行删除理由**：本节原内容"4h+ P0-3/0107 行为漂移"是基于 pre-N4-merge 状态估算；
N4 PR #48 实际落地后该估算已**过时**，实际工作量 = 0（已自动闭环）。

### X1-X5 — 三份 1012 报告 cross-refs 统一 P0 ✅ 全部 5 PR 合入 main

per `docs/superpowers/specs/2026-10-13-1012-code-review-cross-refs.md`：
- **X1 模型配置管理权限** ✅ **PR #45** `d5f108df` — service guard + API 显式传 actor
- **X2 must_change_password 服务端强制** ✅ **PR #46** `85d94fa1` — isAllowedWhenMustChange + 边界钉死
- **X3 检查点恢复保留研究问题** ✅ **PR #39** `5c12b2c3` — alembic 0128→0130 renumber
- **X4 流式 CRLF + EOF 守门（chat + research 共用）** ✅ **PR #47** `3c7ea35f` — additive parser 改动（compat with M5）
- **X5 会话归属 + 状态互斥** ✅ **PR #41** `2c037d72` — abort→answer 200→409 契约

### 71 红收尾 + N3 预算降级信号 ✅ **PR #51** Main `655a4cbe`

8 commits / 31 files / +1190/-68 / 350 测试 0 回归（PR #20/#21/#24/#25/#26/#27/#23 净收）：

| 来源 PR | 修法 | 红 |
|---|---|---|
| #20 | FastAPI 0.136.1 `_IncludedRouter` 不存在，删 dead code 分支 | 1 |
| #21 | `/share/{token}` 拎独立 router，去 `mustChangePasswordGuard` | 1 |
| #23 | 4 wiki fixture password + cache_clear + item.profile + must_change_password | 9 |
| #24 | 3 fixture 同步 item.profile + must_change_password（与 #23 同 root cause family） | 13 |
| #25 | rbac review 修复 + 4 fixture 跟新契约对齐（password 必填 / item.profile 叶子 / 签名补 dbSession） | 12 |
| #26 | ontology fixture 13 文件 mock 跟 M0-P0.4 3-collection refactor 对齐 | 9 |
| #27 | M3 预算闸门静默失效 → 用户可见降级信号（第 7 个 error code `ERROR_BUDGET_METERING_DEGRADED`） | 0 红 (净功能) |

测试验证（10 integration 文件 187/187 绿 + ontology 13 文件 163/163 绿 = **350/350**）：
- test_rbac_api 42, test_user_clearance 3, test_route_auth_guard 4, test_identity_create_user_password 3,
  test_evaluation_report_share 14, test_wiki_link_admin_api 12, test_wiki_import_api 43,
  test_wiki_import_retry_api 10, test_wiki_reclassify_api 21, test_wiki_suggestion_api 35
- ontology 13 文件 163 passed (commit `31af4202` 描述)

合并时阻塞：test_rbac_api 11 真红（password 必填 + item.profile 叶子 + 签名补 dbSession 缺参）→ `8dd5e7a4` 修完 42/42 绿。

### N4 — qwen-uplift 知识保留 ⏳ 独立 plan

per `qa-system-qwen-multistep-uplift` 记忆，qwen-uplift 回滚后
`pc.selected` frozen + 路由 hook 不可达的知识未沉淀。

**所需**：独立 plan（"按任务匹配模型"的更激进方案）

**注意区分**：N4 qwen 知识保留 ≠ N4 手动续跑 PR #48。
- **N4 手动续跑**（PR #48，已合）：用户级"Qwen 失败后手动选 deepseek 续跑"功能，**本 SSOT 范围外**
- **N4 qwen 知识保留**（本条）：路由策略级"按任务模式自动选模型"功能，**仍 OPEN**

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
| 8 | N4 qwen 知识保留 | ⏳ | — | 独立 plan（路由策略："按任务匹配模型"） |
| ─ | chat PLAN_EMPTY fixture | ✅ | `29f481bd` (#29) | 4 chat test files 38 reds |
| ─ | model_config_api apiKey fixture | ✅ | `c025c04a` (#33) | 422 校验对齐 P1-2 |
| ─ | **N4 手动续跑** (PR #48) | ✅ | `5f447349` (PR #48, merged 2026-10-17) | 17 commits / 24 files / +1775/-90 / 31 tests；**SSOT 范围外**，仅 cross-ref |

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
- **N4 手动续跑** cross-ref (SSOT 范围外独立 plan)：
  - **PR #48** feat(n4): qwen 多步任务中断后跨模型手动续跑 (2026-10-17) ✅ merged `5f447349`
  - 17 commits (含 main-merge + sawTerminal 收尾), 24 files, +1775/-90, 31 tests
  - sawTerminal 收尾 commit: `89d31119` (4 参 resumeMultiStepRun 加 EOF 守门)
  - Spec: `docs/superpowers/specs/2026-10-17-n4-qwen-resume-design.md`
  - Plan: `docs/superpowers/plans/2026-10-17-n4-qwen-resume.md`
  - SDD ledger: `.superpowers/sdd/2026-10-17-n4-qwen-resume/progress.md`
  - 实施 worktree: `/private/tmp/qa-n4-qwen-resume` (已 `git worktree remove --force` 清理, 2026-10-17)
  - 合并冲突: 2 处 (chatStore.ts onStepPlan/onStepResult + chat.ts resumeMultiStepRun duplicate export), 均保留 main 侧 + 删 N4 重复函数
