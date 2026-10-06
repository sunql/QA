# 变更：feat-multi-step-persist

- **日期**：2026-10-06
- **作者**：Claude / 启琳
- **Phase**：Chat 多步编排 — 落库 / 自动重试 / 动态压缩 / 手动续跑
- **状态**：in-review（Task 1-9 已提交分支 `feat/multi-step-persist`；**未合并 main**）
- **关联变更**：
  - [feat-multi-step-nl2sql](../feat-multi-step-nl2sql/summary.md)（predecessor：多步拆步本体）
  - [feat-qwen-multistep-uplift](../feat-qwen-multistep-uplift/summary.md)（同一批多步链路上的邻近变更）
- **迁移版本**：0114_multi_step_persist, 0115_multi_step_run_session_id_text
- **MEMORY**：[qa-system-resetstepsfrom-data-compressed.md](../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-resetstepsfrom-data-compressed.md)

> **交付边界说明**：本变更落地了 spec 的**后端全链路**（落库 / 重试 / 压缩 / 续跑 API / 清理函数）
> 与 spec §8.2 的**聊天面板**部分（失败步续跑按钮 + 压缩徽章）。spec §8.1（session 列表徽章）
> 与 §8.3（续跑弹窗）**未做** —— 详见文末「遗留项」第 6/7 条，记录**不代表 UI 已完工**。
> 保留期清理函数**没有调度入口**，30/7 天当前不会自动执行（遗留项 13）。

---

## 1. 需求

多步（拆步）问答此前是「一次性」的：任一步失败或上下文超限，用户只能把整个问题重问一遍，
且跑过什么、为什么失败、烧了多少 token 全无留痕。本变更把它变成**可审计、可续跑、可回收**的执行单元。

验收标准（用户视角）：

- **可续跑**：一次多步跑中途失败后，用户在前端点该失败步的「续跑」按钮，即从该步续跑，
  **不必重跑已成功的步**；重复提交同一续跑请求不会重跑（幂等）。
- **可重试**：网络/限流类**瞬态**失败自动重试（3 次封顶，1s → 2s 退避）；配置/数据类**永久**
  失败不空转、直接转人工。
- **可压缩**：上下文将超模型窗口时自动压缩**更早**的步，保证后续步还能跑；被压缩的步在
  前端显示徽章。
- **可审计**：每一步的 SQL / 数据 / 用量 / 错误都落库，run 与 step 两级状态机可查。
- **可回收**：成功 run 保留 30 天、失败 run 保留 7 天（非终态 run 不删）。
- **可开关**：`MULTI_STEP_PERSIST_ENABLED=false` 时完整退回旧行为（不落库、不重试、不压缩）。
- 后端覆盖率 ≥ 80%（[[Harness/rules/测试规范.md|测试规范]]：真实 PostgreSQL + 完整 API 链路）。

## 2. 设计评审

### 2.1 session_id 的形态：UUID+FK vs 自由字符串

| 候选 | 内容 | 取舍 |
|---|---|---|
| A（0114 原设计） | `session_id UUID NOT NULL` + `FK → research_session.id ON DELETE CASCADE` | 引用完整性好；但**多步落库的唯一消费方是 chat 链路**，chat 会话 id 是 `chat-<uuid>` / `docqa-<uuid>` 这类自由字符串，且 chat **从不创建** `ResearchSession` ⇒ 该外键在唯一消费方里**永远悬空**，写入 `"s1"` 直接抛 `DataError: invalid UUID` |
| B（0115 采纳） | `session_id String(64)`，**无 FK** | 与既有约定一致（`session_message.session_id` / `session_query_state.session_id` 同为 `String(64)`）；代价是失去 DB 层引用完整性，但那条引用本来就指不到行 |

**最终决定：B**（方案 A 的 FK 是「指向永远不存在的行」的假约束）。研究链路有自己的一套
（`ResearchAgentService` + `ResearchSqlRunner`），不写这两张表；将来若也落库，`str(uuid)` 36 字符同样放得下。

### 2.2 续跑语义：完整 plan 重放 vs 最小正确版

