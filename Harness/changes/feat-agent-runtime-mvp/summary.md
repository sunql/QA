# 变更：feat-agent-runtime-mvp

- **日期**：2026-08-31
- **阶段**：Phase 6.4（L5 AI Native 起步 · Agent 平台）
- **提交**：`feat: agent runtime mvp (Phase 6.4)`
- **验收标准**（计划 Phase 6）：≥2 个 Agent 端到端可调用（用户提问 → Agent → Tool → 响应）+ 覆盖率 ≥ 80%

---

## 1. 需求

Agent Runtime 最小版（MVP）：复用 Phase 6.1 的 Agent Registry 注册资产，实现「注册 → 调度 → 工具调用」最小闭环。

- **调度**：轻量调度器（不替代 LangGraph 等框架），`AgentRuntimeService.run()` 编排：状态门禁 → 工具路由 → 权限策略拦截 → 执行 → 结果回传。
- **工具绑定**：`AGENT_TOOLS` 声明式映射（`agent_code → 工具名元组`，顺序即优先级）。本期绑定 3 个可运行 Agent；未绑定工具的已注册 Agent（元数据占位）→ 409 不可运行。
- **触发路径**：
  1. REST：`POST /api/v1/agents/{agent_code}/run`（确定性测试入口，不注入真实 LLM factory）。
  2. Chat：用户问题显式指名 Agent（`IntentType.AGENT_RUN` 最优先）→ `chat_service._handleAgentRun` → 复用真实 LLM factory + 模型路由 + 术语字典。
  3. 前端：`AgentRuntimePage` 手动触发 + `AgentResponseCard` 卡片渲染（chat 与 REST 双路径共用）。
- **Seed**：`scripts/seed_agents.py` 幂等注册 5 个 Agent（3 可运行 + 2 元数据占位）。

## 2. 设计评审

| 决策点 | 选择 | 理由 |
|---|---|---|
| 调度框架 | 自研轻量 `AgentRuntimeService`，不引入 LangGraph | MVP 只有 3 个单工具 Agent，编排简单；框架预留 Phase 7+ |
| 工具绑定 | 硬编码 `AGENT_TOOLS` dict | 与 `RISK_RULES`/`DEFAULT_SUPPLIER_FEATURES` 同模式；无 DB 配置复杂度 |
| 权限拦截 | 复用 AgentAccessPolicy（Phase 6.1），`deny-by-default` | 无政策条目 → 拒绝执行（409），不静默放行 |
| 调用方身份 | `CurrentUser` 从 API 层透传（`X-User-Id` headers），REST/chat 双路径 | `ChatRequest` 无 user 字段，身份只存在于 API 层；`actor` 归属审计 |
| LLM factory | REST run 不注入（确定性可测）；chat 路径注入真实 factory | 复用 model_router_service 按 Agent 选模型 + term_dictionary 注入术语 |
| 意图优先级 | `AGENT_RUN` 在 `classifyResult` 最优先（显式指名胜过一切启发式） | 指名调用不应被 supplier_360/risk 等正则吸走；用 2 条「正则不误吸」护栏测试 |
| 流式路径 | `_streamInterceptCard` 拦截 4 类卡片意图（360°/risk/graph/agent_run）统一路由 | 默认 UI 全走 `/chat/stream`；此前拦截卡片只在非流式响应回填（审查 HIGH） |
| Token 计量 | `_recordDirectUsage` 补审计（`modelConfigId=None`） | 满足硬约束「每次 LLM 调用必须记录 Token 消耗与成本」 |

## 3. 数据模型变更

**无新表、无 Alembic 迁移。** 复用 Phase 6.1 `agent_definition` / `agent_access_policy`（迁移 0025）。

仅纯代码层新增：

- `IntentType.AGENT_RUN = "agent_run"`（`domain/enums.py`）
- `AgentRunRequest` / `AgentRunRead` DTO（`domain/schemas.py`）
- `ChatResponse.agent_run: AgentRunRead | None`（`domain/schemas.py`）
- 消息常量 `MSG_AGENT_NOT_FOUND` / `MSG_AGENT_NOT_RUNNABLE` / `MSG_AGENT_INPUT_GUIDE` / `MSG_SCHEMA_CHAT_AGENT_RUN`（`services/messages_zh.py` + `domain/error_messages.py`）

## 4. 关键代码

### 后端

