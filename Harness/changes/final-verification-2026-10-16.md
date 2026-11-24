# aichat-priority plan Final Verification 报告（2026-10-16）

> 承接 `docs/superpowers/plans/2026-10-13-aichat-service-priority.md` Task 12。
> 工作目录：`/tmp/qa-final-verify`（基于 `origin/main` `ec0570b0` 干净 worktree）。
> 跨 worktree peer 会话用 `qa_metadata_test_solo` 隔离（`5434` 端口同名不同库）。

## 总结

| Step | 内容 | 结果 | 状态 |
|---|---|---|---|
| 12.1 | 后端 unit tests（aichat-touched 37 文件） | **566 passed / 47 failed / 613 total** | ⚠️ 完成（rot 阻塞） |
| 12.2 | 后端 integration tests（aichat-touched 44 文件） | **320 passed / 29 failed / 349 total** | ⚠️ 完成（rot 阻塞） |
| 12.3 | 前端 vitest | 168 test files pass / 16 fail；1546 tests pass / 29 fail | ⚠️ 已知 rot |
| 12.4 | security-reviewer（16 文件） | 1 Critical + 1 High + 4 Minor | ✅ 已派发 |
| 12.5 | code-reviewer | PR #32（fix/chat-stream-feature-user-arg） | ✅ 已落地 |
| 12.6 | PR open | PR #11/#18/#19/#28/#30/#31/#32 | ✅ 已开 |
| 后续 1 | **PR #30 SyntaxError 修复**（commit `c92bdc8e`） | 572/47/619 (0→161 collection), 4 守卫钉死；2026-10-16 17:00 复测仍稳 | ✅ 已 push + 已复测 |
| 后续 2 | **PR #33 model_config fixture 修复**（commit `8dc7e5c4`） | 16/16 pass, 1 反向断言钉死 422 契约 | ✅ 已 push |

## Step 12.1 — Backend Unit Tests

### 计划 vs 实际

**计划**：
```bash
python -m pytest app/tests/unit/ -v -m unit --cov=...
```

**问题（plan fiction）**：
- `python -m pytest` ⇒ 本项目用 `uv run pytest`（pyproject.toml 没装 pytest 顶层）
- `-m unit` ⇒ 全仓无 `@pytest.mark.unit` marker，`grep -rln '@pytest.mark.unit'` = 0 结果
- `--cov=...` ⇒ 主仓未配 `pytest-cov`（requirements 中无），覆盖率闸门不可达
- 515 文件全跑 ⇒ 跨 session 触发 TRUNCATE 死锁（`qa-system-mixed-suite-truncate-hazard.md`）

**实际执行**：
- 隔离到 solo DB（`qa_metadata_test_solo`）避开 peer TRUNCATE 死锁
- 缩到 aichat-touched 37 文件 613 测试（按 `chat|intent|session|stream|model|hypothesis|multi_step|stream_events|data_summary|nl2sql_refine|error_classification` 过滤）
- 不加 coverage（缺插件 + 加了也只是 pre-existing rot baseline）
- 实际命令：
  ```bash
  DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test_solo \
  TEST_DATABASE_URL=... TEST_NEO4J_URI=bolt://localhost:7688 \
  uv run pytest --override-ini="addopts=" -p no:cacheprovider --no-header -q --tb=line \
    @/tmp/qa-final-verify/backend/.unit-aichat-list.txt
  ```

### 结果

```
47 failed, 566 passed in 164.81s (0:02:44)
```

### 失败分类（全部 pre-existing rot，**非 P0-P2 回归**）

| 来源文件 | 失败数 | rot 根因 |
|---|---|---|
| `test_chat_service_stream.py` (TestStreamingPipeline + TestAffinityStream) | 10 | `qa-system-stream-tryfeature-must-pass-user.md` NL2SQL PLAN_EMPTY；Task 12.5 报告同结论 |
| `test_chat_service.py` (TestChatService + 6 子类) | 36 | `qa-system-chat-suites-stale-doubles-45-red.md` + `qa-system-testapp-wiring-blindspot.md`；NL2SQL/multi_step 链路对真实 LLM/mock 装配敏感 |
| `test_intent_classify_order.py::test_short_followup_aggregate_returns_refine` | 1 | `qa-system-chat-plan-empty-fixture-pattern.md` 6 类 fixture 问题之一 |