| 候选 | 内容 | 取舍 |
|---|---|---|
| A（spec 原案） | 续跑时把前序步的 `data` **拼回 prompt**、按 run 原 plan 重放（spec §7.2 step 5/6） | 理论上更「正确」；但需要 step 间数据依赖成立 |
| B（采纳，2026-10-05 裁决） | 从 `current_step_idx` 起跑、跳过更早的步（仍计入 `completed`），**不**回灌前序 `data`、**不**按原 plan 重放 | 依据：2026-09-28 真机诊断已**证伪**「多步之间存在步间数据依赖」——各子问题独立查询、最后在报告层聚合，故回灌不产生正确性收益，却要在 `adoptRunForResume` 里做复杂的形状判定 |

**最终决定：B**（「最小正确版」）。同时 `adoptRunForResume` 把 `sql` / `sql_hash` **刻意清成 `None`**，
避免将来「sql_hash 命中即复用」的特性拿陈旧 hash 误命中。

### 2.3 压缩落点：丢弃 vs 摘要共存

| 候选 | 内容 | 取舍 |
|---|---|---|
| A | 截断/删除更早步的 `data` | 简单，但丢失原始数据、且续跑/回放再也拿不回 |
| B（方案③，采纳） | `data` **永不删除**，摘要写进独立列 `data_compressed`（JSONB） | 原始数据可追溯；代价是行更大、且必须在续跑重置时显式清 `data_compressed`（见 §5） |

**最终决定：B**。

## 3. 数据模型变更

新增两张表（**新增，无修改既有表**）。迁移：`0114_multi_step_persist`（建表）+ `0115_multi_step_run_session_id_text`
（改 `session_id` 形态并去 FK）。

| 表 | 关键列 | 约束 / 索引 |
|---|---|---|
| `multi_step_run` | `id` PK(UUID)、`session_id` **String(64) 无 FK**、`question`、`model_id`、`datasource_id`、`status` String(20) 默认 `running`、`total_steps` / `completed_steps` / `current_step_idx` / `compressed_count` / `resume_count` / `version`、`idempotency_keys` JSONB 默认 `[]`、`error_summary`、`started_at` / `updated_at` / `finished_at` | `ix_multi_step_run_session_id`、`ix_multi_step_run_session_updated (session_id, updated_at)`、`ix_multi_step_run_status_updated (status, updated_at)` |
| `multi_step_step` | `id` PK(UUID)、`run_id` FK → `multi_step_run.id` **ON DELETE CASCADE**、`step_index`、`status` String(20) 默认 `pending`、`sub_question`、`sql` / `sql_hash`、`data` JSONB、`data_compressed` JSONB、`chart_option` JSONB、`model_used`、`tokens_used`、`cost` Numeric(12,6)、`attempt_count`、`last_error` / `last_error_kind`、时间戳 | `uq_multi_step_step_run_index (run_id, step_index)` 唯一、`ix_multi_step_step_run_id`、`ix_multi_step_step_status_updated (status, updated_at)` |

- **无 CheckConstraint**：`status` 两列是 `String(20)`，取值（run：`running`/`succeeded`/`failed`/`partially_failed`；step：`pending`/`running`/`succeeded`/`failed`/`skipped`/`compressed`）由应用层常量约束，DB 不拦。
- 迁移文件名长度：`0114_multi_step_persist.py` = 25 字符 ✓；`0115_multi_step_run_session_id_text.py` = 38 字符，**超过**模板标称的 ≤ 32（既有 114 个迁移里 28 个同样超，属仓内既有约定偏差，本次未改名）。
- 迁移**只跑一次**（`drop_constraint` / `create_table` 均无 `IF EXISTS`，重复执行会报错）；升级必须按 `DATABASE_URL` 定向，勿裸跑（[[qa-system-alembic-targets-prod]]）。
- 0115 的 downgrade **会显式失败**而不是静默截断：若 `session_id` 含非 UUID 值则 `RAISE EXCEPTION`。

## 4. 接口契约变更

### 新增端点

```
POST /api/v1/chat/multi-step/{runId}/resume
  Auth : Authorization: Bearer <jwt>（Depends(getCurrentUser)）+ session 归属校验
  Body : ResumeRequest { from_step_index?: int, model_override?: int, compress_again?: bool }
  Header: Idempotency-Key: <uuid>
  → SSE 流，事件序列与 POST /api/v1/chat/stream 一致
```

