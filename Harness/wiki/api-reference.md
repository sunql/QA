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

## A1 增量：统一 ID 映射 `/api/v1/id-mappings`

**日期**：2026-10-31 · **变更**：[m0-id-mapping-a1](../changes/m0-id-mapping-a1/summary.md)
（前次：[id-mapping-dto-tighten](../changes/id-mapping-dto-tighten/summary.md)、
[id-mapping-admin-acl](../changes/id-mapping-admin-acl/summary.md)）

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `GET /api/v1/id-mappings?businessObject=&limit=&offset=` | **admin** | 整表/按业务对象列举；`limit` 1–1000，默认 200 |
| `POST /api/v1/id-mappings` | **admin** | 请求体 `{businessObject, externalId}` → **201**；重复 → **409** |
| `GET /api/v1/id-mappings/{unified_id}` | 任意登录用户 | 单点读；不存在 → **404** |
| `PUT /api/v1/id-mappings/{unified_id}` | **admin** | partial，运维改三库指针；不存在 → **404** |
| `DELETE /api/v1/id-mappings/{unified_id}` | **admin** | → **204**；再次 GET → 404 |

`POST` 语义：

- `unifiedId` 由服务端派生 `obj:{businessObject}:{externalId}`，**不接受**客户端传
  `unifiedId`；`businessObject` / `externalId` 去空白 + NFC 归一后落库。
- 任何跨库指针字段（`pgTable` / `pgId` / `neo4jNodeId` / `milvusCollection` /
  `milvusId`）→ **422**（`extra="forbid"`，不是静默丢弃）：这三库列是三库对齐的
  结论，不是调用方可声明的输入。
- 分段含 `:`、控制字符、零宽/格式字符、代理项、行分隔符，或超列宽（32 / 128 / 128）
  → **422**（`ValidationError` → 域错误 handler）。
- `(businessObject, externalId)` 重复 → **409**。

> 事实源：`backend/app/api/v1/id_mapping.py`、`backend/app/domain/id_mapping.py`、
> `backend/app/domain/schemas.py`（`IdMappingCreate` / `IdMappingUpdate`）。

## 本体发现（WP-ONT-AI A1）

**日期**：2026-11-16 · **变更**：[2026-11-16-ontology-discovery-ai](../changes/2026-11-16-ontology-discovery-ai/summary.md)
前缀 `/api/v1/ontology-discovery`；**发现域只写自己的表**（`discovery_source` /
`metadata_snapshot`），不写治理表。全部端点 `Cache-Control: no-store`。

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `POST /api/v1/ontology-discovery/sources` | **admin** | 登记发现源 → **201**；重名 → **409**；策略非法 → **422** |
| `PATCH /api/v1/ontology-discovery/sources/{sourceId}` | **admin** | 改 `status` / `owner` / `access_policy` / `note`；`datasource_id` 与 `name` **不可改**（改 `datasource_id` = 换一条被授权的连接） |
| `GET /api/v1/ontology-discovery/sources?limit=&offset=` | 任意登录用户 | 发现源列表（附各源快照数，一次 GROUP BY 取回，不做 N+1） |
| `GET /api/v1/ontology-discovery/snapshots?sourceId=&limit=&offset=` | 任意登录用户 | 快照列表（新→旧）；快照只增不改 ⇒ 列表即历史 |
| `GET /api/v1/ontology-discovery/snapshots/{snapshotId}` | 任意登录用户 | 快照详情（含 `snapshot` 内容）；不存在 → **404** |
| `POST /api/v1/ontology-discovery/snapshots?sourceId=` | **admin** | 采集快照（内容寻址：内容未变 ⇒ `reused=true`，不新增行） |
| `GET /api/v1/ontology-discovery/diff?domainCode=` | 任意登录用户 | 基线（已发布 ACTIVE）vs 当前治理表的差异 |

**`/diff` 的覆盖度披露**（避免「无破坏性变更」被误读成「全量无破坏」）：

- `scope` 恒为 `GLOBAL`：`domainCode` **只用于定位基线**，不裁剪候选侧（候选是全域编译结果）；
- `covered_sections` / `uncovered_sections` / `partial`：当前差异引擎只比
  **classes / properties / joins** 三分区 ⇒ metrics/kpis/domains/businessObjects **未覆盖**；
- `change_count`（本次返回条数）/ `total_change_count`（真实总数）/ `truncated`
  （上限 500 条）⇒ 大清单下响应体可控且不隐瞒总量；
- `has_breaking_changes` / `overall_risk_level`：`BREAKING→HIGH` / `ADDITIVE→LOW` /
  `COSMETIC→INFO`。

