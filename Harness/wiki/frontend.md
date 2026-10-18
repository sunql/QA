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

## Chat 流式消息绑定的生命周期（fix-chat-assistant-message-id-lifecycle）

**SSOT**：`src/stores/chatStore.ts` 的 `currentAssistantMessageId` —— 「当前在途 assistant 轮次」的唯一指针；`sendMessage` 内 `isCurrent()` 用**严格相等**决定每个 SSE 帧能否落库。

- **指针生命周期 = 一轮流的生命周期**。`sendMessage` 创建 placeholder 时写入；`onDone` / `onError` 与 `finally` 经 `releaseAssistantMessageId()` 释放；`clearMessages` / `resetSession` / `clearForUserSwitch` / `setDatasourceId` / `enterChannel` / `loadSessionMessages` / `deleteSession` 也显式释放。
- **所有 catch 与非流式成功分支必须先过 `isCurrent()`**：切会话后旧轮迟到的响应/失败不得改写新一轮的消息、`loading` 或指针（code-review 复现：旧 catch 无条件置 null 会抢走新一轮指针，后者全部帧被丢弃）。这些分支**不直接写指针**，释放统一由 `finally` 的条件释放完成。
- **`releaseAssistantMessageId()` 必须条件比较**（`get() === assistantMessageId` 才置 null）：本轮晚到的 `finally` 若无条件清空，会抹掉下一轮刚写入的 id，下一轮所有帧随后被严格守卫丢弃 ⇒ 回复空白。`finally` 的 `loading`/`isStreaming` 复位同样要求指针仍属于本轮。
- **不变量：指针被清 = 没有轮次拥有 `loading`**。所有清指针的入口必须一并 `loading: false`。孤儿轮次的收尾因指针不匹配不会复位 loading；若切换动作漏了这一步，流进行中切数据源/渠道会让输入框永久禁用（`sendMessage` 的 `if (loading) return` 静默早返）。
- **`null` 不再表示「放行」**。旧实现 `id === null || id === assistantMessageId` 把「无在途轮次」当成合法消费者，导致会话切换清指针后，上一轮残留 token/done 仍 patch 到新会话最后一条消息。生产代码里 `sendMessageStream` 只有 `sendMessage` 一个调用点，无旁路消费者。
- **误标 aborted**：`sendMessage` 开头按指针中止上一轮。指针不释放时，下一次发送会把**已完成**的回复标成 aborted；旧用例曾把这个 bug 断言为期望行为，现已改为回归用例（已完成轮不标 aborted；真在途轮仍标）。
- **`sendDocQa` 也参与轮次所有权**（该子系统没有 `currentAssistantMessageId`，所有权判据是「自己创建的占位消息仍是当前渠道的最后一条」）：切渠道 / 载入历史 / 发下一条消息后，旧 doc-QA 流的 `token` / `citations` / `done` / `error` 与 catch 分支一律丢弃。否则晚到的帧会 `patchLastMessage` 到新气泡上，且 `done`/`error` 会把新一轮的 `loading` 关掉（typescript-reviewer 与 code-reviewer 联合复现的跨渠道串台）。
- **`loadSessionMessages` 失败保留 messages**（与 `clearMessages` 不同），所以必须就地给被放弃的占位收尾（`isStreaming: false` + `aborted: true`）：指针已清 ⇒ 后续帧全被丢弃，留着 `isStreaming` 就是一个永远转圈、永不更新的气泡。
- **测试断言不能写在 `mockImplementation` 里**：`sendMessageStream` 的 mock 与 SSE handler 同一调用栈，`AssertionError` 会被 `sendMessage` 的 catch 吞掉（当前指针已释放 ⇒ 连错误分支都不写），用例照样通过。要捕获状态到局部变量、出 mock 后再断言。
- **测试锚点**：`src/tests/chatStore.assistantMessageId.test.ts`（19 例：3 条收尾路径 + 2 条 aborted 语义 + 8 个会话切换入口 + 晚到帧/交叠轮次/孤儿流回归）与 `src/tests/chatStore.docQaOwnership.test.ts`（2 例：doc-QA 切渠道后的 token/done 与失败隔离）。

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
