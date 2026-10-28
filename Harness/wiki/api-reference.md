# API 参考

基址：`/api/v1`。JSON 字段为 camelCase（如 `modelName`、`sessionId`、`tokenUsage`）。

## 系统
| Method | Path | 说明 |
|--------|------|------|
| GET | /health | 健康检查 |

## 模型配置
| Method | Path | 说明 |
|--------|------|------|
| GET | /models | 列出（`?activeOnly=true`） |
| POST | /models | 创建（含明文 apiKey，服务端加密） |
| GET | /models/{id} | 获取 |
| PUT | /models/{id} | 更新 |
| DELETE | /models/{id} | 停用（软删除） |

## 会话
| Method | Path | 说明 |
|--------|------|------|
| GET | /sessions/{sessionId}/usage | 聚合摘要（请求数/Token/成本/按模型） |
| GET | /sessions/{sessionId}/usage/list | 明细流水 |

## 后续 Phase
| Method | Path | Phase |
|--------|------|-------|
| POST | /chat | 4 |
| POST | /knowledge/{define,map,metric} | 2 |
| GET/POST | /ontology/{classes,properties,metrics} | 2 |
| GET | /ontology/search | 2 |
| GET/POST | /datasource | 3 |
| POST | /datasource/{id}/test | 3 |
| POST | /datasource/{id}/query | 3 |
| GET/POST/PUT/DELETE | /data-quality/rules | 8 |
| POST | /data-quality/evaluate | 8 |
| POST | /data-quality/scores/compute | 9 |
| GET/POST | /data-quality/reports | 9 |
| GET | /data-quality/reports/{report_id} | 9 |
| GET | /data-quality/reports/{report_id}/progress | 9 |
| POST | /data-quality/reports/{report_id}/regenerate | 9 |
| DELETE | /data-quality/reports/{report_id} | 9 |

## 错误响应

统一 `ErrorResponse`：`{ success: false, error: <message>, detail: <optional> }`。

| 异常 | HTTP |
|------|------|
| NotFoundError | 404 |
| ValidationError | 422 |
| 其他 DomainError | 400 |

## Phase 9 增量：数据质量评分 / 评估报告

**日期**：2026-09-15 · **变更**：[feat-dq-scores-multiselect](../changes/feat-dq-scores-multiselect/summary.md) · [feat-dq-evaluation-report](../changes/feat-dq-evaluation-report/summary.md) · [feat-dq-evaluation-report-progress](../changes/feat-dq-evaluation-report-progress/summary.md)

### POST /data-quality/scores/compute

```typescript
// Request
{
  datasourceId: number,             // 单选，必填
  targetTables?: string[],         // 多选；null/[] = 全表
  ruleTypes?: RuleType[],          // 多选；null/[] = 全规则类型
  timeWindow?: { start: string, end: string }
}
// Response: { rows: ScoreRow[], total: number }
```

`datasourceId` 单选；`targetTables` / `ruleTypes` 多选，AND 组合三条件。

### POST /data-quality/reports（异步）

```typescript
// Request
{
  name: string,
  datasourceId: number,
  classIds: number[],              // 多对象
  rules: { classId: number, ruleIds: number[] }[],
  timeWindowStart: string,
  timeWindowEnd: string
}
// Response: { id, status: 'PENDING', ... }   // 立即返回，后台跑 snapshot
```

提交后**立即**返回 `status: 'PENDING'` 的报告行；不再同步等待 snapshot 生成。

### GET /data-quality/reports/{report_id}/progress

```typescript
// Response
{
  stage: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED',
  completed: number,
  total: number,
  current_rule_id: number | null,
  current_rule_code: string | null,
  message: string | null,
  started_at: string | null,
  finished_at: string | null
}
```

注意：**该路由必须注册在 `/reports/{report_id}` 之前**，否则会被路径参数匹配吞掉。

### 状态机

| status | 含义 | 是否终态 |
|--------|------|----------|
| DRAFT | 草稿（用户编辑中） | 否 |
| PUBLISHED | 已发布（业务可见终态） | 是 |
| PENDING | 排队等待后台任务拉起 | 否 |
| RUNNING | snapshot 进行中 | 否 |
| COMPLETED | snapshot 完成 | 是 |
| FAILED | snapshot 失败（progress.message 有原因） | 是 |

## Phase 6.6 增量：通用多工具 Agent 编排

**日期**：2026-10-28 · **变更**：[feat-agent-orchestration](../changes/feat-agent-orchestration/summary.md)
· **Wiki**：[[agent-runtime]]