**审计**：三个写端点（登记 / 更新 / 采集）都落 `audit_log`（entity_type =
`discovery_source` / `metadata_snapshot`，含 before / after 摘要）。

> 事实源：`backend/app/api/v1/ontology_discovery.py`、
> `backend/app/domain/discovery_schemas.py`。

## 知识服务 Facade（P4）

**日期**：2026-11-16 · **变更**：[feat-knowledge-facade](../changes/feat-knowledge-facade/summary.md)
开关 `ENABLE_KNOWLEDGE_FACADE`（**默认 false** ⇒ 整组路由不注册 ⇒ 404 ⇒ 未部署时对外不存在）。
只读端点、裸 DTO（**不套 `{success,data}` 信封**）、`Cache-Control: no-store`。
鉴权 = 任意登录用户（+ 改密守卫 + 限流）——**域级可见性由服务层权限口径判定**，
不可见 = **200 + 空结果 + 警告**，**不是 403**（不侧漏域是否存在）。

| 端点 | 说明 |
|---|---|
| `POST /api/v1/semantic/knowledge/query` | 跨域通用知识查询：`domain` 是**唯一的域参数**（换 domain 即服务第二个域，零代码改动） |
| `POST /api/v1/procurement/knowledge/query` | **采购薄别名**：请求体少一个 `domain`（固定 `PROCUREMENT`），其余逐字一致；**别名文件里没有任何业务分支**（AST 守卫 + 运行时等价判据） |

**请求**（主接口）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `domain` | str | ✅ | 数据域 code |
| `question` | str | ✅ | 1..500 字 |
| `channels` | list[str] | ✖ | 缺省 = 四路全开；`GRAPH` / `METRIC` / `RAG` / `SQL` |
| `entityHints` | list[{type,code}] | ✖ | 实体提示（**只提示，不放宽权限**） |
| `topK` | int | ✖ | 1..50，缺省 8 |
| `releaseId` | int | ✖ | 钉住某版发布；缺省 = 该域 ACTIVE（无则**降级为空 + 显式标记**）。⚠️ 钉住的发布**不可见**（域不匹配 / 非 admin 看不到该状态）⇒ 同样降级为空，且**不区分「不存在」与「无权」** |
| `scope` | {orgCodes, entityTypes, maxSecurityLevel} | ✖ | **只收窄**（不得放宽）。`entityTypes` **真的收窄**（请求侧 ∩ 可见集 ⇒ 空则空结果 + 警告）；`maxSecurityLevel` **本批不被消费**（用户级安全等级无权威来源 ⇒ 显式告警） |

**响应**：`question` / `domain` / `release` `{id, version, manifestHash, degraded}` /
`answerDraft`（**确定性编排摘要，非 LLM**）/ `entities[]` / `evidence[]`
`{channel, kind, ref, title, snippet, releaseId, releaseVersion, source}` /
`routeResults[]` `{channel, status, itemCount, degradedReason, warnings[]}` / `warnings[]`。

**四路状态语义**（`routeResults[].status`）：

| 状态 | 含义 |
|---|---|
| `OK` | 查到了 |
| `EMPTY` | 查了但没命中（**与 `DENIED` 分开**） |
| `DEGRADED` | 能力不可用（如 RAG 闸门关）⇒ 带 `degradedReason` |
| `DENIED` | **域不可见**（无权查）⇒ 零证据 + 警告；一路都不跑，且**连发布面都不解** |

**引用可回溯**：发布读 ⇒ 每条证据带 `releaseId` + `releaseVersion` + `source=RELEASE`。
**降级不是「无版本的证据」**：降级 ⇒ **零证据** + `release.degraded=true` + 警告
（**不直读治理表** ⇒ 证据永远有版本可引；`source=LIVE` 是**保留常量，本批不产出**）。

> 事实源：`backend/app/api/v1/knowledge_facade.py`、
> `backend/app/api/v1/procurement_knowledge.py`、
> `backend/app/domain/knowledge_schemas.py`、
> `backend/app/services/knowledge/`（编排 + 权限 + 域切片 + 四路）。

## 采购源绑定（M1-1.1）

