---
created: 2026-09-01
updated: 2026-10-18
---

# Frontend

> 前端架构说明（施工中）。

## 菜单架构（feat-menu-hierarchy）

### 数据流
```
AppLayout mount → fetch /api/v1/menu-config → MenuConfig (DB)
                                              ↓
                            Ant Design Menu + SubMenu
```

### 兜底
- API 失败 → console.warn + setUseFallback(true) → 渲染 fallbackNav.ts
- openKeys 持久化：localStorage["menu.openKeys"]

### 预留字段
- permissionCode: string | null
- roles: string[]

本期不消费这两字段；后续接入 acl_service 时启用。

## Phase 9 增量：数据质量评估报告 UI

**日期**：2026-09-15 · **变更**：[feat-dq-evaluation-report](../changes/feat-dq-evaluation-report/summary.md) · [feat-dq-evaluation-report-progress](../changes/feat-dq-evaluation-report-progress/summary.md) · [feat-dq-scores-multiselect](../changes/feat-dq-scores-multiselect/summary.md)

### 路由

| 路径 | 组件 | 说明 |
|------|------|------|
| `/data-quality?tab=scores` | `DataQualityPage.tsx` (`ScoresTab`) | 单数据源 + 多表 + 多规则类型评分；Select `mode="multiple"` + `maxTagCount="responsive"` |
| `/data-quality/reports` | `DataQualityReportListPage.tsx` | 报告列表 |
| `/data-quality/reports/new` | `DataQualityReportCreatePage.tsx` | 4 步 wizard：选对象 → 每对象选规则 → 组合列表（可删行/删整组）→ 时间窗口 |
| `/data-quality/reports/:id` | `DataQualityReportDetailPage.tsx` | 详情：进度卡（顶部）+ KPI 卡片 + 维度图 + 规则明细 + 违规样本 |

### 进度轮询模式（detail 页）

```typescript
useEffect(() => {
  if (!isInProgress(report?.status)) return
  const id = setInterval(async () => {
    const progress = await getReportProgress(reportId)
    setProgress(progress)
    if (isTerminal(progress.stage)) {
      clearInterval(id)
      // 拉一次完整 report（带 snapshot）
      const full = await getEvaluationReport(reportId)
      setReport(full)
    }
  }, 1000)
  return () => clearInterval(id)
}, [report?.status, reportId])
```

要点：

- 用 `setInterval(1000)`，不用 `setTimeout` 递归（避免时间漂移）。
- `clearInterval` 必须在 effect cleanup 里返回，避免组件卸载后继续跑。
- `isInProgress(status) = status === 'PENDING' || status === 'RUNNING'`。
- `isTerminal(stage) = stage === 'COMPLETED' || stage === 'FAILED'`。

### 进度卡 UI

- `antd Progress`：PENDING 用 `default`、RUNNING 用 `active`、COMPLETED 用 `success`、FAILED 用 `exception`。
- 当前规则：`progress.current_rule_code`。
- 失败原因：`progress.message`（仅 FAILED 时填）。
- 中英文 `statusLabels` 已加 PENDING/RUNNING/COMPLETED/FAILED。

### 评分 Scope 多选

- `datasource_id` 仍是单选（业务上下文粒度）。
- `targetTables` / `ruleTypes` 改 `mode="multiple"` + `maxTagCount="responsive"`。
- payload 仅在非空时传 `targetTables` / `ruleTypes` 字段；空数组走全量（后端处理）。

## SSE 鉴权头 SSOT（fix-frontend-sse-auth-header）

**SSOT**：`src/api/authHeaders.ts` 的 `authHeaders(extra?)` —— 有 token 时注入 `Authorization: Bearer <token>`，同时恒定注入 `X-Tenant-Id`；axios 拦截器（`src/api/client.ts`）用的是同一个函数。

**为什么需要它**：SSE 是流式响应，axios 不擅长，三条链路都走**裸 `fetch`**，因此**绕过了 `httpClient` 拦截器** —— 头必须显式注入，忘记就 403。

| 链路 | 调用点 |
|---|---|
| Chat 对话流 | `src/api/chat.ts` `sendMessageStream` |
| 文档问答流 | `src/api/document.ts` `searchDocumentsQa` |
| Wiki 问答流 | `src/api/wikiChat.ts` `sendWikiChat` |

**用法规约**：

