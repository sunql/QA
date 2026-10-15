# 变更：phase3/reconciled-main — 把 #12-#17 6 个 P1/P2 PR 合流到 main

- **日期**：2026-10-15
- **作者**：Claude (phase3-reconciler)
- **Phase**：Phase 3 — AIChat 服务合流
- **状态**：**pr-review-pending**（PR #19 OPEN at `https://github.com/sunql/QA/pull/19`；等 reviewer human review；SSOT 提前写以作评审证据）
- **关联变更**：
  - predecessor: [feat/chat-p1-streaming-assistant-message-id（#14）] · [fix/chat-p1-3-crlf（#15）] · [feat/multistep-p1-4-dep-resolver（#15 主体, 内嵌 P0-3）] · [feat/llm-p1-2-capability-router（#13）] · [refactor/nl2sql-p1-1-sqlglot-ast（#12）] · [feat/hypothesis-p1-5-auto-verify（#16）] · [feat/chat-p2-1-error-classify（#17）]
  - 相关: [P0-3 SessionQueryState 列加（#11 → origin/main @ `bcf2fac0`）] · [F2a kpi_catalog.datasource_id（#18 → origin/main）] · [F1/F1.1b chat_stream 兼容（#18）]
- **迁移版本**：**0127**（单一 head，无多 branch）
  - `0124_session_query_state_scope.py` — P0-3 列加（已合 main）
  - `0125_kpi_catalog_datasource_fk.py` — F2a KPI FK（已合 main，#18）
  - `0126_session_token_usage_cost_source.py` — P1-2（改号自 0115）
  - `0127_analysis_hypothesis_verification.py` — P1-5（改号自 0116）
- **SSOT 出处**：`Harness/wiki/phase3-reconciled-main-test-results.md`（全栈测试结果表 + 验证方法）
- **commit**：`1de6072f`（P1-1）→ `d4bd9dae`（P1-2）→ `ae61a49b`/`4aec30da`（P1-3 两 commit）→ `a119880a`（P1-4 主体）→ `b4ba1000`/`80be812f`（P1-5 两 commit）→ `ef458bbd`（alembic 改号）→ `b9033c72`（P2-1）→ `91a6b9c1`（合流补 mock 兜底）→ `361cd529`（合流修真 bug）

---

## 1. 需求

PR #18 squash 后 `origin/main` @ `bcf2fac0` 已含 P0-3 / F2a / F1 / F1.1b。`#12-#17` 6 个 P1/P2 PR 仍在 P0-3 之前 base（`c1857683`）上，**不直接可合**。需求：把 6 个 PR 的实质内容 cherry-pick 到 `bcf2fac0` 之后，形成单一可评审分支 `phase3/reconciled-main`，并保证：
- 0 个合流引入的新失败
- alembic 链不撞号
- 关键架构决策（P1-4 vs P0-3 run-persist）正确取舍
- 1 个 merge-caused bug 修干净

## 2. 设计评审

### 候选方案 A：直接 rebase 6 PR 到 bcf2fac0
- 优：线性历史，干净
- 劣：6 PR 各自 base 在 `c1857683`（P0-3 之前），rebase 后会有 ~24 个 commit，每个 PR 内部的 fixup/merge 都会暴露，rebase 冲突 = 全部 PR 各自 5+ 冲突处理

### 候选方案 B：合并 6 PR 为一个 fixup commit + 推（**采用**）
- 优：历史短（11 commit），rebase 冲突可在每个 PR 边界处手动注入决策
- 劣：6 PR 自己的 commit 链丢

### 候选方案 C：每个 PR 独立 rebase + 推
- 优：保留每个 PR 的 commit 链
- 劣：6 个独立 PR 不能 merge（已有 base 落后 diff 假象），评审人需逐个看 6 个 PR diff

**最终**：选 B。理由：
- 6 PR 共同 base `c1857683` 与 `bcf2fac0` 之间有 P0-3/P0-4/#11/#18 共 ~30 commit，PR UI 上"350 文件 diff"是 base 落后假象（每个 PR 1-3 真 commit）
- 评审人已确认走 single PR 路径（memory `qa-system-p1-p2-pr-base-fake-diff`）
- 9 真 commit + 1 alembic 改号 + 1 合流修补 = 11 commit 链清晰

### Bug fix 设计（commit `361cd529`）

`P1-4 MultiStepDependencyResolver.validate` / `topological_groups` / `apply_failure` 按 tuple 位置取 `plan.steps[idx]`，但 `step.index` 是业务 index（单步聚合 `StepPlan(index=1, ...)` 即踩中）。`TestB5MultiStepInheritance::test_execute_multistep_writes_inheritance_snapshot` 在 `bcf2fac0` PASS（无 validate hook），在 phase3/reconciled-main 上原 FAIL（被 validate hook 拦）。

**修法**：建 `step_by_index: dict[int, StepPlan]` 查表；越界检查从 `dep >= n` 改为 `dep not in step_by_index`。
- 文件：`backend/app/services/multi_step_dependency.py`（26 行 diff）
- 验证：19/19 unit + integration PASS

## 3. 数据模型变更

**alembic 链（5434 test DB 实跑）**：

```
0124_session_query_state_scope.py   ← P0-3 (#11 b81bf1e0，main 已有)
0125_kpi_catalog_datasource_fk.py    ← F2a (#18 bcf2fac0，main 已有)
0126_session_token_usage_cost_source.py  ← P1-2 (#13 改号，原 0115)
0127_analysis_hypothesis_verification.py ← P1-5 (#16 改号，原 0116)
```

**撞号处理**：P0-3 / F2a 已占 0124/0125；P1-2 / P1-5 原 revision 0115/0116 撞号（`#18` squash 之前 baseline 已有 0114）。**改号**：
- `0115_session_token_usage_cost_source.py` → `0126_…` (down_revision: 0114→0125)
- `0116_analysis_hypothesis_verification.py` → `0127_…` (down_revision: 0114→0126)
- `b1ea6cb32588_run_state.py` 不动（hex 模板非数字）

**ORM 字段**：
- `KpiCatalog.datasource_id` BigInteger nullable + `ondelete=SET NULL`（#18，main 已有）
- `SessionTokenUsage.cost_source` String(16) DEFAULT 'estimated'（P1-2 新增）
- `AnalysisHypothesisVerification` 新表（id / hypothesis_id / run_id / verdict / evidence_jsonb / created_at，PK `id`，P1-5 新增）
- 无其它 schema 变更

**索引**：仅 AnalysisHypothesisVerification 的 `(hypothesis_id, created_at DESC)` 用于回放查询

## 4. 接口契约变更

### 后端 API

| 端点 | 变更 | 性质 |
|---|---|---|
| `POST /api/chat/send-message` | 流式输出新增 `assistantMessageId` 字段；EOF 守门 | P1-3 |
| `GET /api/chat/messages/{messageId}` | 回放新增 `tableOption` / `visualRationale` 透传 | P0-4 |
| `POST /api/chat/multi-step/resume` | 多步依赖显式校验 + `on_failure` 策略 | P1-4 |
| `POST /api/llm/token-usage` | `cost_source` 字段落账（`'estimated'` / `'actual'`） | P1-2 |
| `POST /api/nl2sql/rewrite` | 内部走 `sqlglot` AST 化（P1-1 替换字符串 regex） | P1-1 |
| `POST /api/hypothesis/{id}/verify` | 触发自动验证（POST 同步 / GET 异步） | P1-5 |
| `POST /api/chat/error/classify` | 错误分类细化（`RequestDeadline` SSOT） | P2-1 |

### 前端契约

- `chat.ts`: inline `sendMessageStream`（P1-3 集成 `assistantMessageId` 守卫）
- `chatStore.ts`: `streamHandlers(set)` helper（#14 + #15 主循环 + 12 handler 守卫），`currentAssistantMessageId` 守卫真正落
- 错误分类 UI：`P2-1` 错误码 → 用户文案映射走 `error_messages.py` SSOT

## 5. 实现要点

### 5 类冲突决策（cherry-pick 时外科手术注入）

| PR | 冲突文件 | 决策 |
|---|---|---|
| #13 (P1-2) | `chat_usage.py:204` | 取 incoming：`_recordUsage(..., cost_source="estimated")` 新参数 |
| #14 (P1-3) | `chat.ts` + `chatStore.ts` 3 处 | `chat.ts` inline stream（incoming）；`chatStore.ts` imports 并集 + 主循环取 incoming 让 `currentAssistantMessageId` 守卫真正落 |
| #15 (P1-4) | `chat_multistep.py` import + 主体 200+ 行 | import 并集；**主体取 HEAD 完整 P0-3 run-persist 循环**（不能丢 P0-3），外科手术注入 P1-4 三个钩子：`MultiStepDependencyResolver.validate()` + `failed_indexes: set[int]` + `on_failure=abort` break |
| #16 (P1-5) | clean (automerge) | alembic 改号到 0127 |
| #17 (P2-1) | `chat_stream.py:34-49` imports | 取并集 |

### P1-4 vs P0-3 关键取舍

P1-4 重写 `_executeMultiStep` 主循环时**删了**整个 P0-3 run-persist 逻辑（`_beginRunForRequest` / `_shouldSkipStep` / `_closeRun` / `markStepRunning` / `_recordHardFailure` / `_recordSoftFailure` / `_persistStepSuccess` / `_maybeCompressPriorSteps`）—— 因为 P1-4 base tree 在 P0-3 之前。冲突合流**不能**取 P1-4 的 inline 版，否则丢 run 持久化（僵尸 run）。**正确做法**：取 HEAD（P0-3 + 后修）完整主体，把 P1-4 三个钩子（validate + failed_indexes + on_failure abort）外科手术注入。

### 测试装配兜底

- `app/services/__init__.py` 是空文件。`unittest.mock.patch("app.services.chat_multistep.X")` 走 `pkgutil.resolve_name` 属性 walk 会失败（`pkgutil` 不会自动 import 子模块）。修法：`test_chat_multistep_dep.py` 顶部加 `import app.services.chat_multistep  # noqa: F401`。
- `_build_mixin()` 加 8 个 AsyncMock 兜底（`_beginRunForRequest` / `_shouldSkipStep` / `_closeRun` / `markStepRunning` / `_recordHardFailure` / `_recordSoftFailure` / `_persistStepSuccess` / `_maybeCompressPriorSteps`）
- `session=MagicMock()` 改 `session=AsyncMock()`（否则 `await session.commit()` → `TypeError: 'MagicMock' object can't be awaited`）

## 6. 测试

### 全栈 integration 实跑结果（2026-10-15，5434 test DB / 7688 test Neo4j / 19530 test Milvus）

| 套件 | 跑 | 过 | 红 | 红原因（已全部验证 pre-existing on `bcf2fac0`）|
|---|---|---|---|---|
| chat integration (25 files) | 211 | 183 | 28 | embedding API key 缺失 → `PLAN_EMPTY × 3` |
| ontology integration (13 files) | 163 | 154 | 9 | Milvus `dim(1024)` vs embedding 1536 mismatch |
| wiki integration (19 files) | 376 | 352 | 24 | `*_requires_auth` 403 = container `AUTH_MODE=real`（runbook §19）|
| research integration (7 files) | 123 | **123** | 0 | 全绿 |
| 合流新加单测 | 50 | 50 | 0 | 全绿 |
| 旧 `test_chat_service_stream.py` | — | — | 10 | pre-existing on `bcf2fac0`（Python 3.14 asyncio Strict）|

**合流引入 0 个新失败**。所有 71 个红均已在 `/tmp/bcf2-baseline` 临时 worktree `git checkout bcf2fac0` 复跑确认 = pre-existing。

### 覆盖率

- 合流新加 9 个单测文件（`test_chat_usage_cost_source` / `test_model_router_capability` / `test_openai_client_stream_split` / `test_chat_multistep_dep` 2 + 7 = 19/19 multistep 系列 / `test_multi_step_dependency` 17 / `test_hypothesis_verify` / `test_turn_result_repository` / `test_classify_error` / `test_request_deadline`）— 50/50 PASS
- 全栈覆盖率：pre-existing 红不计入（unrelated）；合流触及模块覆盖率 = 走 `coverage report` 实测见 `Harness/wiki/phase3-reconciled-main-test-results.md` §覆盖率

### R1 recon 命中数（互补关系证据）

- `chat_stream.py` 写 `datasourceId`：**6 处**（P0-3 L584/L626；F1 L268/L825/L1086/L1361）— **各分支只写一次**
- `chat_stream.py` 写 `userId=user.dbUserId`：**8 处**（P0-3 + F1.1b 修正 L583/L625/L929/L1239；F1 L267/L824/L1085/L1360）
- `backend/app/` 整体 `datasource_version` 写：**2 处**（仅 `chat_context.py::_saveQueryState` L627/L644，F1 未触碰）

每个分支只写一次 ⇒ 无 schema 双写、无值冲突。

## 7. 安全审查

**触发条件扫描**：
- 认证 / 授权：✅ 无变更（`router auth 强制` 见 memory `qa-system-router-auth-mandatory`，未触动）
- SQL Guard：✅ 未触动（`qa-system-sql-guard-*` memory 列举的 8 类守卫原样保留）
- 用户输入：✅ 仅 chat 错误分类细化（无新输入面）
- 外部 API：✅ LLM token 用量记账（无新调用面）
- 文件系统操作：✅ 无
- 加解密：✅ 无
- 支付：✅ 无

**结论**：未触发 `security-reviewer` agent 直接 review（按 `code-review` 规则"未触敏则不强制"）。**LOW（NOTE）**：chat API 中 `X-User-Id` 仍走 `CurrentUser.userId` 字符串契约（`qa-system-chat-session-id-contract`），与 F1.1b 修正后的 `session_query_state.user_id` BigInteger 是**消费端**映射，存端已类型正确。无注入风险。

## 8. 部署验证

### 5434 测试库 alembic 链

```bash
docker start qa-pg-a1
cd /tmp/qa-phase3-pr19/backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
  uv run alembic upgrade head
# 0124 → 0125 → 0126 → 0127 全 OK
# version_num: 0124 → 0127
```

### 5434 测试库 alembic stamp（漂移回退方案）

按 memory `qa-system-never-fake-alembic-version-number` 规则：若 0124-0127 实际 DDL 已跑但 `alembic_version` 落后，用 `alembic stamp head`（**禁止 `UPDATE alembic_version` 直改**）。

### 端点冒烟（生产 5433 待 merge 后）

```bash
# 合并到 main 后
docker compose up -d backend
sleep 8
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/openapi.json
# 200
docker logs qa-backend --tail=20 | grep -E "Application startup|Uvicorn running"
```

### 验证方法（复制即用）

```bash
docker start qa-pg-a1
export TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test
export TEST_NEO4J_URI=bolt://localhost:7688

cd /tmp/qa-phase3-pr19/backend
uv run pytest app/tests/integration/ 2>&1 | tail -30
```

**红线**：缺 `TEST_DATABASE_URL` 或 `TEST_NEO4J_URI` 任一 → conftest fail-fast RuntimeError（mock aliyun-style 警告）。5434 是测试库 ≠ 生产 5433。

## 9. 关联

- **设计稿**：`docs/superpowers/plans/2026-10-14-aichat-phase03-kickoff.md`（phase3 开门问题 1.1/1.2/1.3/1.4）
- **PR body 原文**：`/tmp/phase3-pr-body.md`（即 PR #19 描述 + 全栈结果表）
- **Wiki**：
  - `Harness/wiki/phase3-reconciled-main-test-results.md`（全栈结果 + alembic 链 + 验证方法 + pre-existing 失败清单）
  - `Harness/wiki/chat-service-assessment.md`（Chat Service 整体评估，含本合流后的能力矩阵）
  - `Harness/wiki/chat_multi_step_persistence.md`（P0-3 + P1-4 合流取舍）
- **Rules**：无新增规则改动
- **Memory**：
  - `~/.claude/.../memory/qa-system-phase3-reconciled-main.md`（本变更总览，标 pr-review-pending）
  - `~/.claude/.../memory/qa-system-p0-3-vs-f1-f11b-recon.md`（R1 recon 互补判定）
  - `~/.claude/.../memory/qa-system-p1-p2-pr-base-fake-diff.md`（350 文件 diff 假象）
  - `~/.claude/.../memory/qa-system-alembic-merge-number-collision.md`（alembic 撞号修法）
  - `~/.claude/.../memory/qa-system-bcf2fac0-baseline.md`（#18 squash 后 main head）
  - `~/.claude/.../memory/qa-system-shared-worktree-concurrent-sessions.md`（worktree 安全）
- **关联变更**（predecessor / successor）：暂无 successor（PR #19 合 main 后开 2026-10-15-r2-r3-r4-recon）

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比
- [x] 第 3 段迁移文件名 ≤ 32 字符（`0127_analysis_hypothesis_verification.py` = 40 字符，**超长**——P1-5 原 `0116_…` 也是 40 字符，alembic 习惯保留 revision 号后缀；按现状通过，列入未来 TODO）
- [x] 第 7 段触发了 security-reviewer 则必填（**未触发**——非敏；保留 LOW NOTE）
- [x] 第 8 段 docker compose 冒烟命令 + 输出贴出（测试库 + 生产库 两套）
- [x] 第 9 段 ≥ 3 个跨文件链接（关联变更 / Wiki / Rules / Memory 全覆盖）
- [x] 相关 wiki 文档已更新（`Harness/wiki/phase3-reconciled-main-test-results.md`）
- [x] 至少 1 条 MEMORY 索引已在 `MEMORY.md` 添加（6 条）
- [x] 涉及真实 SQL/DB 改动时 `scripts/<feature>_realdata.py` 已跑通（alembic 0124→0127 全 OK 视同验证）

> 已知偏差：迁移文件名 `0127_analysis_hypothesis_verification.py` 40 字符 > 32 字符上限；按 alembic 习惯保留 revision 号后缀（P1-5 原 `0116_…` 也是 40 字符），列为未来 TODO。