> **开关语义（先读这条）**：`ENABLE_AGENT_ORCHESTRATION` 默认 `false`。
> 关闭时**不注册**下列路由 ⇒ 请求得到 **404**（不是 401 —— 路由不存在就没有鉴权层）。
> 打开后但没带 token ⇒ **401**。两者含义完全不同，别混。

### 配置面 `/api/v1/agent-orchestrations`

| Method | Path | 成功码 | 说明 |
|---|---|---|---|
| POST | `/api/v1/agent-orchestrations` | **201** | 建 profile（**创建后一律 `enabled=false`**，需显式 toggle） |
| GET | `/api/v1/agent-orchestrations` | 200 | 列当前用户可见的 profile |
| GET | `/api/v1/agent-orchestrations/tool-options` | 200 | 列**当前有效**的可编排工具（**不含 `handlerRef`**） |
| GET | `/api/v1/agent-orchestrations/{agentCode}` | 200 | 查单个 profile（⚠️ **此端点没有 owner/admin 过滤**，列表端点有 —— 已知限制） |
| PUT | `/api/v1/agent-orchestrations/{agentCode}` | 200 | 全量更新（**必须带 `revision`**，乐观锁） |
| POST | `/api/v1/agent-orchestrations/{agentCode}/toggle` | 200 | 显式启停（body `enabled` + `revision`） |

```typescript
// POST /api/v1/agent-orchestrations
{
  agentCode: string,              // ≤64
  instructions: string,           // ≤10000
  toolNames: string[],            // 默认 []
  defaultMaxIterations?: number,  // ≥1，默认 20
  defaultMaxToolCalls?: number,   // ≥1，默认 30
  defaultMaxCostUsd?: number,     // ≥0，默认 5.0
  defaultTimeoutSeconds?: number, // ≥1，默认 900
  maxObservationChars?: number,   // 1000..50000，默认 50000
}
// → 201
{ agentCode, instructions, enabled, defaultMaxIterations, defaultMaxToolCalls,
  defaultMaxCostUsd, defaultTimeoutSeconds, maxObservationChars, revision,
  bindings: [{ toolName, enabled, sortOrder }] }
```

- 请求模型 `extra="forbid"` ⇒ 多带一个字段就 **422**（不静默忽略）。
- **系统上限不在 DTO 里重复**，由 service 层按 `Settings` 校验（`assertWithinSystemCeilings`）
  ⇒ 越界是 **422**，不是 400。
- `tool-options` **只列当前有效的工具**（`enabled=true` **且** `input_schema` 合法
  **且** 在只读白名单内）⇒ 阶段 0 还没补 schema 时它是空的。

### 运行面 `/api/v1/agent-runs`

| Method | Path | 成功码 | 说明 |
|---|---|---|---|
| POST | `/api/v1/agent-runs` | **202** | 建 run ⇒ `{ runId, status: "QUEUED" }`（**同步返回，不等执行完**） |
| GET | `/api/v1/agent-runs` | 200 | 列**当前用户自己**的 run（`agentCode` / `status` / `limit` / `offset` 可选） |
| GET | `/api/v1/agent-runs/{runId}` | 200 | 查 run 详情 |
| GET | `/api/v1/agent-runs/{runId}/steps` | 200 | 查轨迹（`step_no` 升序） |
| POST | `/api/v1/agent-runs/{runId}/resume` | **202** | 恢复 `PAUSED` 的 run ⇒ 回到 `QUEUED` |
| POST | `/api/v1/agent-runs/{runId}/cancel` | 200 | 取消（返回当前摘要） |

```typescript
// POST /api/v1/agent-runs
{
  agentCode: string,
  input: string,                 // ≤20000
  modelConfigId: number,         // ≥1
  limits?: { maxIterations?, maxToolCalls?, maxCostUsd?, timeoutSeconds? }
}
// → 202 { runId: string, status: "QUEUED" }
```

- `GET /{runId}/steps` 的 `afterStepNo` 是**闭区间下界**（返回 `step_no >= afterStepNo`），
  前端轮询用 `afterStepNo = lastStepNo + 1` 只取增量。
- `resume` 的必填项**由持久化的 `pause_code` 决定**（不是由 DTO 决定）：
  `NEEDS_INPUT` / `AMBIGUOUS` 必须带 `input`；
  `BUDGET_EXHAUSTED` 必须带 `additionalIterations` / `additionalToolCalls` /
  `additionalCostUsd` / `additionalTimeoutSeconds` 之一。
  缺了 ⇒ **422**。
