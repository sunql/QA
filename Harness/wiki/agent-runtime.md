# Agent 运行时总览（数据模型 / 管理面 / 三条执行路径 / chat 接入）

> 2026-10-25 梳理自代码实读。定位：伞形文档——L4 循环细节见 [[agent-loop]]，
> 工具双结构见 [[agent-tool-maintenance]]，NL2SQL 上下文见 [[nl2sql-engine]]。
> 2026-10-28 增量：新增**第三条**执行路径（通用多工具编排，见路径 C）。

## 一句话定位

**Agent = 「代码工具执行体（白名单）+ DB 元数据（免发版调暴露面）+
deny-by-default 分层 ACL」的可注册调度单元**；运行时要么单工具直跑
（显式指名 / 确定性意图），要么进 LLM 驱动的有界循环（L4），失败全线降级 NL2SQL；
2026-10-28 起还有**第三条**独立路径——通用多工具编排（路径 C），它有自己的
配置面、运行面与 Worker 进程，**默认关闭**，且**不从 chat 触发**。

## 1. 数据模型：Agent 不是一段代码，是一行注册元数据

`AgentDefinition`（`backend/app/domain/models.py:1552`）：

| 字段 | 语义 |
|---|---|
| `agent_code` | 业务唯一键（如 `SUPPLIER_RISK_AGENT`），路由识别用 |
| `status` | 治理门禁：仅 `ACTIVE` 可运行；`DRAFT`/`DEPRECATED` → 409 |
| `data_domains` / `data_layers` | JSONB 授权词表。**feat-data-domain-layer（2026-10-25）后 SSOT 迁到 `data_domain` / `data_layer_rule` 表**：创建/编辑经 registry 校验，常量只作未 warmUp 时的 fallback |
| `tool_name` | **FK-by-name 绑定工具**；None = 未绑定 = 不可运行（409）。创建后改工具名 = 新建工具行 + 迁移绑定（改名断链） |
| `policies` → `AgentAccessPolicy` | 一行 = (Agent × data_object × data_layer) 的权限，deny-by-default；删除 Agent 级联清理 |

## 2. 管理面（Agent Registry 页）

- CRUD + 词表校验：`_vocabCheckDomains` 查 `dataDomainRegistry`（enabled 集，
  禁用域不可用于新写入）、`_vocabCheckLayers` 查 `layerRuleRegistry`；
  授权语义下未 warmUp 时 fallback 常量，**不开放未加载状态**
- `/agents/options` 统一下发 domains / layers / tools 词表——前端 Agent 页与
  工具管理页共用（DB 驱动，方案 2 后 7 层词表同源）
- 工具绑定走 `agent_binding_cache`（DB cache 唯一数据源，miss → 409，无 dict
  fallback）；lifespan warmUp 预载 registry
- 装配校验三级白名单（handler_kind → handler_ref → arg_extractor_kind），
  详见 [[agent-tool-maintenance]]

## 3. 运行面：三条执行路径

三条路径**彼此独立**：各自有入口、状态存储与生命周期，**互不调用**。
路径 A / B 走 API 进程的同步链路；路径 C 走**独立 Worker 进程**（异步排队）。


### 路径 A —— 单工具直跑（`AgentRuntimeService.run`）

`backend/app/services/agent_runtime_service.py:98`，六步链路：

```
① getAgent                    → 不存在 404
② status ≠ ACTIVE             → 409
③ bindingCache.getToolName    → 无绑定 → 409（agent_not_runnable_no_tool）
④ _resolveTool                → DB-backed registry，miss 时 reload_one；
                                不存在/禁用 → 409 tool_unbound_or_disabled
⑤ _enforcePolicies            → deny-by-default，违例 403
⑥ 名字预解析（供应商名→编码，Phase 6.5）→ arg_extractor（None → 422）
   → tool.handler → AgentRunRead（token/cost 全透传）
```

**策略拦截 `_enforcePolicies`（:182）是核心安全边界：**

- **deny-by-default**：工具声明的每个 `data_layer` 都须有 READ/MASKED_READ
  授予，缺任一层 → 403
