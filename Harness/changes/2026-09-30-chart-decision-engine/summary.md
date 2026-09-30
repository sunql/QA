# 变更：图表决策引擎 + 标准化 Spec + 渲染器

- **日期**：2026-09-30
- **作者**：Claude / 启琳
- **Phase**：图表渲染（`app/domain/chart_spec.py` + `app/services/chart_{thresholds,decision,renderer,spec_builder,label,service}.py` + 前端 `types/chat.ts` / `api/chat.ts` / `theme/chartTheme.ts` / `components/chat/{ChartRenderer,KpiCard,MultiStepPlanCard,MessageItem}.tsx` / `components/EChart.tsx`）
- **状态**：done（已提交到 `feat/chart-decision-engine`）
- **关联变更**：[feat-frontend-theme-echarts-tokens](../feat-frontend-theme-echarts-tokens/summary.md)（主题 token 的前置）、[chore-chart-service-deadcode](../chore-chart-service-deadcode/summary.md)（同文件的上一轮清理）
- **迁移版本**：无（阈值走 `system_config` 种子，无 schema 变更）
- **SSOT 出处**：`Harness/wiki/chart-rendering.md`（本变更重写）
- **commit**：`3ab48ca`（后端）+ `5f5fd91`（前端）+ 本记录（文档）；分支 `feat/chart-decision-engine`

---

## 1. 需求

用户原话两条：

> 「能否增加一个判定的引擎，**不能完全让LLM判定**」
> 「输出的结果不要直接让LLM调用类似EChars代码绘图，**最好能够先输出一个json，
> json的格式做成标准化的，由系统一个专门的代码转换成文字输出+图或者表输出**」

改造前「画什么图」由两段逻辑决定，都不受控：

1. `ChartService.recommendChartType` **只看列形状**（列类型 + 行数），只有 4 个出口
   （TABLE/PIE/BAR/LINE），**永远不返回 SCATTER**，且「饼图最多 6 行」硬编码在规则里。
2. 定了类型之后，**LLM 直接写 ECharts option JSON**。

三个已实测的后果：

- **语义判不出来**：「供应商 + 数量」两列，占比问句与分类比较问句形状完全一样，引擎
  无法区分。
- **月份列出不了折线**：`infer_column_type` 的 TIME 判定要求 `^\d{4}[-/]\d{2}[-/]\d{2}`，
  所以 `202605` / `6月` 全判成 STRING → 落进「1 字符串 + 1 数值」→ 出饼图而不是折线。
- **多步完全不出图**：`chat_multistep.py` 里 `ChartType` 出现 0 次，`chartType` 恒为 null。

**验收标准**：选型由规则表决定且可测；LLM 只在规则歧义时输出一个语义标签；图型与
渲染负载由标准化 spec 派生；多步每步出图；L1 KPI 直答接指标卡；线上契约
（`chartType`/`chartOption`/`data`）不变。

## 2. 设计评审（用户已确认的 8 项决策）

| # | 决策 |
|---|---|
| 1 | spec 驱动**图表/表格**；文字回答仍由 LLM 写 |
| 2 | 一期 11 类；**地图二期** |
| 3 | 多步：**每个 step 各出一张图** |
| 4 | 引擎**规则优先**；LLM 只做兜底分类，只输出语义标签 |
| 5 | 同比环比 / 瀑布一期只做引擎，形状不匹配时**确定性降级** |
| 6 | **服务端只发结构（不含颜色）**，前端套主题色 |
| 7 | L1 KPI 直答一并接指标卡 |
| 8 | 线上契约 `chartType` + `chartOption` + `data` 保持不变 |

两处与原始表述的偏差（已与用户对齐）：

- **标准化 JSON 由代码产出，不由 LLM 产出**。spec 的每个字段都能从 `QueryPlan` 与列名
  确定性派生，让 LLM 写只会引入「字段不存在 / kind 非法 / 引用了不存在的列」这类需要
  再校验的失败面。用户要的「标准化 JSON 驱动渲染」这个结果完全达成。
- **`applyChartTheme(option, token)` 不接 `isDark`**（计划文本里带了它）。轴色/文字色/
  tooltip 底色都从 token 取，而 token 本身就是按主题选的；再多传一个 isDark 等于给同一件
  事留第二个真相来源。

