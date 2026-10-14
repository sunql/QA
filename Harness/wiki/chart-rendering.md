# 图表决策引擎 + 标准化 Spec + 渲染器

> 2026-09-30 重写。旧文描述的是「LLM 直接写 ECharts option」的契约，那套已经不在了；
> 变更记录见 `Harness/changes/2026-09-30-chart-decision-engine/`。
> 2026-10-01 补充「可视化输出策略」（图/表/图+表 + 判断依据）：契约新增 `tableOption` /
> `visualRationale`，多步顶层继承图**反转**为无图；变更记录见
> `Harness/changes/2026-10-01-visual-output-strategy/`。

## 原则

**画什么图由代码决定，不由 LLM 决定。** 规则表先按数据语义选出图型，LLM 只在规则
**歧义**时给一个语义标签（`TREND|SHARE|RANK|COMPARE|RELATION|DETAIL|KPI`），
**绝不产出图表代码**。LLM 拿不到「写 option」这个自由度，也就没有「写出非法 option」
这个失败面。

三条派生契约：

1. **服务端只发结构，不发颜色**（决策 6）。颜色由前端主题层补，见下文「前端」。
2. **任一环节失败都降级为表格，绝不产出空图**。前端的渲染门会渲染空图 —— 用户看到
   一片空白比看到表格更糟。
3. **文字回答仍由 LLM 写**（`_generateAnswer` 未动）。引擎只管图表/表格。

## 模块与依赖方向

| 文件 | 职责 |
|---|---|
| `app/domain/chart_spec.py` | `ChartSpec` / `ChartKind` 数据模型 + 校验（纯数据，无 IO / LLM） |
| `app/services/chart_thresholds.py` | 4 个阈值的 `system_config` 读取（魔数治理） |
| `app/services/chart_decision.py` | 决策引擎：规则表 → 候选集 → 消歧 → `ChartDecision`（纯函数） |
| `app/services/chart_renderer.py` | `ChartSpec` → **不含颜色**的 ECharts option（纯函数，逐 kind 一个 builder） |
| `app/services/chart_spec_builder.py` | kind + columns + data + plan → `ChartSpec`（标签取自 plan 的 alias/target） |
| `app/services/chart_label.py` | 歧义消解分类器（唯一的 LLM 介入点） |
| `app/services/chart_service.py` | 门面：decision → spec → renderer；保住 token 计量口径 |

依赖单向：`chart_service` → `chart_decision` / `chart_spec_builder` → `chart_renderer` → `chart_spec`。

## 输入信号

`plan: QueryPlan|None`、`intent`、`columns`、`data`、`question`。预计算 `ChartSignals`：
列类型、维度/指标/时间列、`hasFormula`（`plan.aggregations[].formula` 非空 = 占比，
由 `validatePlan` 强制）、`isTopN`、`wantCombo`（同比/环比）、`wantWaterfall`、
`questionTrend` / `questionShare` 问句线索。

**ETL 列黑名单**：`ETL_LOAD_TS` / `UPDATE_DATE` / `CREATE_TIME` 等不进时间维，
否则会出现「按 ETL 加载时间画折线」。

## 规则表（首个命中者胜，顺序即优先级）