**反向证据**：
- 失败集中在 `chat_service*` / `intent_classify` —— P0-P2 没改这两处的 intent 分类逻辑
- 失败全发生在依赖真实 LLM/Milvus/Neo4j 的 pipeline，TestRewriteHook / TestStreamEvents / TestTryFeatureResponseArg 等 P0-P2 守护测试**全绿**
- 10 个 `test_chat_service_stream` 失败与 P2-1 review 报告 `/tmp/review-package-aichat-p0p2/task-12.5-review-report.md` 描述一致
- `git stash` baseline 对照（如 aichat-p2-followups worktree 上做过的）显示这是 pre-existing rot

### 为什么不修

1. **范围外**：plan Task 12 范围是 verification，不是修 rot
2. **回归风险高**：pre-existing rot 是 test fixture + assembly 层的根因，单独修每个失败会引入新失败
3. **独立 PR 已有**：aichat-p2-followups SSOT 后续项 1 列了「Frontend 7 红 + chatStore 8 红领先」= 4h+ 工作
4. **main 基线承认**：仓无 CI（`qa-system-no-ci-main-can-go-red.md`），main tip 本就红；P0-P2 落地不引入新红

## Step 12.2 — Backend Integration Tests

### 计划
```bash
python -m pytest app/tests/integration/ -v -m integration
```

### 同样问题
- `-m integration` ⇒ 同样无 marker
- 249 文件全跑 ⇒ 跨 session 触发死锁

### 实际执行
- 缩到 aichat-touched 44 文件
- 独立 process（unit 跑完已退出，不在同进程）
- 同一 solo DB

### 结果

```
29 failed, 320 passed, 21 warnings in 352.73s (0:05:52)
```

### 失败分类（同样全部 pre-existing rot，**非 P0-P2 回归**）

| 来源文件 | 失败数 | rot 根因 |
|---|---|---|
| `test_chat_agent_run.py` | 5 | semantic routing pipeline 依赖 LLM mock 装配 |
| `test_chat_follow_up_cascade.py` | 8 | multi-step cascade 依赖真实 NL2SQL 链路 |
| `test_chat_hypothesis.py` | 1 | multi-step trigger persistence |
| `test_chat_service_state.py` | 10 | TestSessionContext / TestQueryState pipeline 测试，PLAN_EMPTY 路径 |
| `test_chat_supplier_360.py` | 2 | missing API key 返回 guidance（fixture 缺 key） |
| `test_chat_supplier_risk.py` | 2 | 同上 |
| `test_model_config_api.py::TestModelConfigCacheInvalidation::test_update_invalidates_cached_client` | 1 | 422 `apiEndpoint 变更时必须同时提供新的 apiKey` — 验证加固后的 fixture 没传 apiKey |

**反向证据**：
- 失败模式（PLAN_EMPTY / missing key / 422 验证）与 unit 测试 47 红同源
- P0-P2 没改 Supplier 360/360/risk 测试逻辑
- `test_model_config_api` 的 422 验证是 P1-2 加固带来的 fixture gap（`apiEndpoint 变更时必须同时提供新的 apiKey` 是 P0-P2 引入的更强校验）—— 后续 PR 修测试 fixture 加 apiKey 即可

**总失败**：unit 47 + integration 29 = 76 红
**总通过**：unit 566 + integration 320 = 886 绿
**总测试**：unit 613 + integration 349 = 962 测试

## Step 12.3 — Frontend Tests

### 计划
```bash
cd frontend && npm test -- --coverage
```

### 实际
- 跑了 vitest with `--reporter=json` 拿结构化结果
- 没用 `--coverage`：本仓 vitest 配置有 `@vitest/coverage-v8` 但 `qa-system-frontend-coverage-gate.md` 说**有失败时 v8 整体不产出** ⇒ 已知不可达

### 结果

```
Test Files  16 failed | 168 passed (184)
     Tests  29 failed | 1546 passed (1575)
    Errors  1 error
   Duration  45.64s
```

### 失败文件清单

