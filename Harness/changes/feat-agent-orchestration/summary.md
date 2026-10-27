# 变更：feat-agent-orchestration

- **日期**：2026-10-28
- **作者**：Claude
- **Phase**：Phase 6.6 通用多工具 Agent 编排
- **状态**：in-review
- **关联变更**：无前序（本特性自成一条线；底座沿用 [[../../wiki/agent-runtime]] 记录的既有 Agent 运行时）
- **迁移版本**：`0135_agent_orchestration`（revision `0135`，down_revision `0134`）
- **MEMORY**：`qa-system-general-agent-orchestration.md`

---

## 1. 需求

**背景**：平台上的 Agent 一直是「**一个 Agent 绑一个工具**」（`agent_definition.tool_name`
FK-by-name）。一个 Agent 想先查 360° 视图、再做风险评估、再做图推理，只能拆成三个
Agent 分别跑，或者塞进 chat 让 L4 自由发挥——后者不受预算/只读/ACL 的硬约束管，
也无法回放轨迹。

**目标**：让一个 Agent 能**按顺序调用多个只读工具**，并且每一步都留下可回放的
轨迹、可核算的 token/成本、可审计的 ACL 判定。

**做完是什么样（用户视角）**：

1. 管理员在「编排配置」页给某个 Agent 写一段编排指令、勾几个**只读**工具、
   设预算上限；保存后配置是**停用**的，显式点「启用」才生效。
2. 业务用户在「编排任务」页发起一次任务，看到它从 `QUEUED` → `RUNNING` →
   `SUCCEEDED`，右侧时间线逐步列出「规划了什么、调了哪个工具、观察到什么」。
3. 工具调用受**只读白名单**（7 个 handler）+ **分层 ACL**（deny-by-default）约束；
   越权不是报错，而是**暂停**并等管理员补授权。
4. 预算（迭代数 / 工具调用数 / 成本 / 超时）任一触顶 ⇒ 暂停，可带增量预算恢复。

**验收标准**：

- 既有 Agent 的行为**零变化**（设计稿 §2 的非回归底线）；
- 新能力**默认关闭**（`ENABLE_AGENT_ORCHESTRATION=false`），关闭时不注册路由、不起 Worker；
- 只允许 7 个只读 handler；
- 新代码覆盖率 ≥ 80%；
- 真实 PostgreSQL 上跑通「建配置 → 建 run → 快照只含只读工具 → 清理」全链路。

**非目标**（本特性明确不做）：不接 NL2SQL 写工具、不接外部 HTTP 工具、
不做并行工具调用、不做多租户编排、不改 `agent_definition` 的既有字段。

---

## 2. 设计评审

### 2.1 候选方案对比

| 维度 | 方案 A：扩展 L4 Agent Loop | 方案 B：**独立第三条执行路径**（采纳） | 方案 C：配置驱动的 L4 变体 |
|---|---|---|---|
| 形态 | 在 `run_agent_loop` 里加「多工具」分支 | 新建 `agent_orchestration_runner` + **独立 Worker 进程** + 5 张新表 | 仍走 `run_agent_loop`，用 `profile` 表下发参数 |
| 既有 L4 回归风险 | **高**——L4 是 chat 的降级路径，改它会牵动 chat / NL2SQL / 计量 | **零**——物理分流，L4 一行未改 | **高**——同 A，只是把参数外置 |
| 状态/轨迹 | 复用 L4 的内存态，**无法回放**（L4 的 step 不落库） | 独立 `run` / `step` / `usage` 三张表，**可回放、可核算** | 仍不可回放 |
| 执行进程 | 复用 API 进程（长任务占住请求/事件循环） | 独立 Worker（`profiles` 门控，不发布端口，不被 `depends_on` 拉起） | 复用 API 进程 |
| 只读/预算/ACL 边界 | 与 NL2SQL 工具混在同一套判定里，难证明「只有 7 个 handler」 | **服务端唯一判定点** `assertOrchestrationToolValid`（enabled + BUILTIN + 白名单 + schema） | 与 A 同 |
| 失败恢复 | L4 的「异常收敛为正常返回」语义会掩盖编排的暂停/恢复需求 | 独立状态机 `QUEUED/RUNNING/PAUSED/SUCCEEDED/FAILED/CANCELLED` + 租约 + `resume` | 无暂停概念 |
| 交付成本 | 低 | **高**（5 张表 + Worker + 2 套前端 + 9 个端点） | 中 |
| 结论 | ❌ 违反非回归底线 | ✅ **采纳** | ❌ 继承了 A 的全部缺点，又没拿到 B 的可回放性 |

**最终决定**：**方案 B**。代价是交付量大，收益是「既有功能零行为变化」这条底线
**在架构上就是成立的**（物理分流，不是靠约定维持）。落地时用三条机制把代价压下去：

1. **默认关闭**（`ENABLE_AGENT_ORCHESTRATION=false`）：特性出厂即下线，
   灰度由人按 [手册](deploy-gray-rollout.md) 手动开。
2. **只读白名单硬编码**（`READ_ONLY_HANDLER_REFS`，7 项）＋ **schema-only 阶段 0**：
   编排侧的第一波动作**只补 `input_schema`**，一个 `enabled` 都不翻 ⇒
   既有 legacy Agent 行为零变化（见 §7 已知限制 12）。
3. **复用既有只读 handler 与既有 ACL**：不新写工具实现，ACL 直接调
   `AgentRuntimeService.enforcePolicies`。