| 规则 | 判据 | 结果 |
|---|---|---|
| R00 | 无 plan / 无数据 / 无列 | 表格 |
| R01 | 恰好 1 行 + 1 指标 | KPI 指标卡 |
| R01S | 单行 + 多指标或多维 | 表格（一个点的散点图 / 一个格子的热力图都不是图） |
| R02 | `hasFormula` 且行数 ≤ `CHART_PIE_MAX_ROWS` | 环形图 |
| R03 | `hasFormula` 且行数超出 | 横向柱状 |
| R04 | `isTopN` + 1 维 1 指标 | 横向柱状 |
| R05 | 瀑布线索 + 可拆解 1 维 1 指标 | 瀑布 |
| R06 | 同比/环比线索 + 时间维 + ≥2 指标 | 柱线组合 |
| R07 | 有可用时间维 + ≥1 指标 | 折线（**歧义**） |
| R08 | 2 指标 + 1 维 | 散点 |
| R09 | 2 维 + 1 指标且矩阵完备度 ≥ `CHART_HEATMAP_MIN_COVERAGE` | 热力图 |
| R10 | 2 维 + 1 指标但矩阵太稀 | 分组柱状 |
| R11 | 1 维 + 1 指标且行数 > `CHART_HBAR_MIN_ROWS` | 横向柱状 |
| R12S | 问句明说「占比」但计划没算出 formula | 环形（行数少）/ 横向柱状 |
| R12 | 1 维 + 1 指标 | 柱状（**歧义**） |
| R13 | 无聚合 + 非数值列 ≥2（明细） | 表格 |
| R14 | 其余 | 表格 |

### 歧义与消歧

`_AMBIGUOUS_RULES = {R07_TREND_LINE, R12_CATEGORY_BAR}` —— 这两条规则的**形状相同、
语义不同**（占比问句与分类比较问句的数据形状完全一样）。引擎此时返回**候选集**而非
单选题，`chart_label.py` 的分类器从中挑一个：

- 候选只有一个 kind 时**一次 LLM 都不调**（不确定性不存在就没有消歧调用）。
- 问句线索命中且目标 kind 在候选集内 → 直接定案，不调 LLM（`_QUESTION_CUES`）。
  线索表只挂**可达**规则：R12 曾挂过「占比」线索，但占比问句在 R12S 就被接住，
  走到 R12 时线索必为 False（死线索），已删。
- 分类器答的不是白名单标签 → 保留规则原判（不是「换一个」，是解析失败）。
- 分类器异常 → 保留规则原判，`cachedTokens` 等用量照实记账。

## 阈值治理

按 `Harness/rules/魔数治理.md`：`_XXX_DEFAULT` → `system_config` key（去前缀）→
就近 `_getXxx` 读取 → 不缓存 / fail-open / 非正返默认。

| key | 默认 | 含义 |
|---|---|---|
| `CHART_PIE_MAX_ROWS` | 6 | 占比超过这么多行就不画饼 |
| `CHART_HBAR_MIN_ROWS` | 15 | 类目多到该横过来 |
| `CHART_HEATMAP_MIN_COVERAGE` | 0.6 | 交叉矩阵完备度门槛 |
| `CHART_TOP_N_MAX` | 20 | 超过就不算 TOP N |

ETL 黑名单与关键词集合**不治理** —— 它们属解析语法/契约，admin 改了反而挂。

## 标准化 Spec（v1）

```json
{
  "specVersion": 1,
  "kind": "LINE | BAR | HBAR | PIE | DONUT | SCATTER | HEATMAP | KPI | COMBO | WATERFALL | TABLE",
  "title": "供货量 按 供应商",
  "dimensions": [{"field": "SUPPLIER_NAME", "label": "供应商"}],
  "measures": [{"field": "RCV_QTY_PUU", "label": "供货量", "unit": null, "agg": "SUM"}],
  "series": [{"name": "供货量", "kind": "bar", "dimension": "SUPPLIER_NAME", "measure": "RCV_QTY_PUU", "axis": "left"}],
  "orientation": "vertical",
  "sort": {"by": "RCV_QTY_PUU", "order": "desc", "applied": true},
  "showLegend": true,
  "showValueLabels": true,
  "stacked": false,
  "kpi": null
}
```

- **这份 JSON 由代码产出**，不是 LLM 产出。每个字段都能从 `QueryPlan` 与列名确定性
  派生；让 LLM 写只会引入「字段不存在 / kind 非法 / 引用了不存在的列」这类需要再校验的
  失败面。标签来源全部是人类可读串：`plan.aggregations[].alias`、`plan.target`、列名。
