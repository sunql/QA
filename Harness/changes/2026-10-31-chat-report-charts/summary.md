# 变更：分析计划的图表进最终报告（分节）+ 多类别图表可读性

- **日期**：2026-10-31
- **分支**：`feat/chat-report-charts`（base `559971f6`）
- **状态**：**已发布**（main `e54f9005`，2026-10-31 生产部署）
- **关联**：[2026-09-30-chart-in-final-report](../2026-09-30-chart-in-final-report/summary.md)（0105：顶层图 = 最后一步）、[2026-10-01-visual-output-strategy](../2026-10-01-visual-output-strategy/summary.md)（0107 反转：汇总纯文字）、[2026-10-31-replay-plan-cards](../2026-10-31-replay-plan-cards/summary.md)（0138：`steps_snapshot`）、[2026-10-30-chat-chart-rendering](../2026-10-30-chat-chart-rendering/summary.md)（图例/数字格式/PNG）

---

## 1. 需求

用户原话（2026-10-31）：

> 「优化 AIChatService 功能的对话输出，当前问题的分析只在分析计划当中有不同的图表输出，
> 希望能够把在分析计划中一步或者多步的图表能够在最终的分析报告中相关的位置体现，
> 增加对话分析的可读性和体验，另外现在分析过程中的坐标系不太合理，纵轴如果很多横轴
> 会同比例也变小了，例如查询 2025 年每个供应商的采购总额排名，查询的结果输出的图数量
> 很小几乎看不清楚，这个需要优化」

两个诉求：

1. **图表进最终报告的相关位置**：多步分析的每一步图要出现在最终回答对应段落，而不是
   只藏在默认折叠的「查看分析计划」卡片里。
2. **多类别图表可读**：类别一多，图表整体连同坐标轴被等比压缩，条/标签看不清。

## 2. 现状与根因

### 2.1 最终报告 = 纯文字

- `StepAggregator.aggregate`（`backend/app/services/step_aggregator.py:32-60`）产出多步的
  最终自然语言回答；提示词（`:19-23`）只要求「生成对比 / 汇总结论」，无结构、无图。
- 落库为 `session_message.content`，顶层 `chart_type/chart_option` **恒为 None**
  （`chat_multistep.py:710-721`、`chat_stream_multistep.py:258-277`、
  `chat_helpers.py:150-160`）。这是 0107 对 0105 的刻意反转。
- 每步图只存在于 `steps[i]` / `steps_snapshot`，前端只在 `MultiStepPlanCard`
  （默认折叠，`MultiStepPlanCard.tsx:82-88`）里渲染。
- 非流式汇总路径**没有**落 `steps_snapshot`（`chat_multistep.py:710-721`），流式有
  （`chat_stream_multistep.py:271-276`）—— 既有不对称，回放会缺图。

### 2.2 多类别被压扁

三处叠加：

1. 前端图表高度写死 `320`（`frontend/src/components/chat/ChartRenderer.tsx:205`），
   与类别数无关；`EChart.tsx:22` 是另一个写死 `280`。
2. 决策引擎在行数 > 15 时转横向条形图（`chart_decision.py:430-431`），类别轴变成纵轴，
   30 个供应商 → 绘图区约 224-248px → 每条约 8px。
3. 后端类目轴是裸的 `{"type":"category","data":[...]}`（`chart_renderer.py:205`），
   无 `dataZoom` / `axisLabel.interval` / `rotate` / 截断；前端 `chartFormat.ts:58-63`
   明确只处理 value/log 轴。ECharts 默认自动稀疏标签，用户既看不到条也看不到名。

## 3. 已拍板的决策

| # | 决策 | 备选与否决理由 |
|---|---|---|
| D1 | **结构化分节**：报告固定「每步一节 + 节末图 + 结论」 | 否决「标记锚定」（依赖模型输出标记）；否决「文末图表区」（不满足"相关位置"） |
| D2 | **动态高度 + 超限 dataZoom** | 否决 Top-N 截断（图不再等于全量结果，语义变重） |
| D3 | **本批只做聊天界面**；PDF 分节插图下一批 | 多步 PDF 今天本来就无图（非回归）；导出契约扩展工作量大 |

## 4. 设计

### 4.1 报告格式契约

多步汇总（含只有一个数据步的计划）固定输出：

```
## 分析报告
### 步骤 1：<子问题 / 步骤描述>
<该步的分析结论>
### 步骤 2：<子问题 / 步骤描述>
<该步的分析结论>
### 结论
<跨步对比、差异、趋势>
```

- 失败步骤不单独成节，只在结论中如实说明（不给图）。
- 步骤数与顺序以 `multi_step_plan` 的数据步为准。

### 4.2 后端

**`step_aggregator.py`**：系统提示词改为强制上述分节格式；输入不变（每步已有
description / 子问题 / SQL / 数据摘要 / summary / error，均经 `_sanitize`）。