- 新增任何 SSE 端点，一律 `headers: authHeaders({ "Content-Type": "application/json" })`，不要手写 `Authorization`。
- 未登录（token 空）→ 只发 `X-Tenant-Id`，不发 `Authorization`（与拦截器一致）。
- **必须有用例断言请求头**（`expect(init.headers).toMatchObject({ Authorization: ..., "X-Tenant-Id": ... })`）：`wikiChat` 当初正是因为没有任何头断言，才把「完全没注入」的 403 带到线上。

**遗留**：`api/chatHistory.ts` 仍发死值 `X-User-Id`；`datasource` / `localImport` 有重复拦截器；`RoutingMetricsPage` 不发鉴权头。

## 假设验证状态标签（chore-p2-residual-cleanup）

**SSOT**：`src/components/chat/HypothesisPanel.tsx` —— `verificationStatus` 是数据库字符串（系统边界），渲染前先经 `KNOWN_VERIFICATION_STATUSES` 白名单收窄，未知值回落 `pending`（「待验证」），**绝不把未知枚举拼进 i18n key**（缺 key 时 i18next 回落成 key 本身）。

- 文案 key：`chat.hypothesis.status.{pending,supported,contradicted,inconclusive,execution_failed}`，`zh-CN` / `en-US` 两套齐备。
- **测试必须断言真实文案**：旧用例用 `/pending/i` 匹配，恰好命中回落 key `chat.hypothesis.status.pending`，缺失 i18n 时照样绿；现在每条用例额外断言 `queryByText(/chat\.hypothesis\.status\./)` 为空。
- **后端验证判定链路未接线**：`Hypothesis.verify()` / `VerificationResult` / `_recordVerification` 已作为死代码删除（无生产调用方）；前端「验证」按钮仍把 `verificationSql` 当普通问题走既有 QUERY 链路，因此 `verification_status` 恒为 null ⇒ 标签恒为「待验证」。要真正流转需新 API + 前端改调用。

## 错误细分类型（errorType）端到端（chore-p2-errortype）

后端 `error` SSE 帧携带 `errorType`（`app/services/stream_events.ErrorType`：`domain` / `llm` / `internal` / `provider_transient` / `provider_protocol` / `schema_reference` / `sql_syntax` / `db_connectivity` / `permission` / `query_timeout`）。前端链路：

```
SSE frame ── api/chat.ts parseEvent ── onError(message, detail?, errorType?)
          ── chatStore（首发/续跑两处）写 ChatMessage.errorType
          ── MessageItem：ERROR_TYPE_HINT_KEYS 白名单 → 错误气泡下方提示
```

**关键约定**：

- `errorType` 是**系统边界原始字符串**，只用于查表。`MessageItem` 的 `ERROR_TYPE_HINT_KEYS` 只列用户可以采取动作的 7 类；`domain` / `llm` / `internal` 不追加提示（正文已面向用户），未知类型静默不渲染。
- **绝不**把 `errorType` 拼进 i18n key：i18next `returnNull: false`，缺 key 会原样回显 key（与 hypothesis 状态标签同一个坑）。
- doc-QA / Wiki 客户端用 `{...obj, kind}` 展开，`errorType` 运行时已透传；是否有消费端取决于页面，聊天是本次接线的目标。
- 网络层兜底（`catch`）没有 `errorType`，不要伪造一个。

**测试锚点**：`chatApi.test.ts`（三参透传）、`chatStore.test.ts`（回填消息字段）、`MessageItem.test.tsx`（真实文案 + 反向断言无裸 key / 未知枚举不泄露）。

## 研究型 Agent 入口（research）（feat-research-entry-ux-fixes）