- `kpi` 仅 `kind == "KPI"` 时非空：`{"label", "value", "unit", "delta"}`（`delta` 一期恒 null）。
- `kind == "TABLE"` 时渲染器产出沿用既有 `{"columns", "rows"}` 负载，**契约不变**。
- 校验（`validateSpec`）：kind 在枚举内、每个 field 存在于 `columns`、维度 ≤2、指标 ≤2、
  图形类 kind 的 series 非空。**任一校验失败 → 降级 TABLE + warning，绝不产出空图。**
- **`series` 是系列装配的唯一来源**：`name` / `measure` / `axis` 都由 series 声明，
  渲染器读 series（不是自己按位置拼 `measures[index]`）。否则 builder 改一边、渲染器
  另一边不变，而 `validateSpec` 校的是 series —— 「校验通过」与「画得对」就分家了。

## 渲染器

11 个 kind 各一个 builder，输出**不含颜色**的 ECharts option（无 `color`、无
`itemStyle.color`、无轴/文字色）。`renderChartOption` 是**全函数**：任何异常都降级为
表格负载。`_normalizeFormatters` 归一化 `{d}` → `{c}` 保留在渲染器出口（LLM 不再写
option，但渲染器自己产出的 formatter 仍要正确）。`toNumber(value)` 是公开的取值助手
（KPI 路径也用它），排除 bool、保留 None。

## 线上契约

`chartType` + `chartOption` + `data` 三个字段名与语义**保持不变**，前端保持透传；
**0107 起新增** `tableOption` + `visualRationale` 两个负载字段，随同下发的三条路径一起走。

| 位置 | 说明 |
|---|---|
| `ChatResponse` | `chartType: ChartType \| None` + `chartOption: dict \| None` + `tableOption: dict \| None` + `visualRationale: dict \| None`（后两者 0107 新增，仅单步查询路径填值，其余意图为 None） |
| `StepResultRead` / `EVENT_STEP_RESULT` | 每步各带 `chartType` + `chartOption` + `tableOption` + `visualRationale`（多步每步出图，决策 3；失败步骤四字段全 None） |
| **多步顶层** | `chartType`/`chartOption`/`tableOption` = **None**（0107 反转，见下）；`visualRationale` = `{"code": "SUMMARY_TEXT_ONLY", "params": {}}` |
| **多步流式** | 数据步逐条发 `step_result`（各步自带图/表/依据）；汇总步纯文字，`done` 帧携带 `visualRationale`（SUMMARY_TEXT_ONLY）。**不再发 `EVENT_CHART`** —— `_reportChartEvent` 已删 |
| L1 KPI 直答 | 命中即返回 `chartType=kpi` + `{"kpi": {...}}`；值不能转成数字时不发卡（空壳卡比不发更糟）。`tableOption` 为 None（决策 4：KPI 不附表）。**0107 起带 `visualRationale = R01_SINGLE_VALUE_KPI` 并一并落库** —— 原先该快路径 `_buildKpiChart` 只返 2-tuple、**绕开整个 rationale 机制**，于是**出了图却没有任何判断依据**（实时与回放都没有），直接违反「不论是否输出图，必须输出一个判断逻辑」；修复见 commit `7e6d510` |
| `chartOption` 语义 | 仍是 ECharts option，**但不含颜色**；`kpi` 类型不是 ECharts，负载为 `{"kpi": {...}}` |
| `tableOption` 语义 | `{"columns", "rows", "truncated?"}` —— 图之外的明细表投影，**仅图形类 kind 附**；TABLE 的表已在 chartOption、KPI 单值卡没有表，这两种 kind 下为 None。截断阈值复用 `FULL_DATA_THRESHOLD`（默认 100），落库再按 `_PERSIST_MAX_TABLE_ROWS`（200）兜底 |
| `visualRationale` 语义 | `{"code": str, "params": dict}` 结构化判断依据（为什么用这个图 / 为什么不画）。**后端只出 code + 插值变量，文案在前端 i18n**（21 个 code）。**不进 PDF**（决策 6） |

