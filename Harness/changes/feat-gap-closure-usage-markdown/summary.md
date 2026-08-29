# 变更：设计差距补齐（会话/用量看板 + Markdown 回复渲染）

- **日期**：2026-08-12
- **作者**：AI 助手
- **Phase**：5.x（Phase 5 之后的差距补齐，按设计稿缺项优先级执行）
- **状态**：done（#63 用量看板、#64 Markdown 渲染）

## 1. 需求

对照 `docs/设计01-架构蓝图.md` / `docs/设计02-详细设计.md` 与原实现做三档差距分析后，本轮补齐两个缺口项：

1. **会话/Token 用量看板（原设计中"用量看板"能力，实现缺失）**——按会话聚合调用次数/Token/成本，展示按模型汇总与逐次调用流水，供运营观察成本。
2. **Markdown 回复渲染（原设计"自然语言回答"期望带格式，实现为纯文本）**——LLM 回答含编号列表、加粗、表格等 markdown 结构，需正确渲染。

## 2. 设计评审

- **用量看板**：新增 `GET /api/v1/sessions` 列表接口（聚合查询，避免 N+1）。单会话明细复用既有 `GET /sessions/{id}/usage`（summary）与 `GET /sessions/{id}/usage/rows`（流水）。前端 master-detail 布局：左列表 + 右明细卡片。
- **列表加载解耦**（关键决策）：`loadSessions` 的 `useCallback` 依赖数组不得含 `selectedId`，否则自动选中首行会重建回调 → effect 重跑 → 重复请求。分离"加载列表"与"自动选中首行"两个 effect。
- **成本展示精度**：成本为 Decimal，前端 `toFixed(6)` 展示（对齐 `$0.0132608` 这类真实值）。
- **Markdown 渲染**：选择 `react-markdown@10`（统一 renderer，杜绝自写正则替换的 XSS 面）+ `remark-gfm@4`（表格/任务列表）。
  - **流式期间不解析**（关键决策）：`isStreaming=true` 时保持既有 `pre-wrap` 纯文本 + 打字光标，避免逐 token 解析 markdown 造成闪烁；`done` 后一次解析。
  - **XSS 面**：LLM 回答为不可信输入。`react-markdown` 默认不启用 `rehype-raw`，原生 HTML 标签不被渲染为 DOM（按文本转义），无注入面。
  - **安全默认**：默认 URL 过滤（`javascript:` 等协议被剔除）。

## 3. 数据模型变更

无新增表。复用既有 `session_token_usage` 表（聚合查询）与 `session_message` 表（`listSessions` 取最近一条用户问题作为 `lastQuestion` 预览）。

## 4. 接口契约变更

新增 `GET /api/v1/sessions`（`session.py` 挂载前缀 `/api/v1/sessions`，路由 `""`）：

```
GET /api/v1/sessions → [{
  sessionId, totalRequests, totalTokens, totalCost,
  firstRequestTime, lastRequestTime, lastQuestion
}]
```

- 按 `lastRequestTime` 降序；`totalCost` 为 Decimal（JSON 序列化为字符串精度）。
- `lastQuestion`：从 `session_message` 取每会话 `role='user'` 且 `id` 最大的那条 `content`（`max(id)` 子查询 JOIN，避免 N+1）。

## 5. 实现要点

### 后端

- `app/services/token_usage_service.py`：新增 `listSessions(session) -> list[SessionListItem]`。聚合子查询 `GROUP BY session_id` + `func.count/sum/min/max`；最近提问用 `max(id)` 子查询 JOIN（替代 `literal_column("sid")`，跨 DB 兼容）。
- `app/domain/schemas.py`：新增 `SessionListItem`（CamelModel）。
- `app/api/v1/session.py`：新增 `GET ""` 路由。

### 前端

- `src/types/session.ts`：新增 `SessionListItem`；修正 `ModelUsageStat`/`SessionTokenUsage` 对齐后端真实契约（`requests`/`totalTokens`/`totalCost`、`modelName`/`purpose` 可空）。
- `src/api/session.ts`：新增 `listSessions()`。
- `src/pages/UsagePage.tsx`（新增，~150 行）：master-detail 用量看板。会话列：最近提问/会话 ID/调用次数/Token/成本($)/最近活动；明细列：时间/用途/模型/Prompt/Completion/合计/成本($)；明细卡片含 `Statistic`（调用次数/累计 Token/累计成本）+ `Descriptions` 按模型汇总。
- `src/components/chat/MessageItem.tsx`：非流式助手消息改为 `<ReactMarkdown remarkPlugins={[remarkGfm]}>`；流式保持纯文本 + 光标。
- `src/index.css`：新增 `.markdown-body` 样式（段落间距、列表缩进、表格边框、代码块底色）。

