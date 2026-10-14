# 可视化输出策略（图 / 表 / 图+表 + 判断依据）设计方案

> 状态：**待评审**。本文档是设计方案，落地后另写 summary.md 作为 SSOT。
> 依据：用户 2026-10-01 需求 —— 单问 / 多步 / 多轮，每轮回答都要判断输出
> 图、表、还是图+表；有数据清单默认给表；是否出图由 Visualization Rule Engine 判定；
> **必须输出判断逻辑**（为什么用这个图 / 为什么不出图），最终总结同口径；
> 多步每步可出图+表，最后的汇总通常不出图表。

## 1. 现状（已核实到 file:line）

| 事实 | 位置 |
|---|---|
| 决策引擎规则表 R00–R14，纯函数，`ChartDecision.ruleId` 记录命中规则 | `services/chart_decision.py` |
| `ruleId` / `candidates` / `degradeReason` 只在日志与测试可见，**不发前端** | `chart_service.py:120-125` |
| 契约是 `chartType` 单值（11 图型 + TABLE）—— 图 XOR 表，**无图+表组合** | `Harness/wiki/chart-rendering.md` §线上契约 |
| 多步每步各带 `chartType/chartOption`；顶层 = 最后成功数据步那张（0105） | `chat_multistep.py:693-694, 716-717` |
| 汇总步（aggregation_only）本身不出图 | `chat_multistep.py:601` |
| 前端渲染门 `Boolean(chartType)`；表格自取自足（`chartOption.rows`） | `MessageItem.tsx` / `ChartRenderer.tsx` |
| 落库仅 `session_message.chart_type/chart_option` 两列（0105） | `models.py:792-793` |

⇒ 缺口正好三个：① 图+表组合不存在；② 判断依据不外露；③ 汇总步没有「为什么不出图」的说法。

## 2. 目标行为矩阵

| 场景 | 决策结果 | 输出 | rationale 示例 |
|---|---|---|---|
| 单步 / 多轮追问 | 图形类（bar/line/pie/…） | **图 + 数据表** | 「TOP N 排名数据 → 横向柱状图，附数据表」 |
| | TABLE（明细 R13/R14） | 仅表 | 「明细清单（无聚合）→ 表格呈现，不生成图表」 |
| | TABLE（spec 降级） | 仅表 | 「数据结构不满足热力图要求 → 降级为表格」 |
| | KPI | 仅 KPI 卡 | 「单一聚合值 → 指标卡」 |
| | 空数据（R00） | 空表 | 「查询无结果，不生成图表」 |
| 多步每个数据步 | 同上 | 同上（挂在各步卡片） | 同上 |
| **多步汇总步** | — | **仅文字，无图无表** | 「汇总为文字结论，各步图表见上方步骤」 |

三条不变的原则（沿用 chart-rendering.md）：画什么图由代码决定不由 LLM 决定；
任一环节失败降级表格；文字回答仍由 LLM 写。rationale 也**不由 LLM 写**——
它是 ruleId 的确定性映射，零额外 LLM 调用、零成本。

## 3. 后端设计

### 3.1 契约扩展（向后兼容）

`ChatResponse` / `StepResultRead` / SSE `step_result` / `chart` 事件统一增加两字段：

```
tableOption:      { columns, rows, truncated } | null   // 新增
visualRationale:  { code: string, params: dict } | null // 新增（结构化，前端 i18n）
chartType/chartOption/data: 语义不变
```

组合规则（chart_service 出口的装配层，纯函数）：

| spec.kind | chartType | chartOption | tableOption |
|---|---|---|---|
| 图形类 | kind | ECharts option | **表负载**（同一份 data，截断口径复用落库截行） |
| TABLE | table | 表负载 | null（不重复，chartOption 本身就是表） |
| KPI | kpi | `{"kpi":…}` | null（单行数据表无信息量） |
| 汇总步 | null | null | null |

### 3.2 rationale 生成（纯函数，零 LLM）

`buildVisualRationale(decision, spec, degradeReason) → {code, params}`：
`code = ruleId`（R01–R14 / R00_EMPTY_TABLE / R_FORCED_CLIENT）或
`DEGRADE_*`（spec 校验降级）/ `SUMMARY_TEXT_ONLY`（汇总步）；params 携带
行数、维度数、指标数、目标 kind 等插值参数。**前端按 code 做 i18n 模板渲染**，
契约语言无关（详见待拍板 3）。