### 多步顶层行为反转（0105 → 0107）

0105 的「多步顶层 = 最后一个成功数据步骤的图」是为了让图进最终回答（导出 PDF / 历史回放）。
但每个数据步的卡片已各挂各的图（决策 3），顶层再重复同一张图是**冗余**：多步汇总的语义是
**文字结论**，图表是各数据步骤的产物，继承来的那张「最后一步的图」会与各步骤的图**重复，
且误导**读者以为汇总步骤自身产出了图。

0107 起改为：**顶层最终回答无图** —— `chartType`/`chartOption`/`tableOption` 全 None，只带
`visualRationale = SUMMARY_TEXT_ONLY`（「汇总为文字结论，各步骤图表见上方」），解释为什么
这里没有图。**单步不受影响**：最终回答本身就是那一步，图 + 折叠数据表 + 依据照常。

#### ⚠️ 被接受的代价（**已拍板，不要当 bug 修**）

各数据步的图**只活在响应里**（`step_result`/`steps[]`），**从不落库** —— `session_message` 只有顶层一行，
而顶层行现在 `chart_type = NULL`。⇒ **多步对话在服务端消息流里一张图都没有**，两个可见后果：

- **PDF 导出零图**：`collectExportCharts.ts` 是按**服务端消息流**的 `message.chartType` 挑图截图（见下方「导出报告里的图」），
  多步对话因此**导不出任何图**。0105 时这里还有「最后一个数据步」那张。
- **刷新回放零图**：`chatStore.toChatMessage` **不重建 `steps`**（导入表无 steps 路径、`ChatMessageRead` 也不带），
  多步对话回放为**纯文字 + SUMMARY_TEXT_ONLY**，哪里都没有图。「展开步骤卡看各步图」**只对未刷新的实时会话成立**。
- **实时（未刷新）视图不受影响**：各数据步的图/表/依据仍在**步骤卡**里正常显示。

**💡 这是用户 2026-10-01 明确选择「接受」的取舍**（方案 1）—— **主动放弃了 0105
「为多步对话导出最后一步那张图」的能力**。被否决的两个替代方案（记录在案以免反复重提）：

| 方案 | 内容 | 为什么否决 |
|---|---|---|
| 2 | 在汇总行持久化「最后一个成功数据步」的图（即恢复 0105 行为） | **重新引入「实时 vs 回放」不对称** —— 刷新后汇总行有图、实时却不显示，等于把决策 1 想消除的「继承图误导」换个地方复现 |
| 3 | 持久化**每一步**的图 | 需要新的存储形态决策（新表或 JSONB 数组），成本远超收益 |

e2e 已按本决定断言多步 `charts.length === 0`（**与决定一致，不要改这个断言**）。

## 前端