**日期**：2026-11-18 · **变更**：[2026-11-18-m1-procurement-binding-service](../changes/2026-11-18-m1-procurement-binding-service/summary.md)
前缀 `/api/v1/procurement-bindings`；JSON 为 camelCase；写入口全部 **admin**。
**新建一律落候选**（D-3）⇒ 候选不得进入任何读路径。

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `GET /api/v1/procurement-bindings` | **admin** | 全量列表（含候选与已失效）；可选 `classId` / `datasourceId` / `effectiveOnly` |
| `GET /api/v1/procurement-bindings/effective` | 任意登录用户 | **生效视图**：已确认且 `valid_to IS NULL` —— 候选与失效行一律不出现 |
| `GET /api/v1/procurement-bindings/effective/properties` | 任意登录用户 | **生效的属性映射**（读路径唯一入口）：自身已确认且生效 **且父已确认且生效** |
| `GET /api/v1/procurement-bindings/{id}` | **admin** | 详情 = 绑定本体 + 其属性绑定列表；不存在 → **404** |
| `POST /api/v1/procurement-bindings` | **admin** | 新增**候选**源绑定 → **201**；同键位重复 → **409** |
| `POST /api/v1/procurement-bindings/{id}/properties` | **admin** | 新增**候选**属性绑定 → **201**；重复属性 / 键序号 → **409**；父已确认 → **409**（子集合冻结） |
| `POST /api/v1/procurement-bindings/{id}/confirm` | **admin** | 源绑定确认（候选 → 已确认）；重复确认 → **409**；属性未全部确认 → **422** |
| `POST /api/v1/procurement-bindings/{id}/expire` | **admin** | 软失效（写 `valid_to`，不删行）；重复失效 → **409**；**级联失效子绑定并逐行写审计** |
| `POST /api/v1/procurement-bindings/{id}/properties/{propertyBindingId}/confirm` | **admin** | 属性绑定确认（候选 → 已确认）；路径父 id 与归属不符 → **404** |
| `POST /api/v1/procurement-bindings/{id}/properties/{propertyBindingId}/expire` | **admin** | 单条属性绑定软失效（绑错列不必作废整个源绑定）；路径父 id 与归属不符 → **404** |

**路由顺序**：`/effective` 与 `/effective/properties` 必须声明在 `/{id}` **之前**，否则会被 `/{id}` 吃掉
（`id: int` 解析失败 ⇒ 422 而不是列表）。

**状态机（确认 = 版本冻结点）**

```
候选态（父未确认）──▶ 确认态（父已确认，子集合冻结）──▶ 失效态（只读）
   可增删子、可确认子        只能整体失效（级联子）        不可再写
```

⇒ 父已确认时追加 / 确认 / 失效子绑定一律 **409**；父已失效时改子绑定 **422**。
要改绑必须走「失效现役行 → 建新候选 → 重新确认」。
**锁序固定为 父 → 子**（`SELECT ... FOR UPDATE`）⇒ 并发确认不会 TOCTOU，也不会死锁。

**状态码语义**

| 情形 | 状态码 |
|---|---|
| 非 admin 调写入口 / 全量读视图 | **403** |
| 类 / 数据源 / 属性不存在、属性跨类、过滤白名单不合法、**变换表达式不合法**、键序号不连续、**确认时属性未全部确认**、**父已失效时改子绑定**、**actor 超长**、**键序号越 SMALLINT** | **422** |
| 同键位重复、重复确认、重复失效、**父已确认时改子集合（冻结）** | **409** |
| 绑定 id 不存在、**路径父 id 与属性绑定归属不符** | **404** |

⚠️ 「属性绑定不存在」与「属性绑定不属于该父」**共用一句文案**（「不存在或不属于源绑定 id=Y」）
⇒ 不给存在性探测留口子。

⚠️ **数据库异常按约束类型分流**：唯一约束（`uq_*`）⇒ **409**；
CHECK（`ck_*`）/ 外键 / 非空 ⇒ **422**（用户改数据即可解决，重试无用）；
其余 `DBAPIError`（长度超限 / 数值越界）⇒ **422**。
⚠️ commit 路径**也**走翻译器 —— 延迟约束在 commit 时才触发，裸 commit ⇒ 500。
约束名**只进服务端日志**，用户只看到归一化提示。
⚠️ **有效期窗口父子嵌套**：级联失效时父行的失效时刻会**后移**到
`max(父原失效时刻, 各子行最早合法失效时刻)` ⇒ **子行的 `validTo` 恒 ≤ 父行的 `validTo`**。

**过滤表达式的语法**（`filterExpression`）

```
<裸列> <比较符> <常量>            ← 必须至少有一处
以 AND / NOT / 括号连接（OR 禁）
函数只能出现在受控清单里，且不能单独构成一条过滤
```

顶层只有 AND / NOT ⇒ 合取式；只要有一项依赖行 ⇒ 整个过滤依赖行 ⇒ 不可能恒真。

**请求体要点**