## 6. 测试

### 后端

- `test_token_usage_service.py`：`listSessions` 聚合正确性（count/sum/min/max）、`lastQuestion` 取最近用户问题、空表返回 `[]`。
- `test_model_config_api.py`（`TestSessionUsageApi`）：`GET /api/v1/sessions` 返回聚合 + 最近提问；空会话返回空数组。
- 全量：**256 passed**（#63 增量后回归无破坏）。

### 前端

- `MessageItem.test.tsx`（新增，8 例）：纯文本原样、加粗 `<strong>`、有序列表 `<ol>/<li>`、GFM 表格 `<table>`、流式期间保留光标且不解析 markdown、用户消息纯文本、错误消息 Alert 不解析、Token/成本/模型标签并存。
- `UsagePage.test.tsx`（新增，4 例）：列表加载+成本格式化、自动选中首会话+明细渲染（按模型汇总/用途标签）、刷新按钮重拉列表、空列表不渲染明细区。
- 全量：**104 passed**（原 96 + 新 8），`tsc -b` clean。

### E2E（临时验证，已清理）

- 真实浏览器（Playwright）验证 done 后 markdown 渲染为 `<ol>`（3 个 `<li>` 各含 `<strong>`）+ `<table>`（GFM）+ token 标签，12 例全通过。
- 流式中间态（纯文本+光标）由 jsdom 单测覆盖（`MessageItem.test.tsx`）；Playwright `route.fulfill` 会等流耗尽才发响应头，无法可靠捕获"流式进行中"状态，故 E2E 只验证完成态。

## 7. 安全审查

- **Markdown 渲染经 typescript-reviewer**：**0 CRITICAL / 0 HIGH**（3 MEDIUM + 3 LOW，**已全部修复**并复测）。
  - **XSS 面确认安全**：`react-markdown@10` 直接构造 React 元素，不用 `dangerouslySetInnerHTML`；未启用 `rehype-raw` 时 raw HTML（`<script>`/`<img onerror>`）按文本转义，不渲染为 DOM；默认 URL 过滤阻断 `javascript:` 协议。
  - **MEDIUM-1 已修复**：补 XSS 安全回归测试（`<script>`/`<img>` 断言为 null + 原文以文本呈现）。
  - **MEDIUM-2 已修复**：`.markdown-body code` 通用选择器导致 `<pre>` 内 code 双重样式（内层圆角/底色冗余）→ 改为 `.markdown-body :not(pre) > code` 行内样式 + `.markdown-body pre code` 显式清空。
  - **MEDIUM-3 已修复**：`MessageItem` 包 `React.memo`，父组件重渲染时相同 content 不再重复解析 markdown。
  - **LOW-1 已修复**：测试中 `querySelector("tbody") as HTMLElement` 不安全 cast → 先断言非 null。
  - **LOW-2 已修复**：流式 `<Paragraph>` 行高对齐 `markdown-body`（1.7），完成后切换渲染无视觉跳动。
  - **LOW-3 已修复**：补行内/围栏代码块、链接、引用测试。
  - 复测：**107 passed + tsc -b clean**。

## 8. 部署验证

- 真实后端（port 8000）+ 前端 Vite（port 5173）运行中。
- 真实对话验证：`POST /api/v1/chat` 返回 markdown 回答（编号列表 + 加粗供应商名），与渲染测试结构一致。
- 服务端渲染（SSR）确认 `react-markdown` 将真实回答输出为 `<ol><li><strong>…</strong></li>…</ol>`。
- 临时 Playwright 验证（已清理）：真实浏览器中 done 后渲染为 `<ol>`（3 个 `<li>` 各含 `<strong>`）+ `<table>`（GFM），全量 12 例 E2E 通过。

## 9. 关联

- 设计稿：`docs/设计01-架构蓝图.md`、`docs/设计02-详细设计.md`
- Wiki：`Harness/wiki/model-router.md`、`Harness/wiki/data-model.md`
- 上游：`Harness/changes/feat-phase5-production/summary.md`（5.6 流式打字光标为基础）