| 情形 | 状态码 |
|---|---|
| run 不存在 / 归属校验失败 / 查完与被删之间竞态 | 404 |
| `status` 不可续跑、`from_step_index` 越界或其前序有未完成步、重复 `Idempotency-Key` | 409 |
| run 没有数据源快照（`datasource_id is None`） | 409 |

### 修改的 DTO / 事件

- `ChatRequest` 新增 `resumeRunId: uuid.UUID | None`（类型钉成 UUID ⇒ 非 UUID 形态在边界被 Pydantic 拦成 422，另有领域层校验兜底）。
- 新 SSE 事件 `step_compressed`（`{stepIndex, originalRows, compressedRows}`）。
- `multi_step_plan` 事件新增 `runId`（单步路径不发 ⇒ 前端据此不渲染续跑按钮）。

### 状态机

- run：`running → succeeded | failed | partially_failed`；续跑把 `failed`/`partially_failed` 重置回 `running`，`current_step_idx = 起始步`、`finished_at = None`、`resume_count++`、`version++`。
- step：`pending → running → succeeded | failed | compressed | skipped`；续跑把 `>= 起点` 的步重置回 `pending` 并清 `last_error*` / `attempt_count` / `finished_at` / `data_compressed` / `sql` / `sql_hash`。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/domain/multi_step_models.py` | 新建：`MultiStepRun` / `MultiStepStep` ORM 模型 + 状态常量 |
| `backend/alembic/versions/0114_multi_step_persist.py` | 新建：建两张表 + 索引 |
| `backend/alembic/versions/0115_multi_step_run_session_id_text.py` | 新建：`session_id` UUID+FK → `String(64)` 无 FK |
| `backend/app/services/multi_step_persistence.py` | 新建：`createRun` / `createSteps` / `finishStep` / `recordStepError` / `updateRun` / `loadRun` / `loadSteps` / `resetStepsFrom` / `appendIdempotencyKey` / `adoptRunForResume` |
| `backend/app/services/multi_step_persist_hooks.py` | 新建：`MultiStepPersistMixin`（执行链路落库钩子）+ `runStatusFor` |
| `backend/app/services/multi_step_compressor.py` | 新建：`_shouldCompress`（阈值 0.7）+ 压缩实现（+ Decimal 归一为 JSON 原生值） |
| `backend/app/services/multi_step_retry.py` | 新建：`classifyStepError`（沿 `__cause__` 链）+ 重试曲线（`MAX_ATTEMPTS=3`、`TRANSIENT_WAITS=(1,2)`） |
| `backend/app/services/multi_step_resume.py` | 新建：`prepareResume` + `ResumeConflict` / `ResumeNotAllowed` |
| `backend/app/jobs/cleanup_multi_step_runs.py` | 新建：`cleanupMultiStepRuns` 保留期清理（**无调度入口**，见遗留项 13） |
| `backend/app/api/v1/chat.py` | 改：`POST /multi-step/{runId}/resume` + `_sealAbandonedResume`（挂 `StreamingResponse(background=...)`） |
| `backend/app/services/chat_multistep.py` | 改：在 `_executeDataStep` 前后插入落库 / 压缩 / 重试 |
| `backend/app/services/chat_service.py` | 改：`MultiStepPersistMixin` 并入 `ChatService` MRO |
| `backend/app/services/chat_stream.py` | 改：下发 `runId` 与 `step_compressed` 事件 |
| `backend/app/domain/schemas.py` | 改：`ChatRequest.resumeRunId` |
| `backend/app/utils/json_safe.py` | 新建：Decimal / 非 JSON 原生值归一 |
| `frontend/src/components/chat/ResumeRunButton.tsx` | 新建：失败步续跑按钮 |
| `frontend/src/components/chat/MultiStepPlanCard.tsx` | 改：续跑按钮挂载 + 压缩徽章 |
| `frontend/src/stores/chatStore.ts` | 改：`resumeRun`（定向写入目标消息）+ `onStepCompressed` |
| `frontend/src/api/chat.ts` | 改：`resumeMultiStepRun`（带 `Idempotency-Key`）+ `step_compressed` 分发 |
| `frontend/src/types/chat.ts` / `i18n/{zh-CN,en-US}.ts` | 改：类型 + 文案 |

关键算法 / 依赖点：

- **落库钩子**：`MultiStepPersistMixin` 经 MRO 并入 `ChatService`；开关 `settings.multiStepPersistEnabled`（env `MULTI_STEP_PERSIST_ENABLED`，默认 `True`）关闭时**仍做错误分类**，只是不落库。
- **起点唯一事实来源 = DB**：`prepareResume` 把起点写进 `run.current_step_idx`，执行侧 `adoptRunForResume` 从那里读；**只在本次计划形状与原 run 逐字一致时**沿用，形状变了归零整跑，且越界指针（写侧 `_closeRun` 落的 `len(plan.steps)` 哨兵）被读侧钳制。
- **`resetStepsFrom` 不清 `data_compressed`** ⇒ `prepareResume` 里的显式清空是承重的（少了它会留下「`pending` 但 `data_compressed` 非空」的非法态，压缩钩子见非空即跳过 ⇒ 该步此后永不压缩）。
- **依赖注入点**：`Depends(getCurrentUser)`（鉴权）、`Depends(getDb)`（会话）、`assertSessionOwnership`（归属）。

## 6. 测试

全部**真实 PostgreSQL（`qa_metadata_test`，端口 5434）+ 完整 API 链路**，禁止 sqlite 内存库。
按 [[qa-system-mixed-suite-truncate-hazard]] 的约束，unit 层与 integration 层**分进程跑**（integration 先、unit 后）。

后端（`cd backend`，两环境变量必给：`TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test`、`TEST_NEO4J_URI=bolt://localhost:7688`）：