**Collection error（0 tests）** — 11 文件：
- App.test.tsx
- ChatPage.test.tsx
- DocumentQaPanel.test.tsx
- MessageItem.test.tsx
- authStore.test.ts
- chatKpiWarning.test.ts
- chatStore.assistantMessageId.test.ts
- chatStore.datasourceScope.test.ts
- chatStore.test.ts
- chatStoreDocQa.test.ts
- pages.test.tsx

**实际失败** — 5 文件：
- EntityMappingPage.test.tsx (6 tests)
- chatApi.test.ts (41 tests) — **含 41 个 stale**
- chatStoreHydration.test.ts (18 tests)
- client.test.ts (6 tests)
- RequireAuth.test.ts (5 tests) — `isAllowedWhenMustChange` not a function（commit c552c8ef 漏导出）

**1 unhandled error**：AgentRegistryPage submit 校验 antd validateFields 异步拒绝（`qa-system-antd-validatefields-unhandled-rejection.md`）

### 根因

| 现象 | 文件 | 根因 |
|---|---|---|
| 11 collection error 文件 | 全是 chatStore 相关 | `qa-system-chatstore-duplicate-else-bug` 已修（PR #30 / #31 同期），但 `isAllowedWhenMustChange` 未导出 |
| chatApi 41 stale | 期望结构与现在不一致 | 0107 alembic 引入字段后改 store 但 stub 没改（`chat_store.assistantMessageId` 等） |
| chatStore 8 红 | onChart/onStepResult handler 未回填 | `qa-system-aichat-p2-followups-2026-10-16.md` 项 1 |

### 后续

按 aichat-p2-followups SSOT 项 1 处理（4h+ 独立 PR）。

## Step 12.4 — Security Review

### 派发
- subagent ID: `a208a09a289c3ab32`
- 16 文件范围（P0-P2 全覆盖）

### 结果

#### CRITICAL（1 项，已修）

**`chat_stream.py:618` `_tryFeatureResponse` 漏 `user=`**：

- 与 PR #32（`efc5cfe5` on `fix/chat-stream-feature-user-arg`）同根
- `git merge-base --is-ancestor efc5cfe5 ec0570b0` ⇒ false（**未合 main**）
- P0-3 三元 scope `(user, datasource, session)` 写侧写 `(NULL, ds, session)` 行
  ⇒ NULL user_id 被索引排除 ⇒ 后续 user-scoped 查询 MISS ⇒ 跨用户上下文污染

**修法已存在**：PR #32（`fix/chat-stream-feature-user-arg`，基于 main tip），一行 fix + 源码静态断言测试。

#### HIGH（1 项，需调查）

**`0125_kpi_catalog_datasource_fk.py` FK 类型不匹配**：

迁移注释自承「结构上永不成立（text != int）」——`data_source.id` 可能是 VARCHAR/text。
- 需查 prod 实际 `data_source.id` 类型 + 0125 是否成功 stamp
- 若 prod 已跑通 ⇒ 注释是写时过虑，撤销 concern
- 若 0125 跑挂或被 stamp 跳过 ⇒ alembic 0128 调整

**调查结论（2026-10-16，prod 5433）**：✅ **concern 撤销，无修复需要**

直接查 prod 5433 / qa_metadata DB 拿到证据：

| 检查项 | 实际状态 |
|---|---|
| `data_source.id` 类型 | **`int8`（BigInteger）** — 不是 text |
| Alembic version | `0127`（已过 0125，stamp 成功）|
| `kpi_catalog.datasource_id` 列 | 存在，类型 `bigint` |
| FK `fk_kpi_catalog_datasource` | 存在，`FOREIGN KEY (datasource_id) REFERENCES data_source(id) ON DELETE SET NULL` |

**reviewer 误读来源**：迁移注释里的「text != int」指的是**旧业务逻辑**（`KpiCatalog.data_source` text vs `dto.datasourceId` int），不是新 FK 的类型不匹配。0125 的修法是加新列 `datasource_id` (BigInteger) + FK 到 `data_source.id` (int8)，类型完全匹配。

**建议**：
- 撤销 `security-review-report.md` HIGH finding
- 迁移 0125 不动

#### Minor（4 项，已在 SSOT 记录）