- `services/agent_runtime_service.py`（NEW，~200 行）：
  `AgentRuntimeService.run(session, agent_code, params, *, actor)` → `_loadAgent`（查注册 + 404）→ `_assertRunnable`（ACTIVE + AGENT_TOOLS 有绑定 + deny-by-default 策略非空）→ `_invokeTool(agent_code, params)`（解析到工具函数 → 执行）→ `AgentRunRead`。
  - `AGENT_TOOLS`：`SUPPLIER_360_AGENT→supplier_360`、`SUPPLIER_RISK_AGENT→supplier_risk`、`GRAPH_REASONING_AGENT→graph_traverse`。
  - `data_layer` 策略拦截为硬编码骨架（仅校验非空存在性；粒度校验列为已知缺口 LOW#3）。
- `services/agent_tools.py`（NEW）：工具注册表——`supplier_360` / `supplier_risk` / `graph_traverse` 3 个工具，包装既有 `Supplier360Service` / `SupplierRiskService` / `GraphTraversalService`，含参数提取（`arg_extractor` 正则）。
- `api/v1/agent_runtime.py`（NEW）：`POST /agents/{agent_code}/run`（依赖 `getCurrentUser`，透传 actor）。
- `services/chat_service.py`：
  - `processMessage(..., *, user)` 派发 `IntentType.AGENT_RUN → _handleAgentRun`（PermissionDeniedError → 200 + exc.message，LOW#5）。
  - `_handleAgentRun` 成功/失败均走 `_recordDirectUsage(purpose="agent_run")`。
  - `processMessageStream(..., *, user)` 新增拦截分支 → `_streamInterceptCard` 统一路由 4 类卡片意图，产出 `meta → token → done`（done 携带 `agentRun/supplier360/supplierRisk/graphTraversal` 4 个卡片对象，`model_dump(mode="json", by_alias=True)`）。
- `services/intent_service.py`：`_AGENT` 指名正则（`用 xxx_agent ...` / `用 xxx ...`）+ `classifyResult` 最优先分支。
- `services/supplier_risk_service.py`：`_generateRiskPoints` 修复真实计量 bug——`LlmResponse` 为 camelCase（`promptTokens`/`completionTokens`/`modelName`），此前读 snake_case 恒为 0（审查 MEDIUM#2 根因）；改为 camelCase 优先 + snake_case 回退（兼容既有测试 fake）。
- `scripts/seed_agents.py`（NEW）：幂等注册 5 个 Agent（预查 + 冲突 catch）。
- `main.py` / `tests/_testapp.py`：挂载 `agent_runtime` router。

### 前端

- `types/agentRuntime.ts`（NEW）：`AgentRunRead` / `AgentTool` 等类型。
- `api/agentRuntime.ts`（NEW）：`runAgent(agentCode, params)` → POST run 端点。
- `components/chat/AgentResponseCard.tsx`（NEW）：卡片渲染 Agent 结果（工具名 + 结果摘要 + tokens/cost）；chat 与 REST 双路径共用。
- `pages/AgentRuntimePage.tsx`（NEW）：Agent 列表 → 手动触发 → 结果展示（入口：`AppLayout` 菜单 + `App.tsx` 路由）。
- `api/chat.ts`：`StreamSummary` 扩展 `agentRun/supplier360/supplierRisk/graphTraversal` 4 个可选卡片字段（审查 HIGH 修复）。
- `stores/chatStore.ts`：流式 `onDone` 接收并回填 4 个卡片对象（`?? null`，按字段存在性渲染）。

## 5. 测试

| 层 | 文件 | 用例 | 覆盖 |
|---|---|---|---|
| 单元 | `tests/unit/test_agent_tool_registry.py` | 6 | 3 工具注册 + 参数提取 + 未知工具 |
| 单元 | `tests/unit/test_agent_runtime_service.py` | 13 | 状态门禁 / 策略拦截 / deny-by-default / 工具执行 / 不可运行 409 |
| 集成 | `tests/integration/test_agent_runtime_api.py` | 10 | REST run 端点全链路（真实 PG） |
| 集成 | `tests/integration/test_chat_agent_run.py` | 8 | chat 指名 → agent_run 意图 + 结果回传 + 未知/不可运行友好 + 不误吸护栏 + **流式路由 + token_usage 审计** |

**本次审查补强**（RED→GREEN）：
- `test_chat_agent_run_stream_routes_agent_run_and_audits_tokens`：SSE `meta → token → done`（done 携带 agentRun）+ `token_usage` 落行（purpose=agent_run、total_tokens=2、model_name=test-model）。
- `test_chat_supplier_risk_stream_routes_card`：supplier_risk 拦截意图在流式路径同样路由（共享 `_streamInterceptCard`）。
- 后端全套：**1843 passed，覆盖率 93.20%**（门槛 ≥ 80% ✓）。
- 前端：`tsc --noEmit` 0 errors；vitest 348 用例（1 个 `EntityMappingPage` 预存在 flake，隔离运行 6/6 通过）。