```
uv run pytest app/tests/integration/test_multi_step_cleanup.py \
  app/tests/integration/test_multi_step_persist_models.py \
  app/tests/integration/test_multi_step_persist_repo.py \
  app/tests/integration/test_multi_step_persist_wiring.py \
  app/tests/integration/test_multi_step_resume_api.py -q
```

实测输出：`25 passed, 20 warnings in 52.72s`（另加 `test_chat_multi_step.py` 38 例 ⇒ 集成合计 `63 passed`）。

```
uv run pytest app/tests/unit/test_multi_step_compressor.py \
  app/tests/unit/test_multi_step_persist_hooks.py \
  app/tests/unit/test_multi_step_retry.py \
  app/tests/unit/test_chat_multistep_rewrite_hook.py -q
```

实测输出：`43 passed in 10.74s`。

| 层 | 文件 | 用例数 | 关键场景 |
|---|---|---|---|
| 单测 | `app/tests/unit/test_multi_step_compressor.py` | 13 | 阈值 0.7 边界、Decimal 归一、摘要保留 |
| 单测 | `app/tests/unit/test_multi_step_retry.py` | 21 | 沿 `__cause__` 链分类、退避 `(1,2)`、`MAX_ATTEMPTS=3` 独立于退避长度 |
| 单测 | `app/tests/unit/test_multi_step_persist_hooks.py` | 6 | `runStatusFor` 四态、开关关闭仍分类不落库 |
| 单测 | `app/tests/unit/test_chat_multistep_rewrite_hook.py` | 3 | sub-question 改写 hook 不改派模型 |
| 集成 | `app/tests/integration/test_multi_step_persist_models.py` | 2 | ORM 建读回、`session_id` 自由字符串 |
| 集成 | `app/tests/integration/test_multi_step_persist_repo.py` | 5 | 落库 / `resetStepsFrom` / 幂等 key |
| 集成 | `app/tests/integration/test_multi_step_persist_wiring.py` | 11 | 端到端落库、重试耗尽标 `failed`、`adoptRunForResume` 三分支 + 越界钳制、开关关闭零落库 |
| 集成 | `app/tests/integration/test_multi_step_resume_api.py` | 5 | 续跑 API 全链路、404/409 分支 |
| 集成 | `app/tests/integration/test_multi_step_cleanup.py` | 2 | 30/7 天保留期、非终态不删 |
| 集成 | `app/tests/integration/test_chat_multi_step.py` | 38 | 多步链路回归（本特性亦覆盖） |

**覆盖率**（integration 层 + unit 层 `--cov-append` 合并，命令同上加 `--cov=app.services.multi_step_* --cov=app.jobs.cleanup_multi_step_runs --cov=app.domain.multi_step_models`）：