### 2.2 多视角意见

| 视角 | 结论 | 落点 |
|---|---|---|
| `security-reviewer` | approved-with-deferred：0 CRITICAL / 0 HIGH / **3 MEDIUM** / 8 LOW | 3 个 MEDIUM 全部进灰度手册的 **PRE-ENABLE CHECKLIST**（§7、§8） |
| `code-reviewer` | changes-requested：0 Critical / **2 Important** / 11 Minor | I1（灰度手册阶段 0 会改变既有 Agent 行为）→ **本变更已修**（手册 §2.5 + §9 坑 12）；I2（`_replayTool` 未捕获 `PlannerProtocolError`）→ 已在修复轮次修掉 |
| 实现方 | 「多工具 + 可回放 + 预算」三件事必须成对出现，缺一就是半成品 | 采纳独立表 + 独立 Worker |

---

## 3. 数据模型变更

**只新增，不改既有表**（设计稿 §2.2）。既有 6 张 Agent 相关表
（`agent_definition` / `agent_access_policy` / `agent_tool_config` / `agent_schedule` /
`agent_run_log` / `session_token_usage`）的**字段、约束、语义全部不变**。

新增 5 张表（ORM 见 `backend/app/domain/agent_orchestration_models.py`）：

| 表 | 用途 | 关键约束 / 索引 |
|---|---|---|
| `agent_orchestration_profile` | 一个 Agent 一份编排配置（1:1） | `uq_agent_orchestration_profile_agent`；`ck_…_limits`、`ck_…_revision` |
| `agent_orchestration_binding` | profile ↔ 工具绑定（1:N） | `uq_agent_orchestration_binding_tool`（`profile_id`+`tool_name`）；`ck_…_sort_order`。**不存 handler_ref** |
| `agent_orchestration_run` | 一次编排运行 | `uq_agent_orchestration_run_run_id`；`ix_…_status_id`（Worker 扫队列）、`ix_…_creator_time`、`ix_…_agent_time` |
| `agent_orchestration_step` | 运行的逐步轨迹（PLAN / TOOL） | `ix_agent_orchestration_step_run_step`（`run_id`,`step_no`） |
| `agent_orchestration_usage` | 计量台账（run 级 + 每步） | 无索引（**已知缺口**，见 §7 限制 9） |

**迁移文件**：`backend/alembic/versions/0135_agent_orchestration.py`
（文件名 **28 字符**，≤ 32 约束内）。`revision = "0135"`，`down_revision = "0134"`
（线性接在 `0134_data_layer_rule` 之后）。

> **回滚口径**：`0135` 的 downgrade 会 **DROP 这 5 张表** ⇒ 回滚**不做 downgrade**，
> 而是「停 Worker + 把 `ENABLE_AGENT_ORCHESTRATION` 改回 `false`」，表留着无害
> （没有路由就没有写入）。

**CheckConstraint 变化**：无（既有表的约束一个没动；新表自己的约束见上表）。

**部分索引**：无。

**既有表的数据改动**：**零**。唯一被写到的既有表是 `agent_tool_config`，
且**只改 `input_schema`、不改 `enabled`**（阶段 0，见 §8 与灰度手册 §2.5）；
`input_schema` 对 legacy 路径**无副作用**（`AgentToolAssembly.assemble` 只把它拷进
描述符，legacy 入参来自 `arg_extractor`）。

---

## 4. 接口契约变更

### 4.1 新增端点（flag 开启时共 9 条路径）

配置面 `/api/v1/agent-orchestrations`：

| Method | Path | 成功码 | 说明 |
|---|---|---|---|
| POST | `/api/v1/agent-orchestrations` | **201** | 建 profile；**创建后一律 `enabled=false`** |
| GET | `/api/v1/agent-orchestrations` | 200 | 列当前用户可见的 profile |
| GET | `/api/v1/agent-orchestrations/tool-options` | 200 | 列当前有效的可编排工具（**不含 `handlerRef`**） |
| GET | `/api/v1/agent-orchestrations/{agentCode}` | 200 | 查单个 profile |
| PUT | `/api/v1/agent-orchestrations/{agentCode}` | 200 | 全量更新（带 `revision` 乐观锁） |
| POST | `/api/v1/agent-orchestrations/{agentCode}/toggle` | 200 | 显式启停 |

运行面 `/api/v1/agent-runs`：

| Method | Path | 成功码 | 说明 |
|---|---|---|---|
| POST | `/api/v1/agent-runs` | **202** | 建 run ⇒ `{ runId, status: "QUEUED" }` |
| GET | `/api/v1/agent-runs` | 200 | 列当前用户自己的 run |
| GET | `/api/v1/agent-runs/{runId}` | 200 | 查 run 详情 |
| GET | `/api/v1/agent-runs/{runId}/steps` | 200 | 轨迹（`afterStepNo` 为**闭区间下界**） |
| POST | `/api/v1/agent-runs/{runId}/resume` | **202** | 恢复 `PAUSED` 的 run |
| POST | `/api/v1/agent-runs/{runId}/cancel` | 200 | 取消（`RUNNING` 只标记 `cancelRequested`） |

DTO 契约见 `backend/app/domain/agent_orchestration_schemas.py`：
请求模型一律 `extra="forbid"`（多带字段 ⇒ 422）；**系统上限不在 DTO 里重复**，
由 service 层按 `Settings` 校验。

### 4.2 状态机（新增，不影响既有状态机）

