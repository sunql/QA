# Phase 7：Agent Runtime 遗留缺口实现计划

- **日期**：2026-09-01
- **来源**：`Harness/changes/feat-agent-runtime-mvp/summary.md §10` 已知缺口
- **目标**：把 6 个缺口拆分为可独立交付的 change，每个 change 都遵循 TDD + 双审 + SSOT
- **约束**：单文件 < 800 行、函数 < 50 行、覆盖率 ≥ 80%、真实 PG 测试

---

## 总体策略

| 缺口 | Change 名 | 优先级 | 范围 | 预估改动文件数 | 状态 |
|---|---|---|---|---|---|
| G1 Neo4j 同步阻塞 | `fix-neo4j-async-traverse` | P1 | 后端 service | 3 | ✅ 已交付 |
| G2 Token 审计拆分 | `feat-token-usage-split` | P1 | 后端 DTO + service | 5 | ✅ 已交付 |
| G3 图遍历 > 2-hop | `feat-graph-traversal-chat-hops` | P2 | 后端 intent + service | 4 | ✅ 已交付 |
| G4 Chat 语义路由 | `feat-agent-semantic-routing` | P2 | 后端 intent + 前端 chat | 6 | 待实现 |
| G5 批量调度 | `feat-agent-scheduler` | P2 | 后端 model + API + scheduler | 8 | 待实现 |
| G6 data_layers 防漂移 | `feat-agent-tool-layer-contract` | P1 | 后端 registry + 测试 | 3 | ✅ 已交付 |

交付顺序：**G1 → G2 → G6 → G3 → G4 → G5**（先补基础设施与数据契约，再做智能化与调度）。

---

## G1：Neo4j 同步阻塞异步化

### 问题
`GraphTraversalService.traverse/traverseForChat` 是 async 方法，但内部调用 `neo4j_client.traverseBusinessGraph/getBusinessNode` 使用同步官方驱动（`with driver.session()`），会在事件循环中阻塞 I/O。

### 目标
遍历调用不再阻塞主事件循环，高并发时其他请求不被图查询拖住。

### 方案
保留现有同步 `neo4j_client.py`（改动最小，无依赖变更），在 async 调用点用 `asyncio.to_thread()` 包装：

1. `graph_traversal_service.py` 中 `neo4j.getBusinessNode(...)` → `await asyncio.to_thread(neo4j.getBusinessNode, ...)`。
2. `neo4j.traverseBusinessGraph(...)` → `await asyncio.to_thread(neo4j.traverseBusinessGraph, ...)`。
3. 保持 CQL、白名单、错误语义不变。

> 备选方案（后续如需全面异步）：把 `neo4j_client.py` 整体迁移到 `neo4j.AsyncGraphDatabase`。本期只做遍历路径，因为业务图 API 调用量最大。

### 关键文件
- `backend/app/services/graph_traversal_service.py`
- `backend/app/tests/unit/test_graph_traversal_service.py`
- `backend/app/tests/integration/test_graph_traversal_api.py`

### 测试
- 单元：mock `asyncio.to_thread` 断言参数透传正确；异常仍映射为 `NotFoundError` / `ValueError`。
- 集成：真实 Neo4j + 真实 PG，3 跳链路仍然返回 34 hops；并发 5 个请求总耗时 < 串行 5 倍。

### 验收
- `pytest app/tests/unit/test_graph_traversal_service.py app/tests/integration/test_graph_traversal_api.py` 全绿。
- 全量后端覆盖率仍 ≥ 80%。

---

## G2：Token 审计 prompt/completion 拆分

### 问题
Agent/Tool 路径通过 `_recordDirectUsage` 落库，但只传 `tokens_used` 合计，`prompt_tokens` 填合计、`completion_tokens` 填 0。`AgentRunRead` / `ToolResult` 也只有 `tokens_used`，丢失 LLM 拆分量。

### 目标
Agent 运行时产生的 token 消费与 `SessionTokenUsage` 表一样，能区分 prompt / completion。

### 方案
1. 扩展 `ToolResult` 与 `AgentRunRead`：
   ```python
   prompt_tokens: int = 0
   completion_tokens: int = 0
   ```
2. `SupplierRiskService._generateRiskPoints` 返回 prompt / completion 拆分（它内部已读取 `LlmResponse.promptTokens/completionTokens`），`assess()` 透传到 `SupplierRiskRead`。
3. `SupplierRiskRead` 扩展 `prompt_tokens` / `completion_tokens`；`_supplierRiskHandler` 填到 `ToolResult`。
4. `ChatService._recordDirectUsage` 签名改为接收 `prompt_tokens` / `completion_tokens` / `total_tokens`，透传给 `TokenUsageService.recordUsage`。
5. 未拆分的旧调用点（如 supplier_360 / graph_traverse 无 LLM）保持 `completion_tokens=0`。