**新增 `backend/app/services/report_sections.py`（纯函数，无 IO）**

```
buildReportSections(answer: str, steps: list[StepResultRead]) -> list[ReportBlock]
ReportBlock = {"kind": "text", "content": str}
            | {"kind": "chart", "stepIndex": int, "title": str}
```

规则：

1. 标题识别：`^#{1,4}\s*(?:步骤|Step)\s*(\d+)\s*[:：.、]?\s*(.*)$`（宽松匹配 `#` 级数）。
2. `N` 映射到 `step_index == N-1`；必须存在、`sql is not None`、`chartType` 与
   `chartOption` 非空，否则该标题按普通文本处理。
3. 同一步骤只插一张图（首次出现处）。
4. 解析后任何**有图但未被引用**的成功步骤：在末尾补 `### 关键图表` 文本块 + 对应 chart 块，
   按 step_index 升序 —— 图绝不静默丢失。
5. 一个分节都识别不出（模型没按格式输出）：返回 `[text(原文), chart...]` 回退形态。
6. 空答案 / 无步骤 → 返回 `[]`，前端走旧渲染。

**契约（无迁移）**：`reportSections` 是**计算字段**：

- 非流式：`ChatResponse.reportSections`（`_multiStepResponse` 里现算）。
- 流式：`EVENT_DONE` 帧加 `reportSections`（此时最终文本与 completed steps 都在手上）。
- 历史回放：`SessionHistoryService.loadFullMessages` 从持久化的 `content` +
  `steps_snapshot` 现算，`ChatMessageRead` 增字段。
- 图表负载**不重复下发**：chart 块只带 `stepIndex`，前端从 `message.steps[stepIndex]`
  取 `chartType/chartOption/tableOption/visualRationale`。

**顺带修复**：非流式汇总落库补 `steps_snapshot`（与流式对齐），否则非流式多步消息
回放时新报告缺图。

### 4.3 前端

- `types/chat.ts`：`ReportBlock` / `reportSections` 类型。
- `chatStore`：`done` 帧与历史回放写入消息。
- `MessageItem`：`reportSections` 非空时按块顺序渲染 —— text 块复用现有 Markdown
  渲染器，chart 块在该位置渲染 `ChartRenderer`（从 `steps` 解析，取不到则跳过该块）；
  字段为空/缺失走现有逻辑（老消息零回归）。
- 计划卡 `MultiStepPlanCard` 保持原样。

### 4.4 图表可读性

**前端 `frontend/src/utils/chartLayout.ts`（新增，纯函数）**

```
chartHeightOf(option) -> number
```

| 形态 | 高度 |
|---|---|
| `hbar` | `clamp(320, 96 + 类别数×28 + (legend ? 32 : 0), 960)` |
| `heatmap` | `clamp(320, 120 + 行数×26, 960)` |
| `bar` / `line` / `combo` / `pie` / `donut` / `scatter` | 320（>15 行已被决策引擎转 hbar） |
| `table` / `kpi` | 不涉及 |

- `ChartRenderer.tsx`：`style={{ height: chartHeightOf(...) }}` 替换写死 320。
- `chartSnapshot.ts`：PNG 导出改用同一高度函数（宽度仍 800），导出与屏幕一致。
- 高度计算只看 option 结构（`xAxis`/`yAxis` 的 category `data` 长度），不依赖后端新字段。

**后端 `chart_renderer.py`（只加结构，不加颜色）**

| 条件 | 配置 |
|---|---|
| hbar 且类别 > 12 | `axisLabel: {interval: 0, overflow: "truncate", width: 160}` |
| 纵向 bar 且类别 > 12 | `axisLabel: {rotate: 45, interval: 0, hideOverlap: true}`（line 不旋转，保持自动稀疏） |
| 类别 > `_DATAZOOM_THRESHOLD`（40） | 类目轴 `dataZoom: [{inside}, {slider}]`；`grid.bottom` 联动留滑块空间 |

- 阈值进模块头常量并注释「可读性结构边界，不治理」。
- 数值标签 `>12 点关闭` 规则（`_LABEL_MAX_POINTS`）**不动**。

## 5. 非目标

- PDF 导出的分节插图（下一批；本批导出行为与今天一致，多步 PDF 仍无图）。
- Top-N 截断 / 「其他」聚合。
- 计划卡片改版、研究线 `ReportPlanner` / `EChart.tsx` 的尺寸。
- 单步问答的渲染链路（顶层图逻辑不变，仅受高度函数影响）。

## 6. 验收

1. 多步提问（如「第一步：各采购组织的收货数量；第二步：各供应商的收货数量」）：
   最终回答按步骤分节，每节末尾是该步的图；结论节无图；计划卡仍可展开。