- **显式否决优先**：`FORBIDDEN`/`FORBIDDEN_WRITE` 即使存在通配 READ 也拒绝
  （防跨层通配 READ 覆盖层级 FORBIDDEN）
- `data_layer` 比较前 `_normalizeDataLayer` 归一化大写——历史非大写行也须被
  识别，防 fail-open
- 工具不声明层（`data_layers=()`）→ 回退 `data_object` 粒度（旧行为兼容），
  对象上任一 FORBIDDEN 同样优先于 READ
- `MASKED_READ` 视为可访问（脱敏留待后续 Phase）

### 路径 B —— L4 Agent Loop（`run_agent_loop`）

`agent_runtime_service.py:603`。LLM 驱动的纯 Python async while 循环
（刻意不用 LangGraph：handler 全 async、StateGraph 包装复杂、mock 难），
`max_iterations=5` + cost budget，终止条件 `answered / max_iterations /
cost_cap / error`。关键细节：

- 中途异常**收敛为「带累计 token 的正常返回」**——异常穿出去，已花的钱既不
  进台账也不进监控（核心约束③防丢账）
- `max_iterations` 耗尽时若最后一轮有自然语言输出，取作 answer——避免 chat
  误判 L4 失败降级 L2（历史 bug）
- 5 个 NL2SQL 工具（list_tables / describe_table / sample_rows / execute_sql /
  list_joins）+ SQL Guard 双层检查
- 实现细节、prompt 预算策略、错误恢复表 → **[[agent-loop]]**

### 路径 C —— 通用多工具编排（`AgentOrchestrationRunner` + 独立 Worker）

`backend/app/services/agent_orchestration_runner.py` +
`backend/app/workers/agent_orchestration_worker.py`（2026-10-28 新增，Phase 6.6）。

**与路径 A / B 完全独立**——不是「把 L4 泛化」，而是**第三套运行面**：

| 维度 | 路径 C 的做法 |
|---|---|
| 入口 | `POST /api/v1/agent-runs` → **202** + `QUEUED`（**不**从 `POST /agents/{code}/run` 进入，也**不**从 chat 触发） |
| 执行体 | **独立 Worker 进程**（`python -m app.workers.agent_orchestration_worker`），FastAPI 的 `BackgroundTasks` 一律不用 |
| 配置面 | 新表 `agent_orchestration_profile` / `agent_orchestration_binding`（一个 Agent 一份编排 profile + 工具绑定），**不改** `agent_definition` / `agent_tool_config` |
| 工具集合 | **只读白名单 7 个 handler**（`supplier_360` / `supplier_risk` / `graph_traverse` / `wiki_search` / `wiki_read` / `rule_evaluate` / `coverage_status`），`READ_ONLY_HANDLER_REFS` 硬编码 |
| 循环形态 | **Planner → 工具调用 → Observation** 的多步循环（`agent_orchestration_planner.py` + `agent_orchestration_tool_executor.py`），预算上限 `max_iterations` / `max_tool_calls` / `max_cost_usd` |
| 状态 | `QUEUED` → `RUNNING` → `SUCCEEDED` / `FAILED` / `PAUSED` / `CANCELLED`；`PAUSED` 可 `resume`，租约（`lease_owner` / `lease_until`）防重复执行 |
| 开关 | `ENABLE_AGENT_ORCHESTRATION` **默认 `false`** ⇒ **不注册新路由 且 不起 Worker**；Worker 只在 compose `--profile agent-orchestration` 时启动，**不发布宿主端口**、**不在 `depends_on`** 里 |

**关键约束**：路径 C **复用**路径 A 的 ACL 与只读判定（`_enforcePolicies` 同一套
deny-by-default 分层 ACL、同一套 `arg_extractor`），但**不复用**路径 B 的 L4 循环。
旧 Agent 的既有行为**零变化**（`input_schema` 改动对 legacy 路径无副作用；
`enabled` 是唯一影响行为的开关）。

- 设计稿：`docs/superpowers/specs/2026-10-26-general-agent-orchestration-design.md`
- 灰度手册：`Harness/changes/feat-agent-orchestration/deploy-gray-rollout.md`
- 变更 SSOT：`Harness/changes/feat-agent-orchestration/summary.md`