### 关键文件
- `backend/app/services/agent_tools.py`
- `backend/app/domain/schemas.py`
- `backend/app/services/supplier_risk_service.py`
- `backend/app/services/chat_service.py`
- `backend/app/tests/unit/test_agent_runtime_service.py`
- `backend/app/tests/integration/test_chat_agent_run.py`

### 测试
- 单测：`test_record_direct_usage_saves_prompt_completion_split`
- 集成：`test_chat_supplier_risk_records_prompt_completion_split` 断言 `token_usage` 表 prompt > 0 / completion ≥ 0
- 回归：supplier_360 / graph_traverse 无 LLM 时 completion = 0

### 验收
- `pytest app/tests/integration/test_chat_agent_run.py` 全绿。
- 数据库 `session_token_usage` 行在 Agent run 后 `prompt_tokens` / `completion_tokens` 不再错位。

---

## G3：图遍历 Chat 路径支持 > 2-hop

### 问题
`traverseForChat` 硬编码 `_CHAT_DEFAULT_HOPS = 2`，用户问「供应商 100001 的 3 跳关联」无法按预期加深。

### 目标
Chat 图推理问法可携带跳数；无跳数时默认 2，有跳数时按语义解析 1..5。

### 方案
1. 在 `intent_service.py` 扩展 `_SUPPLIER_GRAPH_RE`，新增跳数捕获组：
   - 「3 跳」「三跳」「深度 3」「最多 3 跳」→ 提取数字
2. `IntentResult` 新增 `max_hops: int | None = None`。
3. `ChatService._handleGraphReasoning` 读取 `max_hops`，默认 2，上限 5，调用 `GraphTraversalService.traverse("Supplier", key, maxHops)`。
4. 前端 `GraphTraversalCard` 显示当前 `maxHops`。

### 关键文件
- `backend/app/services/intent_service.py`
- `backend/app/domain/schemas.py`（IntentResult / ChatResponse）
- `backend/app/services/chat_service.py`
- `frontend/src/components/chat/GraphTraversalCard.tsx`
- `backend/app/tests/unit/test_intent_service.py`
- `backend/app/tests/integration/test_graph_traversal_api.py`

### 测试
- 单测：
  - 「供应商 100001 涉及哪些物料」→ `max_hops=2`
  - 「供应商 100001 的 3 跳关联」→ `max_hops=3`
  - 「供应商 100001 深度 5」→ `max_hops=5`
  - 「供应商 100001 10 跳关联」→ 被 clamp 到 5
- 集成：Chat 问 3 跳，返回的 `graph_traversal.max_hops == 3`

### 验收
- intent 单元与 graph traversal 集成测试通过。
- 越界跳数在 service 层被 clamp，不抛 500。

---

## G4：Chat 未指名 Agent 的语义路由

### 问题
当前只有用户显式写出 `*_AGENT` 才触发 `AGENT_RUN`；普通问法如「评估供应商 100001 风险」仍走 NL2SQL，不会自动调用风险 Agent。

### 目标
根据问题语义和 Agent 注册元数据，自动推荐并调用最合适的 Agent（不指名也能命中）。

### 方案
1. 新增 `AgentRoutingService`：
   - 输入：`message: str`
   - 输出：`recommended_agent_code: str | None` + `confidence: float`
   - 策略 V1：关键词匹配（问题分词 + Agent `data_domains` / `description` / `data_layers` 关键词）+ 实体存在性校验（如含 supplier key 才推荐 supplier 类 Agent）。
2. 在 `IntentService.classifyResult` 中，若未显式指名 Agent，调用 `AgentRoutingService`；
   - 高置信度（≥ 0.7）→ `AGENT_RUN`
   - 中置信度（0.4..0.7）→ 仍走 `QUERY`，但在 `ChatResponse` 返回 `suggested_agent` 提示卡片
   - 低置信度 → 原路径
3. 前端在 `ChatPage` 展示 `suggested_agent` 卡片，用户可一键触发。

### 关键文件
- `backend/app/services/agent_routing_service.py`（新建）
- `backend/app/services/intent_service.py`
- `backend/app/domain/schemas.py`
- `frontend/src/components/chat/SuggestedAgentCard.tsx`（新建）
- `frontend/src/stores/chatStore.ts`
- `backend/app/tests/unit/test_agent_routing_service.py`（新建）
- `backend/app/tests/integration/test_chat_agent_run.py`

### 测试
- 「评估供应商 100001 风险」→ SUPPLIER_RISK_AGENT
- 「供应商 100001 的 360° 视图」→ SUPPLIER_360_AGENT
- 「供应商 100001 关联哪些物料」→ GRAPH_REASONING_AGENT
- 「今年采购金额」→ 不路由到 Agent（保持 NL2SQL）

### 验收
- 高置信度语义路由集成测试通过。
- NL2SQL 查询不被误吸（3 个负向护栏测试）。