- `cancel`：`QUEUED` / `PAUSED` 立即取消；`RUNNING` 只标记 `cancelRequested = true`
  （**不伪装成已终止**）。
- `POST /agent-runs`、`/resume`、`/cancel` **带限流**。

### 编排的状态机

| status | 含义 | 是否终态 |
|---|---|---|
| QUEUED | 排队等 Worker 领 | 否 |
| RUNNING | Worker 正在执行（持有租约） | 否 |
| PAUSED | 因 `pause_code` 暂停，可 `resume` | 否 |
| SUCCEEDED | 正常完成 | 是 |
| FAILED | 失败（`errorCode` / `errorMessage` 有原因） | 是 |
| CANCELLED | 已取消（`cancelRequested = true`） | 是 |

`pause_code` 取值：`NEEDS_INPUT` / `AMBIGUOUS` / `PERMISSION_DENIED` /
`BUDGET_EXHAUSTED` / `TOOL_FAILED` / `PLANNER_OUTPUT_INVALID` /
`AGENT_UNAVAILABLE` / `INFRASTRUCTURE_ERROR`。

### 编排端点的 404 / 409 / 422 语义

| 情况 | HTTP | 说明 |
|---|---|---|
| Agent 不存在 / 无编排配置 | **404** | `NotFoundError` |
| 访问**别人的** run（且不是 admin） | **404** | 与「不存在」同码 ⇒ 不泄露存在性 |
| 编排配置已禁用 / Agent 不可用 / 无可用工具绑定 / 快照工具已不可用 / `revision` 冲突 | **409** | `ConflictError` |
| `toolNames` 引用了不存在的工具 | **422** | `ValidationError` |
| 绑定到**已禁用**的工具 | **422** | 文案「工具已禁用：\<name\>」 |
| 工具的 `input_schema` 不合法（缺 `type: object` 等） | **422** | `validateObjectSchema` |
| 预算/上限越界、resume 缺必填字段 | **422** | `ValidationError` |

> **权限被 ACL 拒绝不是 403**：编排路径把 `PermissionDeniedError` 转成
> **暂停**（`pause_code = PERMISSION_DENIED`），不把策略内部信息（data_object /
> data_layer）泄进 Planner 的 Observation（`agent_orchestration_tool_executor.py:252-261`）。
> 处置方式是 `resume` 前调整授权，而不是重试。

### DELETE /api/v1/agent-tools/{name} 的 409 与 `detail` 形状

| 情况 | HTTP | `error` 文案 |
|---|---|---|
| 工具被 legacy Agent（`agent_definition.tool_name`）引用 | **409** | `agent_tool_config 被 Agent 引用，无法删除: <name>` |
| 工具被编排 profile 绑定（`agent_orchestration_binding.tool_name`） | **409** | 同上（**文案不变**） |

`detail` 是 `; ` 分隔的**字符串**（不是 JSON 对象）。新增的稳定 code 段
`agent_tool_in_use_orchestration` 标识「编排引用」这一路：

```
agent_tool_in_use_orchestration; referencingAgents=['legacy_agent']; orchestrationAgents=['orch_agent']
```

- 第一段恒为 `agent_tool_in_use_orchestration`（稳定 code，可用于分支判断）。
- `referencingAgents=[...]`：**legacy** Agent codes 列表，**只有非空时才有这一段**。
- `orchestrationAgents=[...]`：编排 profile 绑定该工具的 Agent codes 列表
  （恒存在 —— 至少有一个编排引用才会走这一路）。
- 编排冲突这一路的两个列表都**排序后**输出；`detail` **不含**工具配置载荷
  （不泄露 `input_schema` 等）。
- 编排引用**优先判定**：同时被 legacy Agent 与编排 profile 引用时走上面这条
  `agent_tool_in_use_orchestration` 形状（两段都在）。
- 编排特性开关（`ENABLE_AGENT_ORCHESTRATION`）**关闭时完全不查**编排引用 ⇒
  `detail` 退回旧的 `referencingAgents=[...]` 形状（无 code 段）。
- 开关打开但**目标库还没升到 0135**（编排表不存在）时，编排引用检查 **fail-soft**
  ⇒ 视为「无编排引用」，同样退回旧形状，而不是 500。

> 事实源：`backend/app/services/agent_tool_config_service.py` 的
> `_OrchestrationToolInUseConflict`（detail 组装）与
> `backend/app/api/v1/agent_tools.py` 的 `DELETE /api/v1/agent-tools/{name}` 路由。