| status | 含义 | 终态 |
|---|---|---|
| QUEUED | 排队等 Worker 领 | 否 |
| RUNNING | Worker 持有租约执行中 | 否 |
| PAUSED | 因 `pause_code` 暂停，可 `resume` | 否 |
| SUCCEEDED | 正常完成 | 是 |
| FAILED | 失败（`errorCode`/`errorMessage` 有原因） | 是 |
| CANCELLED | 已取消 | 是 |

`pause_code`：`NEEDS_INPUT` / `AMBIGUOUS` / `PERMISSION_DENIED` / `BUDGET_EXHAUSTED` /
`TOOL_FAILED` / `PLANNER_OUTPUT_INVALID` / `AGENT_UNAVAILABLE` / `INFRASTRUCTURE_ERROR`。

### 4.3 既有契约：**不变**

`AgentDefinitionCreate/Update/Read`、`AgentRunRequest/AgentRunRead`、
`/agents/options`、既有 run API 的状态码/错误码/错误消息、
AgentRegistryPage / AgentRuntimePage / ChatPage 的现有行为——**一律不变**。
既有 `POST /api/v1/agents/{code}/run` 仍只执行原 `tool_name`（原字段为空仍按旧规则 409）。

### 4.4 ⚠️ 超出设计稿 §9.3 允许范围的改动（必须记账）

**`frontend/src/api/client.ts` 的 `normalizeError` 改了**：现在会把**响应体顶层
`error` 字段**抬升进 `AppError.message`。

- **为什么必须改**：后端的 `ErrorResponse` 是 `{ success, error, detail }`，
  `detail` 恒为 `null`、`AppError.code` 对所有 DomainError 恒为 `UNKNOWN`
  ⇒ 前端拿不到任何可读的失败原因。抬升 `error` 是让编排页能显示后端真实报错
  （如「工具已禁用：wiki_search」）的**最小改法**。
- **影响面**：这是**共享模块**，全站所有走 `httpClient` 的请求都受影响。
- **评审结论**：改动本身合理（顺带修掉 3 个既有前端红），但**必须显式记账**，
  因为它超出了设计稿 §9.3 声明的「前端只新增、不改既有接线」的范围。
- **记账位置**：本节 + [验证报告](../../../docs/superpowers/plans/2026-10-26-general-agent-orchestration-verification.md)。

---

## 5. 实现要点

### 5.1 关键文件

| 角色 | 路径 |
|---|---|
| 配置面 service | `backend/app/services/agent_orchestration_config_service.py` |
| 运行面 service | `backend/app/services/agent_orchestration_run_service.py` |
| 执行器（循环本体） | `backend/app/services/agent_orchestration_runner.py`（+ `…_runner_support.py` / `…_run_views.py`） |
| Planner | `backend/app/services/agent_orchestration_planner.py` |
| 工具准备与执行 | `backend/app/services/agent_orchestration_tool_executor.py` |
| 校验（唯一判定点） | `backend/app/services/agent_orchestration_validation.py` |
| 存储 / 租约 | `backend/app/services/agent_orchestration_store.py` |
| 计量 | `backend/app/services/agent_orchestration_metering.py` / `…_cost.py` |
| 类型与枚举 | `backend/app/services/agent_orchestration_types.py` / `backend/app/domain/agent_orchestration_schemas.py` |
| ORM | `backend/app/domain/agent_orchestration_models.py` |
| API | `backend/app/api/v1/agent_orchestrations.py` / `backend/app/api/v1/agent_runs.py` |
| Worker | `backend/app/workers/agent_orchestration_worker.py` |
| 开关 | `backend/app/config.py`（`ENABLE_AGENT_ORCHESTRATION`，默认 `false`） |
| 路由注册 | `backend/app/main.py`（flag 关闭则**不 include_router**） |
| 前端页面 | `frontend/src/pages/AgentOrchestrationPage.tsx` / `AgentTaskPage.tsx` |
| 前端 API / 类型 | `frontend/src/api/agentOrchestrationApi.ts` / `frontend/src/types/agentOrchestration.ts` / `agentTask.ts` |
| 轮询 hook | `frontend/src/components/agent/task/useAgentRunTracker.ts` |
| compose 契约 | `docker/docker-compose.yml` 的 `agent-orchestration-worker`（`profiles: [agent-orchestration]`） |
| 菜单脚本 | `backend/scripts/seed_agent_orchestration_menu.py` |
| 真实数据脚本 | `backend/scripts/feat_agent_orchestration_realdata.py` |

### 5.2 关键算法

**① 工具可用性判定（唯一判定点，两条路径共用，不得各自实现）**
`assertOrchestrationToolValid(config, toolName)`
（`agent_orchestration_validation.py:99-105`）：

```
enabled == true
  AND handler_kind == BUILTIN
  AND handler_ref ∈ READ_ONLY_HANDLER_REFS（7 项）
  AND validateObjectSchema(input_schema) 通过
```

`validateObjectSchema` 要求：根 `type == "object"`、`properties` 是对象、
`additionalProperties` 为 `false` 或缺省 ⇒ **`{}` 不合法**（这正是阶段 0 存在的理由）。

**② 执行链路（服务端是所有权限与协议决策的最终边界）**