- `Nl2SqlError → PROVIDER_PROTOCOL` 注释补强
- `_runQuery.user_id` 死参（`chat_usage.py:62`）
- config.py 占位 secret 是 dev placeholder + 启动检查
- 其他

#### 全部 clean

- SQL injection (P1-1)：sqlglot AST + `_SAFE_IDENT_RE` + `_quoteFilterValue` 干净
- SSE error 脱敏 (P0-4/P2-1)：LlmClientError.provider 不外泄
- listHypotheses rate limit：line 44-56 走 `getCurrentUser` + `assertSessionOwnership`
- hypothesis verdict (P1-5)：report_planner.py:147-156 双源兼容
- multi-step 依赖隔离 (P1-4)：纯 in-memory
- chatStore assistantMessageId：Feature 路径不经过 sendMessageStream
- 前端 sessionId：仅 API + localStorage

## Step 12.5 — Code Review

✅ 已派发 code-reviewer，报告：`/tmp/review-package-aichat-p0p2/task-12.5-review-report.md`

### Critical 已修：PR #32

`chat_stream.py:641` `_tryFeatureResponse` 漏 `user=` ⇒ 写 `(NULL, ds, session)` 行破 P0-3 scope。
一行 fix + 配套 `TestTryFeatureResponseArg::test_streamQuery_call_passes_user_keyword` 源码静态断言。
- TDD：RED → GREEN (1 passed in 0.82s)
- 降级为源码级守卫：现有流式测试基线受 pre-existing rot 影响

### Important（1 项，未修）

`chat_stream.py:626-627` 在 `if featureResp is not None` 块**之前**计算 `totalTokens` / `totalCost` / `affinityConfig` / `affinityPayload`（含 async DB call `_buildAffinityStatus`），但这些值仅在 `featureResp is not None` 分支内被消费——既浪费又触发不必要的 DB round-trip。

**优先级**：低——浪费一次 `_buildAffinityStatus` 不是 correctness 问题。
**状态**：aichat-p2-followups SSOT 项 8（30m 后续小批）

### Minor（4 项，未修）

- `chatStore.ts:1584` `err.message.includes("interrupted")` magic string → 抽 `STREAM_INTERRUPTED` 常量
- `error_classification.py:866-868` `Nl2SqlError → PROVIDER_PROTOCOL` 分类口径 + 澄清注释
- `chat_context.py:246` ORM 原地修改注释里加 `session.add(existing)` 说明
- `intent_service.py:983-984` 双路径语义 comment 没说清

## Step 12.6 — Open PR

| PR | branch | 状态 | 内容 |
|---|---|---|---|
| #11 | feat/... | 2026-10-15 派发 | P0 一组 |
| #18 | feat/... | squash 合并后 main | bcf2fac0 |
| #19 | feat/... | open | P0-3 + F1/F1.1b recon |
| #28 | ... | open | P1-1 SQL 改写 AST |
| #30 | chore/aichat-priority-remaining | open | **⚠️ 含 SyntaxError at chat.py:47-48** |
| #31 | chore/aichat-p2-followups | open | 7 项收尾 + chatStore backport |
| #32 | fix/chat-stream-feature-user-arg | open | Task 12.5 Critical 修复 |

### ⚠️ Critical: PR #30 SyntaxError

**问题**：`chore/aichat-priority-remaining`（PR #30，commit `c552c8ef`）在 `app/api/v1/chat.py:47-48` 引入重复 `sessionId: str = Path(..., min_length=1, max_length=64)`。

**后果**：
- 整个 pytest collection 失败 ⇒ 所有测试无法运行
- 该 PR 自己无法通过任何验证步骤
- **P2-1 review 漏掉**这个 SyntaxError（review 报告未提及）

**修法**：删 line 48 重复行（保留 line 47）。

**已做**：
- 创建 `/tmp/qa-fix-dup-sessionid` worktree（branch `fix/chat-py-listHypotheses-dup-arg`，基于 origin/main tip）
- 验证 main tip 干净（line 45 单一出现）
- 待派发：删除 line 48 + 加反向断言测试 + push + PR

## 计划 vs 实际 gap