- **页面**：`pages/research/ResearchListPage.tsx`（列表 + 新建）、`ResearchSessionPage.tsx`（会话 + 检查点）、`ResearchReportPage.tsx`（报告）。
- **新建表单三要素**：研究问题、**数据源**（默认选 `isDefault` 源）、**模型**（默认「自动（智能路由）」）、模式。三者共用同一条流水线，`mode` 只改报告章节组织 —— 页面上一句免责说明写明这点，免得用户以为选「归因」会走别的流程。
- **模型选择的 `0` 哨兵**：`Select` 的 `value` 用 `modelId ?? 0`，选项首位是 `{ value: 0, label: 自动 }`，`onChange` 里 `value === 0 ? null : value`。**为什么不用 `undefined`**：antd 在 `value=undefined` 时只显示 `placeholder`，用户看不出默认是「自动」；而请求体仍按 `modelId === null` 判断是否**带键**（`...(modelId === null ? {} : { modelId })`），因为后端 `ResearchSessionCreate` 是 `extra="forbid"` —— 多带一个 `modelId: null` 会被 422 拒掉。**前端加字段必须与后端同批上线**，这是硬约束。
- **`CheckpointCard`**：按 `phase` 渲染「本次针对」目标行 + 相位明细（`runtime_dynamic` → `options.conflicts`；`planning` → `options.plan.steps`；`hypothesis` → `options.candidates` 可勾选，勾选结果按**数组下标**经 `choice.selectedIndexes` 提交）。空明细必给显式空态文案（降级路径下候选本就是 `[]`，不提示会被误判成「没修好」）。
- **键名坑（双键回落）**：`options.plan.steps[].sub_question` 是 snake_case（后端 `normalizePlan` / `singleStepPlan`），`options.stepResults[].subQuestion` 是 camelCase（`ports.stepResult` 里的键名）。**历史 checkpoint 的 `options` 已落库** ⇒ 前端读取器先 camelCase 再 snake_case，**绝不改后端键名**。
- **调用点必须给 `<CheckpointCard key={checkpoint.id} …>`**：组件内有 `useState`（勾选态 / 草稿），换检查点时不重挂载就会**跨检查点串味**。
- **删除**：`DELETE /api/v1/research/sessions/{id}`（硬删 + DB `ON DELETE CASCADE` + 归属不符 404，不泄露存在性）；前端 Popconfirm 二次确认，**服务端确认后**才从列表移除。
- **数据源名映射**：`hooks/useDatasourceOptions.ts` 导出 `useDatasourceOptions()`（启用中的源清单）与 `datasourceName(sources, id)`（id → 名称，**查不到的 id 回落 `` `#id` ``**——有 id 就让用户看到"某个库"，取不到名字不等于不渲染）。**取源失败一律回落空清单**——数据源名只是辅助信息，不做页面可用性的前提（仅展示层降级，不阻断页面）。
- **报告页模式说明**：`ResearchReportPage.tsx` 在模式 Tag 右侧渲染 `research.list.modeDesc.<mode>`（与新建表单下拉项**同一个 i18n key**）。`payload.mode` 运行时是任意 string ⇒ 先经 `asResearchMode()` 收窄；未知值 Tag 与说明**同时**不渲染（不构造文案、不显示原始 key）。
- **共享 `error` 槽的单一归属**：`researchStore.error` 由**会话页**渲染成 Alert，故**只有会话加载/追问类失败**才写它。列表页的删除失败**自带** `message.error`（`ResearchListPage.tsx`），`deleteSession` 因此**只 rethrow 不写槽** —— 否则列表页的删除错误会短暂串到**另一个**已打开会话页的 Alert 里。

## 请求超时（两层）（fix-ontology-batch-frontend-timeout）

axios 超时**两层**，别顺手调齐（2026-10-23 定，2026-10-24 加例外）：

- 全局 `REQUEST_TIMEOUT_MS = 30_000`（`src/config.ts`）——普通请求。错误挂 30s 才报，别为个别长接口调大它
- `LONG_RUNNING_TIMEOUT_MS = 280_000`——长操作 per-call 覆盖：`/ontology/batch/preview`、`/ontology/relations/backfill`、`/ontology/graph/sync-missing`
- **例外**：`runOntologyBatch`（`POST /ontology/batch`）单独用 **300_000 临时预算**（生产批量执行实测贴近 nginx 上限）。长期方案 202 + 后台任务 + 轮询，届时移除

**硬约束**：nginx `proxy_read_timeout=300s` 是上限，280s 是留余量；守卫测试 `ontologyBatchApiTimeout.test.ts` 锁定各调用值，新增长操作调用必须加进去。

**运维语义**：axios 超时 ≠ 后端停止——操作还在跑，超时后**别重试**，先刷新看结果；「网络异常，请稍后重试」文案 = status=0 专有（前端掐断），后端 4xx/5xx 不显示它。

## 通用 Agent 编排 UI（feat-agent-orchestration）

**日期**：2026-10-28 · **变更**：[feat-agent-orchestration](../changes/feat-agent-orchestration/summary.md)
· **Wiki**：[[agent-runtime]]

### 路由