## 6. 安全审查

`code-reviewer` + `security-reviewer` 双审，发现并修复：

| 级别 | 发现 | 修复 |
|---|---|---|
| **HIGH（安全）** | 调用方身份被取未用 → 审计归属丢失 | `CurrentUser` 从 API 层透传 `processMessage/processMessageStream/_handleAgentRun`，`actor=user.userId or "chat"` |
| **HIGH（代码）** | 默认 UI 全走 `/chat/stream`，AGENT_RUN 未路由流式路径 | `_streamInteractCard` 拦截 4 类卡片意图统一路由 + 前端 `onDone`/`StreamSummary` 回填 |
| **MEDIUM#2（计量）** | Agent 内部 LLM 计量不落库；且 `_generateRiskPoints` 读 snake_case 致真实计量恒 0 | `_recordDirectUsage`（purpose=agent_run/supplier_risk）+ `LlmResponse` camelCase 读取修复（真实生产 bug） |
| **LOW#5（体验）** | 权限拒绝回答 `object="?"` | 改用 `exc.message`（含真实 data_object） |
| **LOW#3（粒度）** | `data_layer` 策略仅校验存在性，未做粒度判定 | **已修复**（后续 commit `feat: agent data_layer policy granularity`）：`AgentTool.data_layers` 声明读取层，运行时逐层授权（`data_layer=None` 通配 + 精确匹配），缺失任一层 → 403 |
| **LOW#4（阻塞）** | Neo4j 调用同步阻塞事件循环 | 列入 §10 已知缺口（与既有 graph_traversal 模式一致，不阻断 MVP） |

ACL 四项复查（DTO mass-assignment / 403 侧信道 / actor 派生 / 非 admin 集成测试）全部通过。

## 7. 部署与迁移

- 无 DB 迁移、无新依赖、无配置变更。
- 幂等 seed（新增 Agent 数输出 + 可运行计数校验）：
  ```bash
  cd backend && DATABASE_URL=... uv run python scripts/seed_agents.py
  # 期望：新增 5（或 0 幂等跳过），可运行 3
  ```

## 8. 关键文件清单

**后端新增**：
- `app/services/agent_runtime_service.py` / `agent_tools.py`
- `app/api/v1/agent_runtime.py`
- `scripts/seed_agents.py`
- `app/tests/unit/test_agent_runtime_service.py` / `test_agent_tool_registry.py`
- `app/tests/integration/test_agent_runtime_api.py` / `test_chat_agent_run.py`

**后端修改**：
- `app/domain/enums.py`（IntentType.AGENT_RUN）
- `app/domain/schemas.py`（AgentRun DTO + ChatResponse.agent_run）
- `app/domain/error_messages.py`、`app/services/messages_zh.py`
- `app/services/chat_service.py`（_handleAgentRun + _streamInterceptCard + _recordDirectUsage + user 透传）
- `app/services/intent_service.py`（_AGENT 指名 + 最优先）
- `app/services/supplier_risk_service.py`（计量 camelCase 修复）
- `app/main.py`、`app/tests/_testapp.py`（router 挂载）
- `app/tests/integration/test_token_usage_service.py`

**前端新增**：
- `src/types/agentRuntime.ts` / `src/api/agentRuntime.ts`
- `src/components/chat/AgentResponseCard.tsx` / `src/pages/AgentRuntimePage.tsx`
- `src/components/chat/AgentResponseCard.test.tsx` / `src/pages/AgentRuntimePage.test.tsx`

**前端修改**：
- `src/api/chat.ts`（StreamSummary 4 卡片字段）、`src/stores/chatStore.ts`（onDone 回填）
- `src/components/chat/MessageItem.tsx`（agentRun 渲染）、`src/App.tsx`、`src/components/common/AppLayout.tsx`
- `src/i18n/locales/zh-CN.ts` / `en-US.ts`、`src/types/agentRegistry.ts`

**SSOT / Wiki**：
- `Harness/changes/feat-agent-runtime-mvp/summary.md`（本文件）
- `Harness/wiki/business-domain.md`（Agent Runtime 章节）

## 9. 验证

### 自动化验证

| 项 | 结果 |
|---|---|
| 后端测试 | **1843 passed**（含 6.4 新增 37 用例） |
| 后端覆盖率 | **93.20%**（门槛 ≥ 80% ✓） |
| 前端 tsc | **0 errors** |
| 前端 vitest | **348 用例**（1 预存在 flake，非本 change 回归） |