---

## G5：Agent 批量调度（SCHEDULED trigger）

### 问题
`AgentTriggerType.SCHEDULED` 枚举已存在，但无 schedule 存储、无调度器、无 API，仅支持单次 REST/Chat 触发。

### 目标
允许为 Agent 配置 cron 表达式，到点自动触发并记录运行结果与 Token 消耗。

### 方案
1. 数据模型：新增 `agent_schedule` 表
   ```python
   id, agent_code(FK), cron_expression, params(jsonb), is_active,
   last_run_at, next_run_at, created_time, updated_time
   ```
   Alembic 迁移 `0032_agent_schedule.py`。
2. Service：`AgentSchedulerService` 封装 CRUD + 启动/停止任务。
3. 调度器：使用 `APScheduler`（`AsyncScheduler`）或 `croniter` + `asyncio.Task`。
   - 本期推荐 `APScheduler`，已成熟且支持 cron。
   - 触发时调用 `AgentRuntimeService.run(session, code, params)`。
4. API：
   - `POST /agents/{code}/schedules`
   - `GET /agents/{code}/schedules`
   - `PATCH /agents/{code}/schedules/{id}/toggle`
   - `DELETE /agents/{code}/schedules/{id}`
5. 安全：仅 owner 部门 / admin 可管理；schedule 触发时 actor = "scheduler:{schedule_id}"。

### 关键文件
- `backend/alembic/versions/0032_agent_schedule.py`
- `backend/app/domain/models.py`
- `backend/app/domain/schemas.py`
- `backend/app/services/agent_scheduler_service.py`（新建）
- `backend/app/api/v1/agent_runtime.py`
- `backend/app/tests/integration/test_agent_scheduler_api.py`（新建）
- `frontend/src/pages/AgentRegistryPage.tsx`（增加 schedule 面板）

### 测试
- 单元：`AgentSchedulerService` cron 解析、next_run_at 计算
- 集成：创建 schedule → 手动触发 → 断言 `agent_run` 结果落库
- 安全：非 owner 角色 403

### 验收
- `alembic upgrade head` 成功。
- schedule 到点自动执行，运行结果与 token 消耗写入 DB。
- 全量后端覆盖率 ≥ 80%。

---

## G6：data_layers 注解防漂移

### 问题
`AgentTool.data_layers` 是人工注解，新增工具时可能写错大小写、遗漏层、或与实际 handler 读取的数据源不一致。

### 目标
在注册/测试阶段强制校验 `data_layers` 格式，并通过契约测试防止工具声明与实际行为漂移。

### 方案
1. 格式校验：`AgentToolRegistry.register` 增加：
   - `data_object` 必须全大写
   - `data_layers` 元素必须非空、全大写
   - `data_layers` 为空时必须附带 `# layer-agnostic: ...` 注释（运行时检查跳过，CI lint 可扫描）
2. 契约测试：新建 `tests/unit/test_agent_tool_layer_contract.py`
   - 对 3 个内置工具，断言 `tool.data_layers` 与 handler 实际调用的 service 读取的层一致：
     - `supplier_360` / `supplier_risk` → `Supplier360Service.get360` 读 `EntityMapping(DIM)` + `FeatureValue(FEATURE)`
     - `graph_traverse` → `GraphTraversalService` 读 Neo4j 业务实体子图（`DIM + DWD`）
3. 启动自检（可选）：`AgentRuntimeService.__init__` 调用 `registry.validate()`，若格式违规直接抛 `ConfigError`，阻止服务启动。

### 关键文件
- `backend/app/services/agent_tools.py`
- `backend/app/services/agent_runtime_service.py`
- `backend/app/tests/unit/test_agent_tool_layer_contract.py`（新建）
- `backend/app/domain/exceptions.py`（新增 `ConfigError` 已存在则复用）

### 测试
- 注册非法大小写 layer → `ValueError`
- 注册空元素 layer → `ValueError`
- 内置 3 工具契约测试通过

### 验收
- 新增工具未通过校验时单测失败，阻止合并。
- 全量后端测试通过。

---

## 通用验收（每个 change 都需要）

1. **TDD**：先写测试（RED）→ 实现（GREEN）→ 重构（IMPROVE）。
2. **覆盖率**：后端 `pytest app/tests/ --cov=app --cov-fail-under=80` 通过。
3. **前端**：如有前端改动，`tsc --noEmit` 0 errors + vitest 不回归。
4. **双审**：`code-reviewer` + `security-reviewer` 通过。
5. **SSOT**：每个 change 在 `Harness/changes/feat-<name>/summary.md` 记录需求/设计/测试/验证。
6. **commit**：按 `feat:` / `fix:` 规范提交。

---

## 下一步建议

建议从 **G1（Neo4j 异步化）** 开始：改动最小、独立性强、安全收益明确；完成后直接验证图遍历并发性能提升。