2. 「2025 年每个供应商的采购总额排名」：图高随供应商数增长，条与标签可读；
   供应商超过约 30 家时出现缩放条（高度封顶 960）。
3. 刷新回放：分节与图与实时一致。
4. 回归：单步问答、历史会话、导出 PDF、表/ KPI 形态不受影响。

## 7. 测试计划（TDD）

**后端**

- `unit/test_report_sections.py`：标题变体（`###`/`##`、`步骤 N` / `Step N`、全角冒号）、
  失败步骤不成节、无图步骤不成图、重复引用只插一次、未引用步骤末尾补齐、
  完全不符合格式的回退、空输入。
- `unit/test_chart_renderer.py`：hbar 多类别标签配置、纵向 bar 旋转、dataZoom 阈值与
  `grid.bottom` 联动、阈值以下逐字不变（反向守卫）。
- `integration/test_chat_multi_step.py`：非流式/流式响应含 `reportSections` 且块顺序正确；
  非流式落 `steps_snapshot`。
- `integration/test_chat_history_api.py`：回放现算 `reportSections`。

**前端**

- `tests/chartLayout.test.ts`：各图型高度、封顶、图例余量、无类别轴时的默认值。
- `tests/ChartRenderer.test.tsx`：hbar 30 类渲染高度 936；60 类封顶 960 且 option 带
  dataZoom（后端产物）；table/kpi 不受影响。
- `tests/MessageItem.test.tsx`：有 `reportSections` 时分节渲染、chart 块取 steps 图、
  缺字段回退旧渲染（反向守卫）。
- `tests/chatStore.test.ts`：`done` 帧与回放写入 `reportSections`。
- `npx tsc --noEmit` 0 错误。

**E2E 冒烟**（部署后）：真实浏览器跑上述两个提问，检查最终报告内图数量/位置与
30 类图的可读高度。

## 8. 风险

| 风险 | 缓解 |
|---|---|
| 模型不按分节格式输出 | 解析器宽松 + 回退形态（原文 + 文末图），功能不丢 |
| 输出分节但步骤号错位 | 严格映射 + 未命中按文本处理；未引用图末尾补齐 |
| 动态高度让消息过长 | 封顶 960px；超限走 dataZoom |
| `dataZoom` 与底部图例/滑块挤占 | `grid.bottom` 联动 + 单测钉住 |
| 回放与实时不一致 | 同一纯函数 `buildReportSections` 在两处调用 |

## 9. 交付物

| 文件 | 动作 |
|---|---|
| `backend/app/services/report_sections.py` | 新增（解析/校验/补齐） |
| `backend/app/services/step_aggregator.py` | 系统提示词改为分节格式 |
| `backend/app/services/chat_helpers.py` | `_multiStepResponse` 增 `reportSections` |
| `backend/app/services/chat_multistep.py` | 非流式落 `steps_snapshot` + 传 `reportSections` |
| `backend/app/services/chat_stream_multistep.py` | `done` 帧增 `reportSections` |
| `backend/app/services/session_history_service.py` | 回放现算 `reportSections` |
| `backend/app/domain/schemas.py` | `ChatResponse` / `ChatMessageRead` 增字段 |
| `backend/app/services/chart_renderer.py` | 类目轴标签 + dataZoom |
| `frontend/src/utils/chartLayout.ts` | 新增高度函数 |
| `frontend/src/components/chat/ChartRenderer.tsx` | 动态高度 |
| `frontend/src/utils/chartSnapshot.ts` | 导出高度与屏幕一致 |
| `frontend/src/components/chat/MessageItem.tsx` | 分节渲染 |
| `frontend/src/stores/chatStore.ts` / `types/chat.ts` | 契约字段 |
| `Harness/wiki/chart-rendering.md` | 契约与尺寸规则更新 |
| 测试 | 见 §7 |

## 10. 测试证据（2026-10-31 实施后实测）

### 后端（5 个文件，真实 PostgreSQL 5434 + Neo4j 7688）

```
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test
TEST_NEO4J_URI=bolt://127.0.0.1:7688
TEST_NEO4J_PASSWORD=qa_neo4j_dev_2026
pytest app/tests/unit/test_report_sections.py app/tests/unit/test_step_aggregator.py \
       app/tests/unit/test_chart_renderer.py app/tests/integration/test_chat_multi_step.py \
       app/tests/integration/test_chat_history_api.py -q
```

| 套件 | passed | failed |
|---|---|---|
| `unit/test_report_sections.py` | 9 | 0 |
| `unit/test_step_aggregator.py` | 11 | 0 |
| `unit/test_chart_renderer.py` | 60 | 0 |
| `integration/test_chat_multi_step.py` | 41 | 0 |
| `integration/test_chat_history_api.py` | 25 | 0 |
| **合计** | **146** | **0** |