## 3. 落地内容

| 任务 | 产出 |
|---|---|
| 1 | `domain/chart_spec.py`：`ChartSpec` + `validateSpec`（失败 → TABLE + warning） |
| 2 | `services/chart_thresholds.py`：4 个 `system_config` 阈值（魔数治理 3 例/读取函数） |
| 3 | `services/chart_decision.py`：R00–R14 + 候选集 + ETL 黑名单 + `_QUESTION_CUES` |
| 4 | `services/chart_renderer.py`：11 个 builder，**逐键断言无颜色**；全函数降级 |
| 5 | `services/chart_spec_builder.py`：plan alias/target 派生标签 |
| 6 | `services/chart_label.py`：白名单分类器 + 失败回退 + token 计量 |
| 7 | `services/chart_service.py`：门面接线；`_chartStep` 下传 plan；旧形状规则与 LLM 写 option **删除** |
| 8 | 多步每步出图：domain → schema → mapper → SSE 事件 → `_executeDataStep`；分类器**每轮最多 1 次** |
| 9 | L1 KPI 直答：`_buildKpiChart`（复用渲染器，不手搓结构）；`_firstPresentValue` 统一取値口径 |
| 10 | 前端：类型/白名单/i18n（12 键）/选择器/渲染门放宽为 `Boolean(chartType)`/多步每步挂渲染器 |
| 11 | 前端：`chartPalette` + `applyChartTheme` + `KpiCard`；`EChart.tsx` 同路径接主题 |
| 12 | 文档：重写 `Harness/wiki/chart-rendering.md`、更新 `chat-service-capabilities.md`、本变更记录 |

### 过程中发现并修掉的两个真缺陷（均为 TDD 先红后绿）

1. **死信号**：`questionTrend` / `questionShare` 被计算但从未被消费 —— R07/R12 的歧义
   永远要靠 LLM 分类器。修法：`_QUESTION_CUES` + `_isAmbiguous`（线索命中且目标 kind 在
   候选集内就不再歧义）+ 新规则 **R12S**（问句明说「占比」但计划没算出 formula）。
2. **歧义翻转丢轴**：`_dimensionFields` 把「时间轴替换」的条件挂在 kind 上，于是
   LINE→BAR（消歧翻转）后 spec 维度为 0 → `coerceSpec` 降级 TABLE。修法：抽出
   `_axisFields(signals)` —— 时间轴的选法只取决于 signals，与最终 kind 无关。

### 一处语法错误的自我纠正

`test_chat_service.py` 的假 LLM 在删除「图表 JSON」分支时把 `if "解析为查询计划"`
的函数体一并删掉（留下悬空 `elif`）。**是收集期 IndentationError**，被
`test_chat_service.py` 的后续运行发现并修复。

## 3.5 代码评审与修复（2026-09-30，同一批未提交改动）

两组评审各跑一遍：`typescript-reviewer`（前端）+ `code-reviewer`（后端，结论 APPROVE：
0 CRITICAL / 0 HIGH / 2 MEDIUM / 4 LOW）。**报出来的问题全部处理，没有「记下来以后再说」**。

### 修复轮复评（同一批评审员，只审这一轮的 diff）

两个 scoped 复评都是 **APPROVE**：逐条把上面 6 + 9 个修复对着代码验了一遍（CONFIRMED/REFUTED），
没有发现修复引入的新问题。剩下的两条观察也一并处理掉：

- 前端：KPI 的导出按钮要求 `rows` 非空 —— 只有 `{kpi: {...}}` 时导出的是空表，
  与其给一个点了没内容的按钮，不如不给。已在代码里写清这条取舍（现有两条链路都带 `data`）。
- 后端：复评指出散点**生产形状**（builder 只发一条 series）下的 y 轴回退没有直接用例
  —— 补 `test_scatter_with_production_shape_resolves_y_from_position`。

### 后端（评审 MEDIUM/LOW → 修复）