```
TOTAL  464 stmts  13 miss  97%
Required test coverage of 80.0% reached. Total coverage: 97.20%
```

逐模块：`multi_step_models.py` 100% · `multi_step_retry.py` 100% · `multi_step_persistence.py` 98% · `multi_step_compressor.py` 97% · `multi_step_persist_hooks.py` 95% · `cleanup_multi_step_runs.py` 95% · `multi_step_resume.py` 93%。

前端（`cd frontend`）：

```
npx vitest run src/tests/chatStore.test.ts src/tests/chatApi.test.ts src/tests/MultiStepPlanCard.test.tsx
```

实测输出：`Test Files 1 failed | 2 passed (3)` / `Tests 1 failed | 88 passed (89)`。

- 唯一失败 `chatStore > 流式 plan 事件回填 ReAct 查询计划（Phase E）` 是**既有失败、与本变更无关**：
  在 `main` 基线上同一用例同样失败（基线 `1 failed | 39 passed`，本分支 `1 failed | 40 passed`，
  失败集合不变、差额恰为本分支新增的 1 条 `chatStore resumeRun 定向写入` 用例）。
- 本变更新增前端用例：`chatStore.test.ts` 的 `resumeRun 定向写入`（1）；`chatApi.test.ts` 的
  「续跑与压缩（Task 9）」6 例（`runId` 传递、单步路径 `undefined`、`step_compressed` 分发与非法负载、
  `resumeMultiStepRun` 的 `Idempotency-Key` 与 camelCase body、runId URL 编码）；`MultiStepPlanCard.test.tsx`
  的「续跑与压缩徽章」4 例（失败步渲染按钮、无 `runId` 不渲染、压缩徽章行数、流式中按钮禁用）。

## 7. 安全审查

**未单独触发 `security-reviewer` 代理**（本变更虽命中模板列的触发条件 —— 新增认证端点 + 用户输入 + 复用 SQL Guard ——
但 Tasks 1-9 的执行与评审均未排这一步；属实为**缺口**，建议合并前补一次 `security-reviewer` 复核）。
以下为按代码实况逐项自查的结论，**非** security-reviewer 的裁定：

- **认证**：`/resume` 端点强制 `Depends(getCurrentUser)`，无「只读就免鉴权」的绕过（对照 [[qa-system-router-auth-mandatory]] 的教训）。
- **越权 / 侧信道**：`assertSessionOwnership(session, str(run.session_id), user)` 在**读取 run 之后、任何写之前**执行；run 不存在与无权限**同返 404**（不用 403 区分，避免「存在 vs 无权限」侧信道）。
- **输入校验**：`from_step_index` / `model_override` / `compress_again` 均为受类型约束的 Pydantic 字段；`resumeRunId` 钉成 `uuid.UUID`（非法形态在边界 → 422，不漏到服务层抛 500）。
- **SQL 安全**：本变更新增的库操作全部走 SQLAlchemy 参数化（无字符串拼接）；业务查询仍经既有 SQL Guard（只读 SELECT）—— 本变更**未放宽**任何 SQL 校验。
- **注入 / 路径**：前端 `resumeMultiStepRun` 对 `runId` 做 `encodeURIComponent`（有一条专门用例「防路径注入」）。
- **密钥**：无新增硬编码密钥；无新增外部凭据。

结论：**未发现 CRITICAL / HIGH**（基于上述自查）；但**无 security-reviewer 的独立复核记录**，该缺口已如实登记。

## 8. 部署验证

- **端点冒烟（实测）**：对**当前运行的 `qa-backend` 容器**（`http://localhost:8000`）跑
  `curl -s http://localhost:8000/openapi.json` ⇒ 共 **245** 条路径，其中**没有任何** `multi-step` 路径。
  根因：该容器起于 `2026-10-05T12:49Z`（早于本特性的提交），跑的是**旧镜像**。
  ⇒ **未做**基于本分支新镜像的冒烟。理由：本计划交付的是代码 + 文档，**不含部署动作**；
  且本仓已记「前端必须 compose build 才生效」，重跑部署须与前端同批，不在本任务范围。
  **本项如实登记为未验证。**