```
Planner 只提出下一步动作（CALL_TOOL / FINISH / ASK_USER），不直接执行工具
  → 快照绑定校验 _assertSnapshotBinding
  → 绑定存在且启用 _loadEnabledBinding
  → 工具当前可用   _loadEnabledToolConfig + _assertReadOnlyBuiltin
  → 入参 JSON Schema 校验 _validateArgs
  → 解析工具       _resolveTool（registry.reload_one）
  → 分层 ACL       _enforceAcl（复用 enforcePolicies，deny-by-default）
  → 执行 handler（唯一调用点 PreparedTool.tool.handler）
  → Observation + step + usage 落库
```

**③ 租约与恢复**
Worker 领走 run 时写 `lease_owner` / `lease_until`（默认 60s）；
`renewLease` 要求「owner + RUNNING + 租约仍有效」三条件；租约过期由
`_recoverOneExpired` 回收重排队。所有写入都走 `_lockLeasedRun` ⇒ 陈旧租约无法提交。

**④ 注入防御**
run 的 `instructions` 与 Planner 输出都过 `prompt_sanitizer`
（`wrapUntrusted` + security preamble，L1 注入防御）。

**⑤ 计量**
每次 LLM 调用（Planner / 工具内部）都落 `agent_orchestration_usage` 一行
（`caller_type` 区分 PLANNER / TOOL）；run 级汇总带 `cost_source`
（`MEASURED` / `ESTIMATED` / `UNKNOWN` / `MIXED`）与 `cost_complete`。

**⑥ 依赖注入点**
FastAPI `Depends(getCurrentUser)`（全部 9 条路由**强制鉴权**）、
`Depends(getDb)`、`Depends(getAgentOrchestrationConfigService)` /
`Depends(getAgentOrchestrationRunService)`；`POST /agent-runs`、`/resume`、`/cancel`
另带 `@limiter.limit(rateLimitValue)`。

---

## 6. 测试

### 6.1 新增测试

| 层次 | 文件 |
|---|---|
| 单元 | `test_agent_orchestration_schemas.py`、`test_agent_orchestration_validation.py`、`test_agent_orchestration_tool_executor.py`、`test_agent_orchestration_planner.py`、`test_agent_orchestration_metering.py`、`test_agent_orchestration_worker.py`、`test_agent_orchestration_runner.py`、`test_agent_orchestration_runner_failures.py`、`test_agent_orchestration_runner_recovery.py`、`test_agent_orchestration_runner_recovery_pause.py` |
| 集成（真实 PostgreSQL） | `test_agent_orchestration_config_api.py`、`test_agent_orchestration_config.py`、`test_agent_orchestration_acceptance.py`、`test_agent_orchestration_route_auth.py`、`test_agent_orchestration_tool_delete.py`、`test_agent_orchestration_worker_claim.py`、`test_agent_runs_api*.py`（create / list / read_acl / steps / resume / cancel / history_audit）、`test_agent_orchestration_menu_seed.py` |
| 前端单测 | `src/tests/agentOrchestrationApi.test.ts`、`AgentOrchestrationPage.test.tsx`、`AgentTaskPage.test.tsx`、`AgentRunTimeline.test.tsx`、`App.test.tsx` |
| 前端 E2E（**浏览器级 mock**） | `e2e/agent-orchestration.spec.ts`（4 条，**green**） |

关键场景覆盖：只读白名单拒绝、schema 校验拒绝 `{}`、禁用工具拒绝（422
「工具已禁用：wiki_search」）、ACL 拒绝转 `PERMISSION_DENIED` 暂停、预算触顶暂停与
`resume`、租约过期回收、崩溃重放、重复工具调用幂等、他人 run 一律 404、注入防御、
计量四类 `cost_source`、菜单播种幂等。

### 6.2 质量门禁（实测输出见[验证报告](../../../docs/superpowers/plans/2026-10-26-general-agent-orchestration-verification.md)）

| 门禁 | 命令 | 结果 |
|---|---|---|
| backend lint | `backend/.venv/bin/ruff check <14 个新文件>` | `All checks passed!`（exit 0） |
| 前端类型 | `npx tsc -p tsconfig.json --noEmit` | exit 0 |
| E2E 类型 | `npx tsc -p e2e/tsconfig.json --noEmit` | exit 0 |
| 后端集成 session | `pytest app/tests/integration`（独立 session） | **107 passed** |
| 后端单测 session | `pytest app/tests/unit`（独立 session） | **231 passed** |
| 前端定向 vitest | 6 个 Task-16 测试文件 | `Test Files 6 passed (6)` / `Tests 78 passed (78)` |
| **新代码覆盖率** | 编排模块（integration + unit 两个 session，`--cov-append`） | **2083 stmts / 117 miss / 94.44%**（门槛 ≥80%） |

> **为什么必须分两个 pytest session**：集成与单测的 seed fixture 会对**同一个共享测试库**
> 做 TRUNCATE/INSERT，混在一个 session 里会撞出 1 failed + 9 ERRORS（`UniqueViolationError`
> on `uq_feature_rule_scope_code`、`ForeignKeyViolationError` on
> `fk_business_object_primary_domain`）—— 那是**争用**，不是分支缺陷（两位评审独立复现、
> 结论一致）。因此按隔离 session 分别跑再取并集，并在报告里写明原因。
>
> 独立复测（Task 16 实施者，17 个模块口径）：`TOTAL 2204 stmts / 119 miss / 95%`，
> `Required test coverage of 80.0% reached. Total coverage: 94.60%`。
> 与上表 94.44% 的差异来自**模块集合不同**（17 vs 15），不是回归。

**净新增失败 = 0**（与基线 `4d4114bb` 对比，同一套测试库，从不并发跑）：