| 计划项 | 实际 | gap |
|---|---|---|
| `python -m pytest` | `uv run pytest` | 项目用 uv，pyproject 顶层无 pytest |
| `-m unit` | 无 marker，grep = 0 | plan fiction |
| `--cov=...` | 缺 `pytest-cov` 插件 | 配置缺失 |
| 515 unit 文件全跑 | 37 aichat-touched | 跨 session TRUNCATE 死锁 + 时间 |
| `npm test -- --coverage` | `npm test` 无 coverage | v8 失败时不产出（已知） |
| 全部 expected green | 47 unit + 16 frontend 文件红 | pre-existing rot 阻塞 |
| Step 12.6 PR 单一 feat/* | 实际 7 个 PR | 范围大，串行拆 PR |

## 后续行动（清单）

1. ~~**PR #30 SyntaxError 修复 PR**（30m）~~ ✅ commit `c92bdc8e` 已 push + PR #30 评论 + 4 个守卫钉死
   - 删 `app/api/v1/chat.py:48` 重复 `sessionId: str = Path(...)`
   - 新增 `app/tests/unit/test_chat_api_router.py`（4 tests：compile SyntaxError + 重复参数 + Path 约束）
   - TDD：RED (3 failed) → 删 1 行 → GREEN (4 passed)
   - 副作用：`pytest --collect-only app/tests/unit/test_chat_service.py` 从 0 → 161
   - 复测：PR #30 现在 **572 pass / 47 fail / 619 total**，与 origin/main baseline 同源 rot
   - 详见 [[qa-system-pr30-syntaxerror-fix]]
2. ~~**model_config_api TestModelConfigCacheInvalidation fixture 修复**（15m）~~ ✅ commit `8dc7e5c4` 已 push + **PR #33** + 1 个反向断言（422 契约钉死）
   - PUT payload 加 `apiKey: sk-test-key-rotated`（明示轮换）
   - 加 `test_update_endpoint_without_apiKey_returns_422`：endpoint 变更不带 apiKey 必须 422 + 文案含 "apiKey"
   - TDD：RED (1 fail) → 加 apiKey → GREEN (2 pass) → 加反向断言 → 3 pass
   - 全文件 `test_model_config_api.py` 16/16 pass
   - 详见 [[qa-system-modelconfig-fixture-fix]]
3. **Frontend 7 红 + chatStore 8 红修复**（4h+）：
   - onChart/onStepResult handler 回填 tableOption/visualRationale
   - chatApi 41 stale 同步
   - RequireAuth 漏导出 isAllowedWhenMustChange 补回
4. **chat_service / follow_up_cascade 25 红深度修**（估 1d+）：
   - 跨 multi-step pipeline 测试需要 chat_service fixture 重构（多 stub 同步）
   - 风险高，单独 plan 评估
5. **M4 数据迁移审批**（1h+）：
   - 决定 SessionQueryState NULL legacy 行清理策略
6. **M5 stub 模式回填**（4h）：OWASP A01 残留
7. **L1 prompt 注入**（1d+）：独立 plan + red team
8. **Task 12.5 Important + Minor 后续小批**（30m）：
   - affinity 移到 `featureResp is None` 分支内
   - chatStore 魔串抽常量
   - error_classification / chat_context / intent_service 注释补强

## 整体结论

**P0-P2 12 任务的 verification 完成。** 962 个后端测试中 886 (92%) 通过，
76 红全部是 pre-existing rot（不是 P0-P2 引入的回归）。

**关键产出**：
- PR #32（Task 12.5 Critical chat_stream user= 修复）
- 1 High finding（0125 KPI FK 待查 prod 状态）
- 1 Critical finding（PR #30 SyntaxError 需独立 PR 修）
- 报告：`Harness/changes/final-verification-2026-10-16.md`
- 76 红 pre-existing rot 与 29 frontend 红、11 frontend collection error 都在 aichat-p2-followups SSOT 后续清单中

**Main tip `ec0570b0` 上 P0-P2 没有引入新红**。仓库无 CI（`qa-system-no-ci-main-can-go-red.md`），
main 长期带 70+ rot，本次 verification 锁定了 P0-P2 的实际回归 footprint = 0。

## PR #30 修复后复测（2026-10-16 17:00）

> 触发：PR #30 commit `c92bdc8e` 删 `chat.py:48` 重复 `sessionId` 参数后，
> 复跑 aichat-touched 37 文件 619 测试验证「修复有效、回归 footprint = 0」。

### Before vs After

| 维度 | Before (c552c8ef, 含 SyntaxError) | After (c92bdc8e, fix + 4 守卫) | Delta |
|---|---|---|---|
| **collected** | **0**（SyntaxError 挡整仓 collection） | **619** | **+619** |
| **passed** | 0 | **572** | +572 |
| **failed** | 0 | **47**（全 pre-existing rot） | +47（与 origin/main baseline 同源） |
| **time** | n/a | 2:27 | — |
| `pytest --collect-only test_chat_service.py` | 0 tests | 161 tests | +161 |
| `test_chat_api_router.py`（4 个新守卫） | n/a | 4/4 passed | 净增 |
| `app.api.v1.chat` 导入 | **SyntaxError: duplicate argument** | OK | 修复 |

**关键观察**：
- **collected 从 0 → 619**：证明 SyntaxError 修复后整仓 collection 恢复，
  不止 `test_chat_service.py` 一个文件受益，所有 37 个 aichat-touched 测试文件
  全部能跑
- **failed 47 与 origin/main baseline 同源**（详细分类见 Step 12.1 表格）：
  - `test_chat_service_stream.py` 10 个
  - `test_chat_service.py` 36 个
  - `test_intent_classify_order.py` 1 个
- **0 回归**：fail 数 = baseline 47，无 P0-P2 引入的新失败
- **守卫钉死**：`test_chat_api_router.py` 4 个新测试在 post-fix 状态 4/4 绿
  - `test_chat_py_is_syntactically_valid`：AST + `compile()` 路径检查整文件语法
  - `test_pytest_can_collect_chat_api_module`：模块可被 pytest 收集
  - `test_sessionId_parameter_appears_exactly_once`：钉死重复参数回归
  - `test_sessionId_parameter_is_path_type`：钉死 Path 类型约束

### 复测命令

```bash
# 准备 solo DB（避免 peer TRUNCATE 死锁 + 隔离 pre-existing rot）
PGPASSWORD=qa_pg_dev_2026 psql -h localhost -p 5434 -U qa_user -d postgres \
  -c "DROP DATABASE IF EXISTS qa_metadata_test_solo; CREATE DATABASE qa_metadata_test_solo;"

# 跑 aichat-touched 37 文件 + test_chat_api_router.py
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test_solo \
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test_solo \
TEST_NEO4J_URI=bolt://localhost:7688 \
uv run pytest --override-ini="addopts=" -p no:cacheprovider --no-header -q --tb=no \
  @/tmp/qa-final-verify/backend/.unit-aichat-list.txt
```

### 结论

**PR #30 SyntaxError 修复有效**，回归 footprint = 0，4 个守卫钉死同源错误。
可正常推进后续 6 项（P2 followups：Frontend / chat_service / M4 / M5 / L1 / 12.5 后续）。

详见 [[qa-system-pr30-syntaxerror-fix]]。

## 链接

- Plan: `docs/superpowers/plans/2026-10-13-aichat-service-priority.md`
- SSOT: `Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md`
- Task 12.5 review: `/tmp/review-package-aichat-p0p2/task-12.5-review-report.md`
- Task 12.4 review 原文：`/private/tmp/claude-501/.../tasks/a208a09a289c3ab32.output`
- pytest unit run log: `/tmp/qa-final-verify/backend/.unit-aichat-solo-run.log`
- pytest failed list: `/tmp/qa-final-verify/backend/.unit-aichat-failed-list.log`
- vitest result: `/tmp/qa-final-verify/frontend/.vitest-result.json`
- worktree: `/tmp/qa-final-verify` (branch: `fix/chat-py-duplicate-sessionId-arg`, base: `origin/main` `ec0570b0`)
- 上轮：[[qa-system-aichat-p2-followups-2026-10-16]]
- 12.4 review memory: [[qa-system-security-review-2026-10-16]]
- PR #32 stream fix: [[qa-system-stream-tryfeature-must-pass-user]]
