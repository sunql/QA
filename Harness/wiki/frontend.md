---
created: 2026-09-01
updated: 2026-09-01
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