### 真实数据验证（live 后端 :8001）

| 场景 | 结果 |
|---|---|
| REST `SUPPLIER_360_AGENT` run | ✅ 返回 profile + entityCodes（跨系统编码） |
| REST `SUPPLIER_RISK_AGENT` run | ✅ level=unknown / fallback 路径可用 |
| REST `GRAPH_REASONING_AGENT` run | ✅ 2-hop SUPPLIES RM-STEEL-001/002 链路返回 |
| Chat 指名 agent_run（stream） | ✅ `meta(agent_run) → token → done`，done 携带完整 agentRun 卡片 + token_usage 审计 |
| 未绑定工具 Agent（PROCUREMENT_COPILOT） | ✅ 409 不可运行（预期） |
| Neo4j 图重播种 | ✅ 节点 31 / 边 47（`seed_graph_relations.py`） |

## 10. 已知缺口

> LOW#3 `data_layer` 策略粒度已修复（commit `feat: agent data_layer policy granularity`）：
> 工具声明 `data_layers`（supplier_360/supplier_risk → DIM+FEATURE；graph_traverse → DIM+DWD），
> 运行时逐层授权（精确匹配或 `data_layer=None` 通配），缺失任一层 → 403。
> seed_agents 从工具注册表派生显式分层策略（DRY 防漂移），并对既有部署做幂等策略愈合
> （删除通配 READ → 显式分层，保证分层约束在旧部署上同样生效，非空操作）。
> 双审（code-reviewer + security-reviewer）补强：
> - FORBIDDEN / FORBIDDEN_WRITE 为**显式否决**：对象匹配 + 层匹配（或 None 通配）即拒绝，
>   优先于任何 READ 授予——修复「通配 READ 覆盖层级 FORBIDDEN」可绕过缺口（审查 HIGH）；
>   独立 403 消息 `MSG_AGENT_RUN_FORBIDDEN`。
> - `data_layer` 写入边界归一化（`strip().upper()`，空串 → None 通配），防大小写/空白
>   导致的静默 fail-closed。
> - 层无关工具（`data_layers=()`）回退对象粒度，对象上任一 FORBIDDEN 仍优先否决。
> 测试：分层单测 9 + 归一化 4 + 集成 2（含分层不匹配 403 + 通配 READ 被 FORBIDDEN 否决）。

- **Neo4j 同步阻塞**（LOW#4）：~~已修复（commit `fix: neo4j graph traversal runs in thread to avoid blocking event loop`）~~：`GraphTraversalService.traverse/traverseForChat` 用 `asyncio.to_thread()` 包装同步 Neo4j 驱动调用，事件循环不再阻塞。见 `change-fix-neo4j-async-traverse.md`。
- **Token 审计仅存合计**（LOW#5）：~~已修复（commit `feat: token usage prompt/completion split`）~~：`ToolResult` / `AgentRunRead` / `SupplierRiskRead` 扩展 `prompt_tokens` / `completion_tokens`，`_recordDirectUsage` 透传给 `SessionTokenUsage`。见 `change-feat-token-usage-split.md`。
- **图遍历仅 2-hop**：~~已修复（commit `feat: chat graph traversal supports >2-hop`）~~：Chat 问句口语跳数（「3 跳 / 三跳 / 深度 N」）经 `extractGraphMaxHops` 解析入 `IntentResult.max_hops`，`_handleGraphReasoning` 用 `resolveChatMaxHops`（None 默认 2，越界 clamp [1,5]）调 `traverse`。见 `change-feat-graph-traversal-chat-hops.md`。注：`graph_traverse` agent tool 仍固定 2 跳（工具路径跳数留后续）。
- **Chat 未指名 Agent 的语义路由**：~~已修复（commit `feat: chat agent semantic routing`）~~：中置信语义路由在既有确定性意图均未命中后触发，高置信（≥0.7）自动 AGENT_RUN，中置信（0.4–0.7）走 QUERY + `suggested_agent` 建议卡片，低置信不干预；关键词纯评分零 LLM 成本，实体/聚合双门禁防误吸。见 `change-feat-agent-semantic-routing.md`。注：「一键触发」交互留作后续（V1 仅展示 + 提示显式指名）。
- **无批量调度**：~~已修复（commit `feat: agent scheduler`）~~：croniter 定时调度（POST/GET/PATCH/DELETE /agents/{code}/schedules）+ 独立 worker 进程轮询 PG `agent_schedule`（条件 UPDATE claim 防并发双跑）+ `agent_run_log` 落库（status/answer/error/tokens/cost/actor=scheduler:{id}）。见 `change-feat-agent-scheduler.md`。