- **真实数据验证脚本**：**未做**。`Harness/rules/变更记录强制规范.md` §一 第 5 条要求涉及真实 SQL/DB 改动的变更
  提供一个幂等的 `scripts/<feature>_realdata.py`，但**全仓 `scripts/` 的 10 个文件里不存在任何 `*realdata*` 实例**
  —— 该约定在实践中是名义性的，本计划也未排这个产物（遗留项 15）。**不新造脚本凑齐**（计划外产物）；
  是否补此约定留给收尾时人类定夺。
- **迁移**：`0114` / `0115` 在本会话中**未对任何库执行**（测试库由 `app/tests/_pg_support.py` 按 `TEST_DATABASE_URL`
  建 schema，未经手工 alembic 升级）；生产库 `qa_metadata`（5433）**未连接、未迁移**。

## 9. 关联

- 设计稿：`docs/superpowers/specs/2026-10-05-multi-step-persist.md`（spec）+ `docs/superpowers/plans/2026-10-05-multi-step-persist.md`（plan）
- Wiki：`Harness/wiki/chat_multi_step_persistence.md`（本变更新建的架构 wiki）
- Wiki（既有，被本变更牵连）：`Harness/wiki/data-model.md` · `Harness/wiki/chat-service-capabilities.md` · `Harness/wiki/nl2sql-engine.md`
- Rules：`Harness/rules/变更记录强制规范.md` · `Harness/rules/测试规范.md` · `Harness/rules/数据库环境使用规范.md` · `Harness/rules/部署与访问规范.md`
- Memory：`~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-resetstepsfrom-data-compressed.md`（本变更的 MEMORY 索引）
- 关联变更（predecessor / successor）：`../feat-multi-step-nl2sql/summary.md` · `../feat-qwen-multistep-uplift/summary.md`

---

## 遗留项（本计划不做，逐条登记）