| 套件 | 分支 | 基线 | 结论 |
|---|---|---|---|
| 后端全树 `pytest app/tests` | 1 failed | 1 failed（同一个 `test_chat_service.py::TestChatService::test_class_filter_logs_successful_pruning`） | **净新增 0**（既有失败：`pytest_sessionstart` 先挂 2 个 root handler，导致 `seed_ontology.py:37` 的 `logging.basicConfig` 变成 no-op，root logger 停在 WARNING，测试断言的 INFO 记录从未发出） |
| 前端 vitest 全量 | 193 文件 / 1809 测试，**7 failed** | 191 文件 / 1773 测试，**9 failed** | **净新增 0**（7 ⊂ 9；7 个是 `client.test.ts`(1) / `kpiCatalogApi.test.ts`(1) / `ontologyApi.test.ts`(5)） |
| Playwright（mock） | **27 failed / 1 skipped / 5 passed**（33 测试，11 spec） | **27 failed / 1 skipped / 1 passed**（29 测试，10 spec） | **净新增 0**（失败用例名清单**完全一致**，27 = 27，空 diff；本分支新增的 4 条 `agent-orchestration.spec.ts` 全绿） |

---

## 7. 安全审查

**已触发 `security-reviewer`**（触发条件：认证、用户输入、外部 API 调用、SQL/DB 写入）。
评审对象：`git diff 4d4114bb...HEAD`。

**结论：approved-with-deferred — 0 CRITICAL / 0 HIGH / 3 MEDIUM / 8 LOW。**
10 条安全不变量全部 PASS，11 条对抗性检查全部 PASS（带 file:line 取证）。

| 严重度 | 编号 | 摘要 | 处置 |
|---|---|---|---|
| **MEDIUM** | M1（= 代码评审 M10） | **只读白名单的 TOCTOU**：白名单断言在 `prepare()` 读到的**第一份**行上（`agent_orchestration_tool_executor.py:90-91`），而真正执行的 handler 从**第二次**读取装配（`_resolveTool` → `registry.reload_one`，同文件 `:246`）。两次读之间管理员改 `handler_ref` 即绕过。**当前不可利用**（唯一非白名单 handler `nl2sql_default` 是惰性占位符），**一旦接上 NL2SQL 就变成可利用路径** | **未修（刻意挂账）** ⇒ 进灰度手册 **PRE-ENABLE CHECKLIST P1**，生产开 flag 前必须关闭 |
| **MEDIUM** | M2（= 代码评审 M4） | **租约只在轮边界续**：`renewLease` 全仓库只被调用一次（`agent_orchestration_runner.py:156-168`，调用点 `:162`）；编排路径的 LLM 客户端**无显式 timeout**（沿用 OpenAI SDK 默认读超时 600s），租约默认 60s ⇒ 单轮慢于 60s 就丢租约 ⇒ 回收重排队 ⇒ 该轮重做（重复 LLM 花费；`getOrCreatePlanStep` 复用 RUNNING 的 PLAN 行且**不**累加 `iteration_count`，`max_iterations` 约束不到）。写入侧安全（都走 `_lockLeasedRun`） | **未修（刻意挂账）** ⇒ PRE-ENABLE CHECKLIST **P2** |
| **MEDIUM** | M3（= 代码评审 M11） | **Planner 工具目录无长度/数量上界**：`agent_orchestration_planner.py:371-410`（`_buildMessages` / `_reduceToolCatalog`）把目录原样拼进 system prompt；`agent_tool_config.description` 是无界 `Text`，`tool_names` 无元素个数上限 | **未修（刻意挂账）** ⇒ PRE-ENABLE CHECKLIST **P3** |
| LOW × 8 | L1–L8 | 见下方「已知限制」第 6 / 8 / 9 / 10 / 11 项 | 记入下方清单 |

**MEDIUM 的处置口径**：本特性**出厂即关闭**，生产开 flag 是人工操作 ⇒
「未修」不等于「不可上线」。三项写进[灰度手册](deploy-gray-rollout.md)的
**PRE-ENABLE CHECKLIST（阶段 3.5）**，作为**生产启用前必须逐项关闭的闸门**；
若本轮灰度前关不掉 ⇒ 保持 `ENABLE_AGENT_ORCHESTRATION=false`（业务不受影响），
挂账走独立变更。

### 7.1 已知限制清单（11 项 + 1 条部署纪律）