| # | 级别 | 问题 | 修法 |
|---|---|---|---|
| 1 | MEDIUM | **单行多指标/多维退化成 degenerate 图**：`SELECT SUM(amount), SUM(qty)` 不带 GROUP BY → R08 散点只画一个点；单行两维 → R09 热力图只有一个格子 | 新增规则 **R01S**（单行 + 多指标或多维 → 表格），TDD 先红后绿（6 例） |
| 2 | MEDIUM | `SpecSeries.name/dimension/measure/axis` 被 builder 构造、被 `validateSpec` 校验，**渲染器却按位置自己拼一遍** → 两个真相源（改一边另一边不变，而校验校的是 series） | 渲染器改读 `spec.series`：新增 `_seriesNameOf` / `_seriesAxisIndex`，`_measureOf` 优先取 `series[i].measure`；series 缺失时仍按位置兜底（防御）。3 例测试 |
| 3 | LOW | `_QUESTION_CUES["R12_CATEGORY_BAR"]` 是**死线索**（占比在 R12S 就被接住，走到 R12 时 `questionShare` 必为 False） | 删除，并加一条「线索表只挂可达规则」的守卫测试 |
| 4 | LOW | `chart_service` docstring 说强制 HEATMAP 在 1 维数据上「降级成柱状」，实际 `coerceSpec` 的降级终点**永远是表格** | 改正注释（降级终点 = 表格） |
| 5 | LOW | `_stepChart` 兜底把 token 记成 `0,0,0`：若异常发生在记账那一句（分类器已经真的花了 token），用量少报且台账缺行 | `_chartStep` 自己兜住记账异常：error 日志 + **已知 token 数照常返回**。2 例新测试（`test_chat_usage_chart_metering.py`），其中一例就是「记账抛错仍返回 120/30/64」 |
| 6 | LOW | `chat_service` 从渲染器 import 私有 `_asNumber` | 提为公开 `toNumber`（3 文件 13 处重命名），KPI 路径不再依赖渲染器内部实现 |

### 前端（评审 2 HIGH / 3 MEDIUM / 4 LOW → 修复）

| # | 级别 | 问题 | 修法 |
|---|---|---|---|
| 1 | **HIGH** | KPI 卡出的是「导出 PNG」按钮，但 KPI 不是 ECharts → 点了必然失败（`showToolbar` 已含 KPI，按钮分支只判 `isTable`） | 按钮分支改 `isTable \|\| isKpi ? CSV : PNG`；3 例测试（按钮种类、CSV 内容、原有 PNG 用例不动） |
| 2 | **HIGH** | 多步每步的 `data` 到不了渲染器 → `table` 子步骤**渲染成空表** | 让表格**自取自足**：`data` 缺省时读 `chartOption.rows/columns`（服务端 TABLE 负载本就带 rows，不必把全量 data 存进 store）。3 例测试 |
| 3 | MEDIUM | 白名单只对 SSE 帧生效，非流式响应原样透传 → 同一份数据两条路径表现不同 | 收窄上移到 `sendMessage`（`normalizeChatResponse` + `normalizeStepResult`），两路径共用 `normalizeChartType` / `asChartOption`。3 例测试（含 steps 逐项 + 不带 steps 不伪造空数组） |
| 4 | MEDIUM | `CHART_TYPE_KEYS` 是第三份手工清单，`value: string` + `as ChartType` 把风险推给运行时 | `value: ChartType \| "auto"` + `<Select<ChartType \| "auto">>`，删掉 cast；补 6 例「选中新类型后发送回传该 chartType」 |
| 5 | MEDIUM | `parseKpiPayload` 丢掉数值字符串、放过 `NaN`（antd 会画出「NaN」） | 新增 `asFiniteNumber`（数字/十进制字符串、拒非有限值），新建 `KpiCard.test.tsx` 7 例 |
| 6 | LOW | `withAxisTheme` 整体替换 `lineStyle`，丢掉虚线/宽度等样式键 | 抽 `withLineColor` 合并而非覆盖；1 例测试 |
| 7 | LOW | `normalizeStepResult` 的 `chartOption` 只做类型断言，不验证是不是对象 | `asChartOption` 收窄（数组/字符串 → null） |
| 8 | LOW | 表格 `rowKey` 用 index（antd 已弃用该参数，控制台告警） | 渲染前给每行配 `__rowKey`（`useMemo`，CSV 导出仍用原始行） |
| 9 | LOW | PNG 导出硬编码 `backgroundColor: "#fff"`（暗色下导出白底） | **未改**：这是本次改造之前的既有行为，且已有测试钉住；导出为白底便于分享/打印，属产品取舍，另开议题 |