| 位置 | 契约 |
|---|---|
| `frontend/src/types/chat.ts` | `ChartType` 联合类型（11 类 + table）+ `TablePayload` / `VisualRationale` 两接口（0107） |
| `frontend/src/utils/chartContract.ts` | **图表字段的唯一收窄口径**（0105 抽出）。`VALID_CHART_TYPES` **必须与后端同步** —— 漏同步的表现是「图不见了但没有任何报错」（未知类型被静默置 null）。**为什么单独一个模块**：收窄规则描述的是**线上契约**不是 HTTP 传输，三个消费者分属三层（SSE/HTTP 响应、历史回放、导出挑图）；留在 `api/chat.ts` 会让 `stores/chatStore` 为一个谓词反向依赖 HTTP 客户端。0107 新增 `asTablePayload` / `asVisualRationale`（**不内置 code 白名单** —— rationale 漏一个 code 只是说明行退化成英文码，代价不对称） |
| `frontend/src/api/chat.ts` | 消费上面的收窄，**流式与非流式同一道**（`normalizeChatResponse` / `normalizeStepResult` / `chart` 事件 / `done` 事件的 `visualRationale`） |
| `frontend/src/components/chat/MessageItem.tsx` | 渲染门为 `Boolean(chartType)`（不是 `chartType && chartOption`：KPI 卡没有 ECharts option 语义）。**消息级 `visualRationale`**（多步汇总 SUMMARY_TEXT_ONLY）渲染为答案下方次要色一行，**只在无顶层图时兜底**，避免与 ChartRenderer 的依据画两行 |
| `frontend/src/components/chat/ChartRenderer.tsx` | `table` → antd Table；`kpi` → `KpiCard` + CSV 导出；其余 → `ReactECharts` + PNG 导出。**表格自取自足**：`data` 缺省时用 `chartOption.rows/columns` 渲染（多步每步不铺全量 data）。**表格带 `truncated: true` 时在下方补一行次要色说明**（落库截行）。0107：图形类在图下方加 antd `Collapse`「数据表」（`defaultActiveKey=[]` **默认折叠**，`tableOption` 为 null 不渲染）+ rationale 次要色一行（**所有形态都渲染**，含「为什么不生成图表」）；TABLE/KPI 不重复出表 |
| `frontend/src/components/chat/MultiStepPlanCard.tsx` | 每个 step 挂同一个 `ChartRenderer`（图跟着步骤走，不错位），并透传该步的 `tableOption` / `visualRationale` |
| `frontend/src/utils/visualRationale.ts` + `i18n/zh-CN.ts`/`en-US.ts` | `visualRationaleText(rationale, t)` 把结构化依据转成文案；`params.kind` 先经 `chatPanel.chartTypes.<kind>` 本地化图型名再插值；缺 key 回退显示 code 原文。i18n 各一套 `chat.visual.<code>`（**21 个 code**），插值用**单花括号** `{rows}`/`{kind}`（本仓 i18next 被覆写为 `{`/`}`，双花括号不插值） |
| `frontend/src/theme/chartTheme.ts` | `applyChartTheme(option, token)` —— 不可变注入调色板/轴色/文字色/tooltip 底色；调用方显式写的 `itemStyle.color`（语义色）优先不被覆盖 |
| `frontend/src/theme/tokens.ts` | `chartPalette`（两套主题各一套分类系列色） |
| `frontend/src/utils/chartSnapshot.ts` | `renderChartPng(option)` → `data:image/png;base64,...`。离屏容器**显式 800×420**（零尺寸容器里 ECharts 拿到 0×0 画布）、**固定亮色 token + 白底**（PDF 页面是白的）、关动画、`finally` 里 dispose + 移除容器、**任何异常返回 `null`**。`needsSnapshot(kind)` 对 `table`/`kpi` 返回 false |
| `frontend/src/utils/collectExportCharts.ts` | 导出前挑图：GET 服务端消息流（**`?tail=true` 取最新**，与导出的「最近 500 轮」窗口对齐）→ 按 `SessionMessage` 主键挑需要截图的轮次 → 离屏渲成 PNG。**以服务端消息流为准而非屏幕列表**（实时会话的前端消息没有 DB id；必须覆盖滚出屏幕的轮次；服务端 `chartOption` 才是落库那份）。截断到 180 张时**保最新**；渲染**块内并发 4、块间串行**（`renderChartPng` 在 `await` 前是同步的，`Promise.all` 会一次性建出全部离屏画布 ≈ 1 GB 后备存储） |

## 导出报告里的图（0105）

**PDF 渲染三档降级**：`原生 table/kpi（从 chart_option 画，不吃图）> 回传的 PNG > 灰色占位框`。
`table` 排最前是刻意的 —— 它是**文字**，比位图清晰，且**前端截图失败时表格照样出**；
占位框留给改动前的历史消息（没有 `chart_option`）。