1. **续跑不做前序结果回灌（spec §7.2 step 5「plan 重放」未做）**：2026-10-05 裁决为「最小正确版」——续跑从 `current_step_idx` 起跑，跳过更早的步（但仍计入 `completed`），**不**把前序步的 `data` 拼回 prompt、**不**按 run 原 plan 重放规划。依据是 2026-09-28 真机诊断已证伪「多步之间存在步间数据依赖」（见 [[qa-system-multistep-no-data-dependency]]）：各子问题独立查询、最后在报告层聚合，故回灌不产生正确性收益。真要做时须同批重审 `adoptRunForResume` 的形状判定。
2. **`sql_hash` 命中缓存跳过 LLM（spec §7.2 step 6 未做）**：字段已落库，`adoptRunForResume` 还**刻意**把 `sql` / `sql_hash` 清成 `None`，避免未来这个特性拿陈旧 hash 误命中。先保证正确性，再优化 token。
3. **压缩后仍 > 95% → 步转 `skipped`**：spec §4.1 定义了该状态，但触发点依赖 `_planAndGenerateSql` 的实际报错形态；先按 permanent 处理，观察线上日志后再实现。
4. **`compress_again` 参数**：`ResumeRequest` 已接收但当前实现忽略（压缩只按阈值自动触发）。
5. **前端 `from_step_index` 选择弹窗**：Task 9 只做「默认从首个失败步续跑」；下拉选步延后。
6. **spec §8.1（session 列表「未完成」徽章）未做**：需要 session 列表接口回传 run 状态，现接口不返回，改动面超出本计划。
7. **spec §8.3（续跑弹窗：`from_step_index` 下拉 + `compressAgain` 复选框）未做**：Task 9 直接把 `fromStepIndex` 定为失败步号、`compressAgain` 固定 false。API 两端都已支持这两个参数（Task 7 的 `ResumeRequest`），只是前端没有入口。
8. **非流式渲染下的续跑入口缺失**：`runId` 与「已压缩」信息都只走 SSE（Task 6 增量）。非流式 `/chat` 的 `steps` 负载不带这两项，故非流式回答里既没有续跑按钮也没有压缩徽章。补齐需要改 `_executeMultiStep` 的读模型构造 + `ChatResponse.steps` 的元素类型。
9. **压缩徽章的「展开原始数据」未做**：spec §8.2 要求 `[展开原始数据]` 链到 `multi_step_step.data`。需要新增 `GET /chat/multi-step/{runId}/steps/{stepIndex}/data`（含归属校验 + 分页），本计划没有这个端点，故徽章目前只是提示。
10. **SSE 中断后前端自动重连续跑**：依赖前端的 SSE 封装改造，单独排期。后端一侧已就绪：Task 7 的 `_sealAbandonedResume` 挂在 `StreamingResponse(background=...)` 上封口（**不是**生成器 `finally` —— 断连主情形不触发），断连不会留下 `running` 僵尸。
11. **超大 data（> 5MB）转对象存储**：spec §14 提到超限走 minio，但当前 `data` 一律进 JSONB。先观察真实 `pg_column_size(multi_step_step.data)` 分布，确认有超限样本后再实现，避免过早引入存储依赖。
12. **并发续跑乐观锁的落库侧强约束**：当前靠 `run.version++` 的自增语义 + 状态校验挡住大部分并发，但**没有** `SELECT … FOR UPDATE`，极端并发下两个请求都可能通过校验。若线上出现双跑，再补行级锁。
13. **清理任务没有调度入口，保留期策略当前不会执行**（2026-10-06 Task 8 计划预检发现）：spec §10.4（`docs/superpowers/specs/2026-10-05-multi-step-persist.md:321`）写「定期清理由新 **cron 任务**执行」，§13 文件清单（同文件 `:358`）把该文件描述为「新建：**cron 清理脚本**」；但本计划只交付一个可导入的函数 —— 无 `__main__`、无 `scripts/cron_*.sh` 包装、无 crontab / launchd 注册。全仓 grep（排除 `.git`）实测：`app.jobs` 的**生产代码**引用为零，唯一引用方是它自己的集成测试 `backend/app/tests/integration/test_multi_step_cleanup.py:10`（`from app.jobs.cleanup_multi_step_runs import cleanupMultiStepRuns`），另两处提及在 spec 与本计划的待建清单里，都是「打算建」而非「谁调用」。故没有任何生产路径会调用它。更关键的是本机 cron 已确认静默失效（`/etc/crontab` 缺失、launchd 契约断裂，见 [[qa-system-cron-silently-broken]]），即便补上注册也不会触发。**结论：30/7 天只是写在代码里的两个常量，线上不会自动回收。** 本计划的处置与 `scripts/backup_pg.sh` 一致 —— 以可手动调用的形态交付 + 在此登记缺口，不粉饰。真要落地调度时，本仓既有两种形态可参照：`scripts/install_pg_backup_cron.sh` 式的外部 cron 安装器，或 agent scheduler 式的「PG 表 + 独立 worker 轮询」（后者不依赖宿主 cron，是当前唯一可靠的一条）。
14. **spec §10.4 没有给「非终态 run」定保留规则**（同上预检发现）：spec §11 失败场景表自己写明「用户主动 kill 浏览器 ⇒ `run.status=running` 残留」，而 §10.4 只为 `succeeded`(30d) / `failed`+`partially_failed`(7d) 定规则 —— Task 8 用 `_TERMINAL` 过滤正确地**不删**这些行，于是它们无限累积。注意第 10 条只覆盖**续跑**入口的 `_sealAbandonedResume`；**全新执行**被 kill 后留下的 `running` 行无人封口，两者不矛盾（已核对：两个执行器里只有 4 处 `session.commit()`，全部紧邻带 `finished=True` 的 `_closeRun`；循环中途 `updateRun(status=FAILED)` 只有 `flush()`，故要么与收尾同事务落盘、要么随会话回滚成 `running`）。补齐需先定「多久算死」的阈值，属策略决策，不在本计划范围。
15. **`变更记录强制规范` §一 第 5 条（`scripts/<feature>_realdata.py`）未做**（2026-10-06 预检发现）：该条规定「涉及真实 SQL/DB 改动时」须有一个幂等的真实数据验证脚本，属**部署阻塞项**。但全仓 `scripts/` 的 10 个文件里**没有任何 `*realdata*` 实例** —— 这条约定在实践中是名义性的，本计划也没有排这个产物。故 §8 按实况写「未做 + 理由」，**不新造脚本凑齐**（计划外产物）。要不要补，收尾时由人类定夺。