| # | 限制 | 事实与影响 |
|---|---|---|
| 1 | **用户可见的暂停文案仍归咎于 Planner** | 崩溃重放路径复用既有 `PLANNER_OUTPUT_INVALID`（**故意不新增 `PauseCode` 成员**——那是 API 契约变更）。**服务端日志**明说「崩溃重放的工具参数不再合法（工具配置已变更，需重新规划）」，但**用户可见**文案仍点名 Planner。缓解：枚举 / i18n / API 契约零变更 |
| 2 | **`timeout_seconds` 是「每次尝试」的窗口，不是「整条 run」的窗口** | `started_at` 在每次 claim 和每次 resume 时被重置。`max_iterations` / `max_tool_calls` 仍是**累计**的，所以 run 整体仍有界。（代码评审 M5） |
| 3 | **前端硬编码了系统上限** | 前端写死 20 / 30 / 5 USD / 900 / 1000-50000，而 `backend/app/config.py` 允许用 `AGENT_ORCHESTRATION_MAX_*` 覆盖 ⇒ 覆盖过的部署出现 UI 边界漂移。服务层仍按 `assertWithinSystemCeilings` 兜底 ⇒ **只是 UI 边界漂移**。（代码评审 M6） |
| 4 | **`errorStatus` / `errorDetailText` 逐字重复** | `frontend/src/pages/AgentOrchestrationPage.tsx` 与 `frontend/src/components/agent/task/errors.ts` 各写一份。（代码评审 M7） |
| 5 | **`resumeRun` 吞掉自身异常** | `frontend/src/components/agent/task/useResumeControls.ts:133-134` ⇒ 恢复被拒（409 / 422）时 `form.reset()` 仍执行，用户填的输入丢失。（代码评审 M8） |
| 6 | **`wiki_search` 的 embedding 花销落在 `wiki_token_usage`，不在 `agent_orchestration_usage`** | ⇒ 用了 `wiki_search` 的 run 是**双台账**，只看编排表**不足以**判定成本完整。（安全 L7） |
| 7 | **被消费的 step 行不写 `finished_at`** | `_consumeSteps` 只把它标成 `SKIPPED`。这是**三条消费路径共有的既有行为**，不是本次新开的口子 |
| 8 | **`cancelRun` 断言 owner 但不校验 `lease_until > now`**；`GET /{agentCode}` **没有 owner/admin 过滤**（列表端点有） | （安全 L1 / L4） |
| 9 | **缺 DB 级唯一索引** | `(run_id, iteration_no, kind)` 没有唯一索引；`agent_orchestration_usage` 没有 `run_id` 索引（按 run 聚合会全表扫）。（安全 L2 / L3） |
| 10 | **`tool_names` 无 `max_length`，`agent_tool_config.description` 是无界 `Text`** | （安全 L5 / M3；与 M3 同源） |
| 11 | **两条前端路由无条件注册** | 不进 feature flag ⇒ 特性关闭时页面仍能打开（只是数据接口 404），没有专门的「特性未启用」页态。（安全 L8） |
| 12（部署纪律） | **阶段 0 的 `enabled` 开关会改变既有 legacy Agent 的可运行性** | 4 个 wiki 工具绑在 4 个 `status=active` 的既有 Agent 上（`WIKI_SEARCH_AGENT` id 6 / `WIKI_READ_AGENT` id 7 / `WIKI_RULE_AGENT` id 8 / `WIKI_COVERAGE_AGENT` id 9）；legacy registry 只装载 `enabled=true` 的行（`agent_tool_config_registry.py:33,79`），否则 409（`agent_runtime_service.py:165-178`）⇒ 翻开关 = 4 个 Agent 从「409 不可运行」变「真的执行」。**处置**：第一波**只补 schema**，翻开关推到**后续波次单独审批**（见[灰度手册 §2.5](deploy-gray-rollout.md)、§9 坑 12） |

---

## 8. 部署验证

> **本特性未部署到生产。** 灰度是**人工执行的手册**
> （[deploy-gray-rollout.md](deploy-gray-rollout.md)），由用户/运维在生产机上执行；
> 本仓只交付「工件 + 手册」。下列冒烟**全部在本地隔离环境**完成。

### 8.1 compose 冒烟（特性**关闭** ⇒ Worker **没起来**）

命令（在含 `docker-compose.yml` + `docker-compose.centos.yml` 的目录下；
`.env` 用**假的**合成文件，不碰真 `docker/.env`）：

```bash
# ① 默认部署：Worker 不在服务清单里
docker compose -f docker-compose.yml -f docker-compose.centos.yml config --services

# ② 显式开 profile：Worker 才出现
docker compose -f docker-compose.yml -f docker-compose.centos.yml \
  --profile agent-orchestration config --services

# ③ 没有任何编排容器在跑
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep agent-orchestration

# ④ 不带 profile 时，离线镜像清单里也没有 Worker 镜像
docker compose -f docker-compose.yml -f docker-compose.centos.yml config --images
```

输出：

```
=== ① 默认 config --services ===
neo4j
ollama
postgres
qa-objects
milvus-minio
milvus-etcd
milvus-standalone
backend
frontend
mysql-demo
--- worker 命中数 ---
0

=== ② --profile agent-orchestration config --services ===
postgres
milvus-etcd
milvus-minio
milvus-standalone
neo4j
backend
frontend
qa-objects
agent-orchestration-worker
mysql-demo
ollama
--- worker 命中数 ---
1

=== ③ docker ps agent-orchestration ===
0
(0 = 没有编排容器在跑)
=== ④ config --images（不带 profile）===
0
```

⇒ **默认 `up` 永不包含 Worker**；Worker 只在显式 `--profile agent-orchestration` 时存在。

### 8.2 端点冒烟（`/openapi.json` + 真实 HTTP）

**（a）进程内枚举**（`createApp().openapi()`）：

```bash
cd backend
ENABLE_AGENT_ORCHESTRATION=false ./.venv/bin/python openapi_smoke.py false
ENABLE_AGENT_ORCHESTRATION=true  ./.venv/bin/python openapi_smoke.py true
```