## 4. 验证

**后端**（`TEST_DATABASE_URL` → 独立 PG 容器 5434 / `qa_metadata_test`）

| 套件 | 结果 |
|---|---|
| `test_chart_spec.py` / `test_chart_renderer.py` / `test_chart_spec_builder.py` / `test_chart_thresholds.py` / `test_chart_service.py` / `test_chart_label.py` / `test_chart_decision.py`（含新增 R01S 6 例、死线索守卫、spec-series 3 例、生产形状散点 1 例） | **222 passed**（评审修复后复跑） |
| `test_chat_usage_chart_metering.py`（新，记账失败边界 2 例）+ `test_chat_usage_cost.py` | **14 passed** |
| `test_chat_multi_step.py` + `test_chat_api.py` + `test_chat_l1_routing.py`（真实 API 链路） | **50 passed** |
| `test_chat_l1_kpi_chart.py`（新，15 例） | **15 passed**（也包含在下面那组的 174 passed 里） |
| `test_chat_service.py` + `test_chat_service_stream.py` + `test_kpi_semantic_match_service.py` + `test_chat_l1_kpi_chart.py` | 45 failed / **174** passed。同一批文件在 **HEAD 基线**（`git worktree add` 隔离复跑）为 45 failed / **159** passed —— 失败集合不变，差额 15 = 新增的 KPI 卡用例。这 45 红是 M3 空计划闸门 × 陈旧替身的历史债（memory `qa-system-chat-suites-stale-doubles-45-red`），**不是本变更引入** |

**前端**

- `npx tsc -b` 通过（评审修复后复跑）。
- `npx vitest run`：**1285 passed / 2 failed**。两项都不是本变更引入：
  `EntityMappingPage > 点击新建并提交调用 createMapping` 已用 `git worktree add` 在
  **HEAD 基线**复现（同样失败）；`AgentRegistryPage` 在全量并发下偶发失败、**单跑 5/5 全绿**。
- 新增/扩展测试：`chartTheme.test.ts`(11)、`EChart.test.tsx`(3)、`MultiStepPlanCard.test.tsx`(5)、
  `KpiCard.test.tsx`(新，7)、`ChartRenderer.test.tsx`(+9)、`chatApi.test.ts`(+12)、
  `ChatPanel.test.tsx`(+6 类型选择)、`chatStore.test.ts`(+2)、`i18n.test.ts`(两字典齐全)。
- ⚠️ `npm run lint` 在本仓库**本来就跑不起来**（ESLint 10 找不到 `eslint.config.*`，仓库仍是
  `.eslintrc` 格式）—— 与本次改动无关，未修。

**未做**（用户明确延后）：`docker compose build backend && docker compose up -d backend`；
本次改动只经 `./scripts/deploy_backend.sh` 的 docker-cp 路径上过容器。

## 5. 风险与后续

1. **`chartType` 枚举扩容是破坏性的**：前端 `VALID_CHART_TYPES` 漏同步 → 新类型静默变
   null。**后端与前端必须同批发布**（本变更已同批）。
2. **热力图门槛最软**（干净度按行数/基数估算）；观察真实命中率后调 `system_config`。
3. **瀑布 / 柱线组合一期基本触发不到**（缺数据形状）；它们会稳定降级，不要误判为 bug。
   同理 **R01S**：单行多指标/多维会稳定落成表格（评审发现的实际形状），不是丢图。
4. **暗色/亮色各调过一次 `chartPalette`**，但热力图 `visualMap` 渐变色带仍是 ECharts 默认
   —— 若后续要统一，需再开一个 `inRange.color` 注入点。
5. 前端主题路径（`applyChartTheme`）只做「补色不改结构」；服务端若哪天真发颜色，需要先
   明确优先级规则。