### 3.3 多步汇总步：取消顶层继承图（反转 0105 的多步部分）

0105 的「顶层 = 最后成功数据步的图」是为了让图进最终回答；但每步卡片已各挂各的图，
顶层重复同一张图是冗余。改为：

- 汇总步 `chartType/chartOption/tableOption` 全 null + `rationale.code = SUMMARY_TEXT_ONLY`
- **单步不受影响**：最终回答本身就是那一步，图+表照常
- SSE 多步终点不再发 `_reportChartEvent` 那张继承图（或保留事件但 kind=null，待定，见待拍板 1）

### 3.4 落库（alembic 0107）

`session_message` 加两列：`table_option JSONB NULL`、`visual_rationale JSONB NULL`。
理由：历史回放与导出 PDF 要还原同样的图+表+依据；塞进 chart_option envelope
可免迁移但污染既有契约（前端三处收窄、PDF 原生表格渲染都读它），不取。

## 4. 前端设计

| 位置 | 改动 |
|---|---|
| `types/chat.ts` / `utils/chartContract.ts` | 收窄加 `tableOption`/`visualRationale`；**四条路径同一道**（SSE/非流式/历史回放/导出）——上次「收窄只做一条路径」已分叉过一次 |
| `ChartRenderer.tsx` | 图形类：图 + 下方折叠「数据表」区块（默认折叠，待拍板 2）；rationale 渲染为图表下方次要色一行说明 |
| `MultiStepPlanCard.tsx` | 每步同一个 ChartRenderer；汇总步只渲染 rationale 行（「为什么不出图」也必须可见） |
| i18n | zh-CN / en-US 各加一套 `chat.visual.*` 模板（按 code 插值） |
| 导出 PDF | 表格原生渲染已有，不变；rationale 不进 PDF（待拍板 6） |

## 5. 测试要点

- 后端：`buildVisualRationale` 纯函数单测（每个 ruleId × 降级路径 × 汇总步）；装配层组合矩阵（图+表/仅表/仅 KPI/全 null）双向断言；契约四路径收窄测试
- 前端：ChartRenderer 三形态渲染；汇总步 rationale 可见；chartContract 收窄（非法 tableOption 置 null 不炸）
- 集成：单步图+表、多步每步图+表且汇总步无图、多轮追问各自独立判定

## 6. 待拍板（一次性评审）

1. **多步顶层继承图取消**（反转 0105 的多步部分）——需求原文「最后一个总结通常不需要输出图或表」，按取消设计；若保留则汇总步 = 图+表+文字三件套，冗余。
2. **图+表时表格默认折叠还是展开** —— 推荐折叠（图为主、表备查），明细场景（仅表）自然展开。
3. **rationale 载体**：结构化 `{code, params}` + 前端 i18n（推荐，契约语言无关、文案集中治理）vs 后端直出中文串（简单但英文界面破功）。
4. **KPI 不附表**（推荐）vs 附单行表。
5. **落库加两列（0107，推荐）** vs 塞 chart_option envelope（免迁移但污染契约）。
6. **rationale 是否进导出 PDF** —— 推荐不进（PDF 是结果文档，判断依据是交互辅助）。
7. **前后端必须同批部署**：契约新增字段 + `VALID_CHART_TYPES` 式同步教训（上次漏同步图静默消失）。本次虽只加字段不改枚举，仍按同批执行。
8. **数据表截断行数**：沿用 `FULL_DATA_THRESHOLD=100`（推荐）还是单独一个 `VIS_TABLE_MAX_ROWS`。

## 7. 任务分解预览（评审通过后细化）

1. 后端：`buildVisualRationale` + 装配层组合规则 + 契约字段（TDD）
2. 后端：汇总步去继承图 + SSE 事件调整
3. 后端：alembic 0107 + 落库/回放/导出接线
4. 前端：契约收窄 + ChartRenderer 三形态 + i18n
5. 前端：汇总步 rationale + e2e（chart_report_e2e.mjs 改断言）
6. 文档：chart-rendering.md 更新 + 本目录 summary.md