```
=== feature-OFF ===
flag = false
total_paths = 252
orchestration_paths = 0
json_dump = []

=== feature-ON ===
flag = true
total_paths = 261
orchestration_paths = 9
   /api/v1/agent-orchestrations ['get', 'post']
   /api/v1/agent-orchestrations/tool-options ['get']
   /api/v1/agent-orchestrations/{agentCode} ['get', 'put']
   /api/v1/agent-orchestrations/{agentCode}/toggle ['post']
   /api/v1/agent-runs ['get', 'post']
   /api/v1/agent-runs/{runId} ['get']
   /api/v1/agent-runs/{runId}/cancel ['post']
   /api/v1/agent-runs/{runId}/resume ['post']
   /api/v1/agent-runs/{runId}/steps ['get']
```

**（b）真实 HTTP 实例**（uvicorn 跑在 `127.0.0.1:8099`，连**隔离测试库**
`localhost:5436/qa_metadata_test` 与 `bolt://localhost:7688`）：

```bash
curl -s http://127.0.0.1:8099/openapi.json \
  | python3 -c "import sys,json; d=json.load(sys.stdin); p=sorted(d['paths']); print('total_paths =',len(p)); print('orchestration_paths =',len([x for x in p if 'agent-orchestration' in x or 'agent-runs' in x]))"

for p in /api/v1/agent-orchestrations /api/v1/agent-runs; do
  printf '%s -> ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8099$p"
done
```

```
=== feature-OFF openapi ===
total_paths = 252
orchestration_paths = 0
[]
=== feature-OFF 端点冒烟（期望 404）===
/api/v1/agent-orchestrations -> 404
/api/v1/agent-runs -> 404

=== feature-ON openapi ===
total_paths = 261
orchestration_paths = 9
   /api/v1/agent-orchestrations
   /api/v1/agent-orchestrations/tool-options
   /api/v1/agent-orchestrations/{agentCode}
   /api/v1/agent-orchestrations/{agentCode}/toggle
   /api/v1/agent-runs
   /api/v1/agent-runs/{runId}
   /api/v1/agent-runs/{runId}/cancel
   /api/v1/agent-runs/{runId}/resume
   /api/v1/agent-runs/{runId}/steps
=== feature-ON 端点冒烟（期望 200，stub auth）===
/api/v1/agent-orchestrations -> 200
/api/v1/agent-runs -> 200
=== tool-options body ===
[]
```

> `tool-options` 返回 `[]` 是**正确**的：隔离测试库里 7 个工具的 `input_schema` 全是 `{}`
> （阶段 0 之前的状态），`validateObjectSchema` 判它们全部不可绑定。
> 这正是「阶段 0 必须先做」的证据。
>
> **判据必须是 404 而不是 401**：flag 关闭时路由**没注册**，请求不会进入鉴权依赖；
> 用「没带 token 所以 401」当证据是典型的假绿。

### 8.3 真实数据验证脚本输出

脚本：`backend/scripts/feat_agent_orchestration_realdata.py`
（**幂等**：自建行按 marker 删除后重建；**不翻任何 `enabled`**；既有 6 张表行数前后比对）。
运行库：本地 `qa_metadata`（host port 5433，**不是**生产）。

```bash
cd backend
AGENT_ORCHESTRATION_REALDATA_DATABASE_URL=... ./.venv/bin/python scripts/feat_agent_orchestration_realdata.py
```

```
DSN  = postgresql+asyncpg://qa_user:***@localhost:5433/qa_metadata
MARKER = feat-agent-orchestration-realdata
------------------------------------------------------------------------
PASS  S1 迁移版本 — version_num=0135 (期望 0135)
PASS  S1 五张新表 — 存在 5/5
PASS  S2 既有表基线行数 — agent_definition=9, agent_access_policy=12, agent_tool_config=7, agent_schedule=0, agent_run_log=0, session_token_usage=2413
PASS  S3 空 schema 被拒 — ValidationError: tool input schema root type must be 'object'
PASS  S4 禁用工具被拒 — message=工具已禁用：wiki_search (期望 '工具已禁用：wiki_search')
      supplier_360: input_schema {} -> {"type": "object", "properties": {"key": {"type": "string"}}, "additionalProperties": false}（enabled 保持 True 未改）
      supplier_risk: input_schema {} -> {"type": "object", "properties": {"key": {"type": "string"}}, "additionalProperties": false}（enabled 保持 True 未改）
      graph_traverse: input_schema {} -> {"type": "object", "properties": {"key": {"type": "string"}}, "additionalProperties": false}（enabled 保持 True 未改）
PASS  S5 schema-only 修复 — 实际写入 3 行（enabled 未翻转）
PASS  S6 三个已启用工具可编排 — supplier_360=True, supplier_risk=True, graph_traverse=True
PASS  S6 四个 wiki 工具仍未启用 — 全部 enabled=false（证明脚本没有改变既有 legacy Agent 的可运行性）
PASS  S6 只读白名单仍是 7 项 — READ_ONLY_HANDLER_REFS=['coverage_status', 'graph_traverse', 'rule_evaluate', 'supplier_360', 'supplier_risk', 'wiki_read', 'wiki_search']
PASS  S7 profile 创建后为停用 — agent=PROCUREMENT_COPILOT enabled=False revision=1
PASS  S7 绑定的是三个只读工具 — bindings=['graph_traverse', 'supplier_360', 'supplier_risk']
PASS  S7 toggle 后启用且 revision=2 — enabled=True revision=2
PASS  S8 run 创建为 QUEUED — run_id=37647bd9-3368-492d-a519-f96b088a2f6d status=QUEUED
PASS  S8 快照只含只读白名单工具 — snapshot_tools=['graph_traverse', 'supplier_360', 'supplier_risk']（无任何写工具）
PASS  S9 feature-off 时新路由缺席 — /openapi.json 中的编排路径 = []
PASS  S10 自建行已清理 — 残留行数=0
PASS  S2 既有表行数前后一致 — 六张既有表行数无变化
------------------------------------------------------------------------
OK  all steps passed
```