## 4. chat 接入：Agent 的四个触发入口

> **路径 C 不在此表内** —— 通用多工具编排**没有** chat 触发入口，
> 只从 `POST /api/v1/agent-runs` 排队进入。


`ChatService.processMessage`（`chat_service.py:463-493`）意图管线，优先级递减：

| 优先级 | 入口 | 行为 |
|---|---|---|
| 1 | **显式指名** `AGENT_RUN` | 意图分类最优先，保证「用 supplier_risk_agent 评估 100001」不被确定性拦截吸走 → 路径 A |
| 2 | **确定性意图拦截** `SUPPLIER_360` / `SUPPLIER_RISK` / `GRAPH_REASONING` | 各自 `_handle*`（路径 A 的领域特化，跳过 NL2SQL） |
| 3 | **未指名语义路由**（`AgentRoutingService`，Phase 7 G4） | 纯关键词评分 `min(1, 命中数×0.4)`：≥0.7 自动调度 Agent，0.4~0.7 附建议卡片；只在既有意图未命中时生效，绝不覆盖确定性拦截 |
| 4 | 通用查询路径 L4 | L4 loop 失败 → 降级 L2/L3 NL2SQL |

## 5. 横切约束

- **prompt 安全**：LLM 入口统一走 `prompt_sanitizer`（wrapUntrusted + security
  preamble，L1 注入防御，PR #43）
- **DB 只读**：业务库连接 `_assert_read_only` 兜底（`business_db_pool`）
- **计量**：路径 A / B token/cost 全链路透传落台账（`_recordUsage`）；路径 C 有**独立**的
  `agent_orchestration_usage` 台账（run 级汇总 + 每步明细，`cost_source` 区分
  `MEASURED` / `ESTIMATED` / `UNKNOWN` / `MIXED`）
- **不可变**：`AgentToolContext` / `ToolResult` / `AgentTool` 均 frozen dataclass

## 关联文件

| 组件 | 路径 |
|---|---|
| 运行时（路径 A + L4 loop） | `backend/app/services/agent_runtime_service.py` |
| Agent / 策略模型 | `backend/app/domain/models.py:1552`（AgentDefinition / AgentAccessPolicy） |
| 工具注册表 + 内置 handler | `backend/app/services/agent_tools.py`（wiki 域在 `agent_tools_wiki.py`，类型契约在 `agent_tool_types.py`） |
| 工具 DB 元数据 registry | `backend/app/services/agent_tool_config_registry.py` |
| 绑定 cache | `backend/app/services/agent_binding_cache.py` |
| 语义路由 | `backend/app/services/agent_routing_service.py` |
| chat 分发 | `backend/app/services/chat_service.py`（`_handleAgentRun` / `_maybeRunL4AgentLoop`） |
| 编排配置面（路径 C） | `backend/app/services/agent_orchestration_config_service.py` |
| 编排运行面（路径 C） | `backend/app/services/agent_orchestration_run_service.py` / `agent_orchestration_runner.py` / `agent_orchestration_runner_support.py` / `agent_orchestration_run_views.py` |
| 编排 Planner / 工具执行（路径 C） | `backend/app/services/agent_orchestration_planner.py` / `agent_orchestration_tool_executor.py` |
| 编排存储 / 计量（路径 C） | `backend/app/services/agent_orchestration_store.py` / `agent_orchestration_metering.py` / `agent_orchestration_cost.py` |
| 编排模型 / 契约（路径 C） | `backend/app/domain/agent_orchestration_models.py` / `backend/app/domain/agent_orchestration_schemas.py` / `backend/app/services/agent_orchestration_types.py` / `agent_orchestration_validation.py` |
| 编排 Worker（路径 C） | `backend/app/workers/agent_orchestration_worker.py` |
| 编排 API（路径 C） | `backend/app/api/v1/agent_orchestrations.py` / `backend/app/api/v1/agent_runs.py` |
| 工具维护手册 | [[agent-tool-maintenance]] |
| L4 loop 细节 | [[agent-loop]] |
| 编排变更 SSOT | [feat-agent-orchestration](../changes/feat-agent-orchestration/summary.md) |