| 路径 | 组件 | 说明 |
|------|------|------|
| `/agent-orchestrations` | `pages/AgentOrchestrationPage.tsx` | 编排配置列表 + 新建/编辑弹窗 + 启停 |
| `/agent-tasks` | `pages/AgentTaskPage.tsx` | 编排任务（run）列表 + 创建 run + 时间线 + 恢复/取消 |

路由在 `src/App.tsx:115-116` **无条件注册**（见下方「已知限制」）。

### 轮询模型（任务页）

```typescript
// src/components/agent/task/useAgentRunTracker.ts
export const RUN_POLL_MS = 2000;              // 固定 2s 一跳

const afterStepNo = lastStepNoRef.current + 1;  // 闭区间下界 ⇒ 只取增量
const [read, incoming] = await Promise.all([
  getAgentRun(runId),
  getAgentRunSteps(runId, { afterStepNo }),
]);
```

要点：

- `afterStepNo` 是**闭区间下界**（后端返回 `step_no >= afterStepNo`）⇒ 每次传
  `lastStepNo + 1`，**绝不重取最后一步**。
- 只在 `QUEUED` / `RUNNING` 时轮询；进入终态或 `PAUSED` 停轮询（终态再补拉一次快照）。
- 用 `setTimeout` 递归（不是 `setInterval`），避免慢请求叠请求。
- **代次机制**：每次 effect 重启换一代次，旧定时器链即使晚一拍恢复也因代次不匹配
  而不再续期 ⇒ 不会出现两条并行的轮询链。
- **404 ⇒ 停止轮询，绝不伪造一个终态运行出来**（`onMissing` → `setPollStopped(true)`）。
- 拉取用 `Promise.all` 并行取「run 快照 + 增量步骤」。

### 错误文案分流

`src/components/agent/task/errors.ts` 的 `describeError` **只按 HTTP status 分流**：

| status | 文案键 |
|---|---|
| 404 | `agentTask.messages.runNotFound` |
| 403 | `agentTask.messages.forbidden` |
| 409 | `agentTask.messages.conflict` |
| 422 | `agentTask.messages.validationFailed`（带 detail）/ `…Generic` |
| `undefined` / `0` | `agentTask.messages.networkError`（= 前端掐断） |

> **不要用 `AppError.code` 判别**：后端对所有 DomainError 都返回 `code = "UNKNOWN"`。
> `AppError.message` 的取值优先级 = 响应 `detail` 对象里的 `message` → 响应体**顶层
> `error`** → axios 兜底文案（`src/api/client.ts` 的 `normalizeError`）。
> ⚠️ `normalizeError` 这条「抬升顶层 `error`」是本特性**对共享模块的改动**，
> 超出设计稿 §9.3 允许的前端范围 —— 已记在变更 SSOT §4。

### 特性关闭时的降级

后端 `ENABLE_AGENT_ORCHESTRATION=false` ⇒ 编排路由**不注册** ⇒ API 返回 **404**：

- 任务页：404 ⇒ 停止轮询，提示「运行不存在」（**不会**伪装成已结束）；
- 配置页：列表加载 404 ⇒ 显示「编排配置列表加载失败」。

### 已知限制（前端）

- **两条路由无条件注册**（不进 feature flag）⇒ 特性关闭时页面仍可打开，只是数据接口 404。
  没有专门的「特性未启用」页面态。
- **系统上限硬编码在前端**（20 / 30 / 5 USD / 900 / 1000-50000），而
  `backend/app/config.py` 允许用环境变量覆盖 ⇒ 覆盖过的部署会出现 UI 边界漂移
  （服务端仍按 `assertWithinSystemCeilings` 兜底）。
- **两套并行的 status 分流 + 两份 i18n 键族**：`describeFailure`
  （`components/agent/useOrchestrationProfiles.ts:62-78`，用 `agentOrchestration.*` 键）与
  `describeError`（`components/agent/task/errors.ts:30-45`，用 `agentTask.*` 键）是两套
  独立实现，同一种 status 在两处各自映射到各自的语言包键。
  （`errorStatus` / `errorDetailText` **不是**重复定义——它们只在 `errors.ts` 定义一次。）
- `useAgentRunTracker.ts:224-226` 的 `useCallAction` catch 把 `resumeRun` 的异常转成
  `setActionError`（`resumeRun` 因此**正常 resolve**）⇒ 恢复被拒（409/422）时
  `useResumeActions.ts:46-51` 的 `reset()` 仍会执行，用户填的内容丢失。