- `SourceBindingCreate`：`classId` / `datasourceId` / `schemaName` / `sourceTable` /
  `bindingRole`（`BASE|NUMERATOR|DENOMINATOR|FILTER`）/ `filterExpression` / `filterAllowlist`。
  ⚠️ **给了 `filterExpression` 就必须给非空 `filterAllowlist`**（不给 = 默认放行 = 越权读 ⇒ 422），
  且表达式用到的每个**列**标识符都要在白名单内（大小写不敏感；字符串字面量整段剥掉）。
  ⚠️ 白名单项**必须是合法标识符且 ≤100 字符** —— PostgreSQL 对 `ARRAY(String(100))`
  的**元素超长是静默截断**（不报错）⇒ 写时校验的白名单与落库的白名单会不是同一个对象。
  ⚠️ **三张互不重叠的授权清单**（白名单只授权**列**）：
  | 清单 | 授权什么 | 谁来填 |
  |---|---|---|
  | `filterAllowlist` | **列**（数据） | 写入方自报 |
  | `SAFE_SCALAR_FUNCTIONS`（21 个） | **函数**（代码） | **系统内置，不可自报** |
  | 保留字禁表（37 词） | **结构词** —— 表达式里不许出现的 SQL 结构 | 系统内置 |
  ⇒ 白名单**自报** `SELECT` / `FROM` 也塞不进子查询；自报 `PG_SLEEP` 也调不动函数。
  ⚠️ 过滤表达式**禁 `OR`**（`X = 1 OR 1=1` 恒真 ⇒ 行级过滤被抹平成全表读）；
  禁 `OR` 只在**剥掉字面量后**的文本上匹配 ⇒ `STATUS = 'A OR B'` 不误杀。
  ⚠️ 过滤表达式**必须至少含一处「裸列 ↔ 常量」的直接比较**（**恒真谓词闸门**）——
  禁 `OR` 只堵住恒真的一种写法，`A = A` / `ABS(A) >= 0` / `CASE ... ELSE 1 END = 1` /
  `UPPER(A) = 'X'` 照样恒真或与行无关 ⇒ 顶层只有 AND / NOT 时，只要有一项依赖行，
  **整个合取式就依赖行** ⇒ 恒真从**语法**上不可达。
  ⇒ 代价（刻意取舍）：**只有函数比较**的表达式不被接受（`UPPER(A) = 'X'` ⇒ 422），
  需要函数时**另配一处裸列比较**：`STATUS = '有效' AND UPPER(NAME) = 'X'`。
  ⚠️ 过滤表达式**禁 `CASE`**（`ELSE` 分支恒为 1，NULL 也走 ELSE ⇒ 恒真）。
  ⚠️ 结构词禁表含 **`TABLE` / `LATERAL`** —— PostgreSQL 的 `TABLE relation` ≡
  `SELECT * FROM relation` ⇒ `EXISTS (TABLE other_tbl)` 不出现 SELECT / FROM 也能跨表读。
  ⚠️ 中文字面量**允许**（业务数据是中文）：共享的 DQ 表达式校验器只认 ASCII ⇒
  送检前把**字面量内部**的非 ASCII 字符掩码成占位符（引号之外仍被拒）。
  ⚠️ 纯空白 `filterExpression` ⇒ 落库为 **NULL**（与「没有过滤」同状态）。
- `PropertyBindingCreate`：`propertyId` / `sourceColumn` / `keyOrdinal`（1..32767，NULL = 非键列）/ `transformExpression`。
  ⚠️ **`transformExpression` 与 `filterExpression` 同级闸门**：禁 DML / UNION / 注释 / `;`、禁 `OR`、
  **列** ⊆ {本属性 `sourceColumn`}、**函数** ⊆ 受控函数清单（21 个）。
  ⚠️ `keyOrdinal` 上限 **32767** —— 库列是 SMALLINT，越界值会在绑定参数阶段抛 DataError ⇒ 500。
  ⚠️ **建时**就校验键序号**前缀**（1..N-1 必须已存在）⇒ 缺位不会拖到「确认」才撞墙。
- **确认请求体不接受确认人**：确认人取当前登录用户（防伪造审计）。
- **actor 长度 ≤64 字符** —— `createdBy` / `confirmedBy` 库列是 `String(64)`，
  超长抛 `StringDataRightTruncationError`（**不是** IntegrityError）⇒ 未处理就是 500 ⇒ 边界先拒 422。

**审计**：写端点全部落 `audit_log`（entity_type = `procurement_source_binding` /
`procurement_property_binding`）；候选→确认复用 **`UPDATE`** 动作（before / after 双端快照）。

> 事实源：`backend/app/api/v1/procurement_bindings.py`、
> `backend/app/services/procurement_binding_service.py`、
> `backend/app/services/procurement_binding_validation.py`。
> 变异反证：`backend/scripts/check_procurement_binding_mutations.py`（证明测试不是「永远绿」）。