pytest 自报 `146 passed, 21 warnings in 85.00s`（wall `real 87.74s`；21 条均为
`slowapi` 的 `asyncio.iscoroutinefunction` DeprecationWarning）。

**基线对照**：Task 0 基线为 step_aggregator / chart_renderer / chat_multi_step /
chat_history_api 四文件共 **123 passed**；本次四文件 137 passed（新增用例随本批加入）
+ 新文件 report_sections 9 passed = 146 passed、**0 failed ⇒ 净新增失败 0**。

**reportSections 负载**：集成测试断言非流式与流式 `done` 两处均为 5 个块
（`text, chart, text, chart, text`），图块只带 `stepIndex` + `title`，**不重复下发
`chartOption`**（前端从 `message.steps[stepIndex]` 取）。

### 前端（6 个文件）

```
npx vitest run src/tests/chartLayout.test.ts src/tests/chartSnapshot.test.ts \
  src/tests/ChartRenderer.test.tsx src/tests/MessageItem.test.tsx \
  src/tests/chatStore.test.ts src/tests/reportSections.test.ts
```

| 套件 | tests passed |
|---|---|
| `chartLayout.test.ts` | 7 |
| `chartSnapshot.test.ts` | 14 |
| `ChartRenderer.test.tsx` | 34 |
| `MessageItem.test.tsx` | 53 |
| `chatStore.test.ts` | 52 |
| `reportSections.test.ts` | 3 |
| **合计** | **163 passed / 0 failed（6/6 files）** |

vitest 自报 `Test Files 6 passed (6) / Tests 163 passed (163)`，Duration 3.15s
（wall `real 3.46s`）。**基线对照**：Task 0 基线 4 文件 145 passed；本次新增
`chartLayout` 7 + `reportSections` 3 及既有文件新增用例，合计 163 passed，
**0 failed ⇒ 净新增失败 0**。仅有既存的 antd/jsdom `getComputedStyle` stderr 噪音，
未触碰的套件同样复现。

### 类型检查

`npx tsc --noEmit`：**0 错误**（wall `real 4.52s`）。

---

## 11. 同批追加：L4 分析路径回显 SQL

上线前测试发现「为什么…」类归因问题前端没有 SQL 卡片。根因分两层，**第二层才是主因**：

1. 采集层：`final_sql` 只认模型主动输出的 ```final_sql``` / `::final_sql::` 围栏，而 L4
   提示词从未要求该格式 ⇒ 生产 `session_message.sql_generated` 恒为 NULL，尽管
   `execute_sql` 确实执行过。改为确定性采集：loop state 新增 `executed_sqls`，只在
   `execute_read_only` **成功**后 append；loop 结束后 `final_sql` 为空则回显最后一条
   （`_lastExecutedSql`），`answered` 与 `max_iterations` 两条终止路径都覆盖；显式围栏仍优先；
   失败/被只读守卫拒绝的 SQL 不采集。
2. 下发层：`chat_stream_entry.py` 的 preamble 分支只发 `meta / chart / token / done`，
   **从不发 `sql` 帧**。补发后序列为 `meta → sql → token → done`（有 SQL 时）。
   非流式路径 `ChatResponse.sql` 本就有值。
3. `max_iterations` 默认 5 → 10：5 轮只够探表 + 试错 SQL，从未进入写结论的轮次；
   成本仍由 `cost_budget_usd` 兜底。

测试：`backend/app/tests/unit/test_agent_loop_final_sql.py`（7 条）+ 
`test_chat_stream_l1_l4.py::test_stream_l4_preamble_emits_sql_frame`（1 条）。采集用例做过
变异验证（把 append 提前到执行前 ⇒ 两条失败用例变红），证明材料为真。

## 12. 发布

- **合并**：PR #84 squash → main `e54f9005`（2026-10-31）。
- **镜像**：`qa-system-backend:latest` = `6354d7e5…`、`qa-system-frontend:latest` = `7288b624…`
  （发版源 = `e54f9005` 的独立 worktree；前端 build 显式 `--build-arg VITE_API_BASE_URL=`，
  实测 bundle 内无 `localhost:5173/api/v1`）。
- **切换**：`docker compose stop backend frontend` → `up -d backend frontend`，无迁移
  （生产 `alembic_version=0138` 与树内 head 一致）。
- **回滚锚点**：`qa-system-backend:pre-deploy-20261031`（`f330d546…`）、
  `qa-system-frontend:pre-deploy-20261031`（`f8b698a6…`）。
- **冒烟**：生产流式 L4 归因题返回 `meta → sql → token → done`，SQL 帧为实际执行的语句，
  终止原因 `answered`；用户人工验收通过。
- **CI**：Frontend typecheck + coverage 绿；Backend integration shard 1/3 红，
  但上一个 main `559971f6` 四个 shard 全红 —— 非本批引入。
