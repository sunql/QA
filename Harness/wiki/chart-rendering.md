# 图表决策引擎 + 标准化 Spec + 渲染器

> 2026-09-30 重写。旧文描述的是「LLM 直接写 ECharts option」的契约，那套已经不在了；
> 变更记录见 `Harness/changes/2026-09-30-chart-decision-engine/`。

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

`chartType` + `chartOption` + `data` 三个字段名与语义**保持不变**，前端保持透传。

| 位置 | 说明 |
|---|---|
| `ChatResponse` | `chartType: ChartType \| None` + `chartOption: dict \| None` |
| `StepResultRead` / `EVENT_STEP_RESULT` | 每步各带 `chartType` + `chartOption`（多步每步出图，决策 3） |
| L1 KPI 直答 | 命中即返回 `chartType=kpi` + `{"kpi": {...}}`；值不能转成数字时不发卡（空壳卡比不发更糟） |
| `chartOption` 语义 | 仍是 ECharts option，**但不含颜色**；`kpi` 类型不是 ECharts，负载为 `{"kpi": {...}}` |

## 前端

| 位置 | 契约 |
|---|---|
| `frontend/src/types/chat.ts` | `ChartType` 联合类型（11 类 + table） |
| `frontend/src/api/chat.ts` | **`VALID_CHART_TYPES` 必须同步** —— 漏同步的表现是「图不见了但没有任何报错」（未知类型被静默置 null）。收窄对**流式与非流式同一道**（`normalizeChatResponse` / `normalizeStepResult`） |
| `frontend/src/components/chat/MessageItem.tsx` | 渲染门为 `Boolean(chartType)`（不是 `chartType && chartOption`：KPI 卡没有 ECharts option 语义） |
| `frontend/src/components/chat/ChartRenderer.tsx` | `table` → antd Table；`kpi` → `KpiCard` + CSV 导出；其余 → `ReactECharts` + PNG 导出。**表格自取自足**：`data` 缺省时用 `chartOption.rows/columns` 渲染（多步每步不铺全量 data） |
| `frontend/src/components/chat/MultiStepPlanCard.tsx` | 每个 step 挂同一个 `ChartRenderer`（图跟着步骤走，不错位） |
| `frontend/src/theme/chartTheme.ts` | `applyChartTheme(option, token)` —— 不可变注入调色板/轴色/文字色/tooltip 底色；调用方显式写的 `itemStyle.color`（语义色）优先不被覆盖 |
| `frontend/src/theme/tokens.ts` | `chartPalette`（两套主题各一套分类系列色） |

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