**导出端点由 GET 改 POST**（契约变更，前后端必须同批）：

```
POST /api/v1/sessions/{sessionId}/export.pdf
{ "messageId": 123, "charts": [{ "messageId": 123, "imagePng": "data:image/png;base64,..." }] }
```

`charts` 缺省为空 ⇒ 等价于旧行为。**图缺失不影响导出本身的成败**（导出是主功能，图是增强）。
边界校验（超限一律 422）：单图解码后 ≤ 2 MB、条数 ≤ 200、总量 ≤ 20 MB、单图 ≤ 8 M px、
全部 ≤ 40 M px（**逐张累加即判**，不是全解完再量体）、`charts[].messageId` 必须属于该 session。

**这条前端调用必须自己带 `Authorization`**：它刻意绕开 `httpClient`（二进制响应走不了
`ApiResponse` 信封解包），代价是**连请求拦截器的 Bearer 注入一起绕开**，而 `sessions` router
是 **router 级**鉴权（`session.py` 的 `APIRouter(dependencies=[Depends(getCurrentUser)])`）
⇒ 没 Bearer 一律 403「请先登录」。所以头由 SSOT `api/authHeaders.ts` 提供，
**不要**再手搓 `X-User-Id` 之类 —— nginx 会把 `X-User-*` 整族剥掉（曾因此静默 403，
且单测因 mock 里虚构了 `X-User-Id` 而看不见）。

**两张闸必须都在，且不能只卡字节**：一张 12000×12000 的纯色 PNG 压缩后只有 580 KB ——
字节闸放行、PNG 魔数也对，交给 Pillow 却要按 1.44 亿像素分配内存（实测单张峰值 RSS +2 GB）。
而 PIL 默认 `MAX_IMAGE_PIXELS = 89_478_485` 只在 **2 倍以上**才抛异常，中间一段不设防。
所以像素数在**任何解码之前**从 IHDR 头自己读（`_pngPixelCount`，纯 stdlib）；读不出 IHDR
（截断/畸形）按「不是合法 PNG」拒掉，不交给 Pillow 赌它报不报错。

**位图缩放要夹两个轴**：只夹宽度时，一张 100×4000 的窄高图宽度合规、高度溢出页面，
reportlab 会在 `doc.build()` 里抛 `LayoutError` —— 那已经出了 `_chart_image_flowable` 的
`try`，于是整份导出 500 而不是降级成占位框。取 `min(框宽/图宽, 框高/图高, 1.0)`。

**为什么是「前端渲染 → 回传 PNG」而不是服务端渲染**：装无头浏览器会让后端镜像陡增；
用 matplotlib 重画等于**第二套渲染器**（与前端的图必然长得不一样，违反 SSOT），
且 `builder.build(payload)` 是**同步调用跑在 async handler 里**，服务端光栅化会阻塞事件循环。
项目对同类问题的既有判断同向（见 `evaluation_report_export_service.py`）。

## 已知边界（一期）

- **地图二期**：需要 GeoJSON + `registerMap` + 地域名映射。当前本体**没有地域属性**
  （`PROVINCE|CITY|REGION|省|市` 命中 0 行），所以地域→地图当前不可达。
- **瀑布/柱线组合一期基本触发不到**：需要计划侧的数据形状配合，不匹配时**确定性降级**
  （→ 柱状 / 柱线组合）。这是设计内的降级，不要误判为 bug。
- **热力图门槛最软**：完备度是按「行数 / (维1基数 × 维2基数)」估的，真实 SQL 结果常是
  稀疏交叉表，命中率可能很低。门槛已进 `system_config`，观察真实命中率后调。
- **占比 vs 分类比较仍可能判错**：`formula` 兜住显式占比；用户问「A 占比多少」而
  NL2SQL 没生成 formula 时落到 R12 歧义分支 —— 这正是分类器存在的理由，也是它唯一的
  判错面（可测、可回归）。