退出码 `0`。

**这次运行证明了什么**：

- **只读性质**：run 的工具快照只含 3 个只读工具，**没有任何写工具**；
- **禁用路径**：绑定被禁用的 `wiki_search` ⇒ 后端自己的 422「工具已禁用：wiki_search」
  —— 走的是 `assertOrchestrationToolValid` 的 disabled 分支，**没有改动任何数据**；
- **既有功能零变化**：六张既有表的行数前后一致；4 个 wiki 工具的 `enabled`
  **全程未被翻转** ⇒ 4 个既有 legacy Agent 的可运行性不变；
- **幂等**：第二次运行同样 `OK all steps passed`（S5 变成「实际写入 0 行」，
  因为第一次已写入同样的值）。

### 8.4 未执行项（明确记录，不得当成已完成）

| 项 | 状态 | 原因 |
|---|---|---|
| **生产部署** | **未执行** | 生产操作全部由人手动执行（灰度手册）。本次交付的是工件 + 手册，**没有**碰生产机、**没有**改 `/opt/qa-system/docker/.env` |
| **生产 `ENABLE_AGENT_ORCHESTRATION=true`** | **未执行** | 同上；且 PRE-ENABLE CHECKLIST 三项尚未关闭 |
| **真实栈浏览器冒烟（Playwright 打真后端）** | **未执行** | 本仓 Playwright 是**浏览器级 mock**（`e2e/support/mockApi.ts` 拦截 `/api/v1/**`，`playwright.config.ts` 只起 Vite）⇒ 打不出真栈。**mock 套件本身是 27 红（分支前后都是）**，本特性新增的 4 条 `agent-orchestration.spec.ts` 全绿，但**不能说「E2E 通过」** |
| **worker 端到端跑完一条真实 run** | **未执行** | 需要一个**启用的**编排 profile + 可用 LLM + 起 Worker 进程。本次只在本地用脚本验证了「建 run ⇒ QUEUED ⇒ 快照正确 ⇒ 清理」，**没有**让 Worker 领起来跑完。真实链路验证放在灰度手册的阶段 5（人工执行） |
| **菜单播种在生产执行** | **未执行** | 人工操作（灰度手册阶段 6） |

---

## 9. 关联

- **设计稿**：`docs/superpowers/specs/2026-10-26-general-agent-orchestration-design.md`
- **灰度手册**：[deploy-gray-rollout.md](deploy-gray-rollout.md)（阶段 0–8、回滚、租约排空、
  暂停码处置、已知坑 12 项、**PRE-ENABLE CHECKLIST**、上线检查清单）
- **验证报告**：`docs/superpowers/plans/2026-10-26-general-agent-orchestration-verification.md`
- **真实数据脚本**：`backend/scripts/feat_agent_orchestration_realdata.py`
- **Wiki**：
  - [agent-runtime](../wiki/agent-runtime.md)（**第三条**执行路径：路径 C）
  - [data-model](../wiki/data-model.md)（Phase 6.6 增量：5 张新表 + `0135`）
  - [api-reference](../wiki/api-reference.md)（9 条新路径 + 编排状态机 + 404/409/422 语义）
  - [frontend](../wiki/frontend.md)（两条路由 + 2s 轮询 + 降级）
- **Rules**：
  - [变更记录强制规范](../rules/变更记录强制规范.md)（§一 的 5 条硬约束 —— 本变更的合并闸门）
  - [开发流程规范](../rules/开发流程规范.md)（真实数据验证门禁）
- **Memory**：`qa-system-general-agent-orchestration.md`
  （`/Users/sunql/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-general-agent-orchestration.md`）
- **入口索引**：[Harness/agents/owner.md](../agents/owner.md)

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A / B / C 三案 + 取舍表 + 最终决定）
- [x] 第 3 段迁移文件名 ≤ 32 字符（`0135_agent_orchestration` = 28）
- [x] 第 7 段触发了 security-reviewer 则必填 CRITICAL/HIGH/MEDIUM/LOW 至少 1 项
      （0 CRITICAL / 0 HIGH / 3 MEDIUM / 8 LOW，另有代码评审 2 Important / 11 Minor）
- [x] 第 8 段 docker compose 冒烟命令 + 输出贴出（§8.1）
- [x] 第 8 段 `/openapi.json` 端点冒烟命令 + 输出贴出（§8.2，进程内 + 真实 HTTP 两轮）
- [x] 第 8 段真实数据验证脚本输出贴出（§8.3，退出码 0，全部 PASS）
- [x] 第 9 段 ≥ 3 个跨文件链接（设计稿 / 灰度手册 / 验证报告 / 4 个 Wiki / 2 条 Rules / Memory / owner.md）
- [x] 相关 wiki 文档已更新（数据模型 / 接口 / 架构 / 前端 —— 4 处全改）
- [x] 至少 1 条 MEMORY 索引已在 `~/.claude/projects/.../memory/MEMORY.md` 添加
      （索引行 + 详情文件均已存在；由控制方写入）
- [x] 涉及真实 SQL/DB 改动时 `scripts/<feature>_realdata.py` 已跑通
      （`backend/scripts/feat_agent_orchestration_realdata.py`，exit 0）
