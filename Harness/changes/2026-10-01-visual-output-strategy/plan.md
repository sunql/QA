# 可视化输出策略（图/表/图+表 + 判断依据）实现计划

> 前置：`design.md`（同目录）。8 项决策已于 2026-10-01 由用户全部拍板（见下）。
> 每个任务 TDD：RED → GREEN → IMPROVE。

## 已拍板决策（锁定，不再讨论）

1. **多步顶层继承图取消** —— 最终汇总回答不再重复显示「最后一个成功步骤」的图（纯文字 + rationale）
2. 图+表时表格**默认折叠**（图为主、表备查）；仅表场景自然展开
3. rationale = **结构化 `{code, params}` + 前端 i18n 模板**渲染，后端不出文案
4. KPI **不附表**
5. 落库 **加两列**（alembic 0107）：`table_option` + `visual_rationale`
6. rationale **不进 PDF**
7. 前后端**同批部署**
8. 表截断沿用 `FULL_DATA_THRESHOLD=100`，且该阈值**迁入 system_config**（仿 chart_thresholds 口径）

## Global Constraints（每个任务都适用）

- 不改 `raw/`；只在用户要求时提交/推送；禁止裸 `git stash`/`git stash pop`
- 不可变模式；各层显式错误处理，绝不静默吞错
- 后端测试用真实 PostgreSQL（`qa-pg-a1` 容器，5434，库名 `qa_metadata_test`）+ 完整 API 链路，**串行**跑；不要同进程混跑 unit + services + integration
- 函数 <50 行；嵌套 ≤4 层；`chat_multistep.py`（806 行）与 `chat_stream.py`（1119 行）**已超 800 行上限，只许净删或持平，新增逻辑进新模块**
- `snake_case` 用于 ORM/Pydantic 字段（CamelModel 自动出 camelCase）
- 前端测试从 `frontend/` 目录跑 `npx vitest run`（别从仓库根跑，会误选 `.worktrees/` 副本）
  - ⚠️ **必须裸跑，绝不要按目录过滤**（如 `npx vitest run src/stores`）：前端的测试文件**绝大多数在 `src/tests/`**（另有少量在 `src/pages/__tests__`），而 `src/api`、`src/stores`、`src/components/chat` 下**一个测试文件都没有** ⇒ 过滤会「跑完、全绿、什么都没验」（实测一次过滤只收集到 2 文件/13 用例并退出 0，而真实规模是四位数）。`exclude` 已含 `e2e/**` 与 `.worktrees/**`，裸跑即正确范围。
    - 确切文件数随任务增减（Task 7 后为 160），**别把它当断言**；要自查就现数：`find src -name "*.test.ts*" | sed 's|/[^/]*$||' | sort | uniq -c`。
  - 只跑用例，**不加 `--coverage`**（`coverage.thresholds` 80% 系既有失败，根因在其他特性的 0% 覆盖，非本次引入）

## Context（已核实到 file:line）

- 决策引擎 ruleId 全集 18 个（`chart_decision.py:292-392`），`ChartDecision.ruleId` 已随 `ChartBuild` 带出（`chart_service.py:44-58`），只进日志不外露
- `ChartBuild` = {chartType, option, promptTokens, completionTokens, cachedTokens, decision, spec}；`buildChart` 出口已有 `coerceSpec` 降级路径（`chart_service.py:117-126`）
- `_chartStep`（`chat_usage.py:177`）返 5-tuple，两个调用点：`chat_service.py:635`（单步）、`chat_stream.py:615`（流式单步）；多步 `_stepChart`（`chat_multistep.py:390`）复用它
- 多步顶层继承图：`chat_multistep.py:597-598`（`last_chart_type/last_chart_option`）→ 汇总步落库 `:637-638` → `_multiStepResponse` `:669-670`；降级收尾 `:702-703, 716-717`；流式 `_reportChartEvent`（`chat_stream.py:1009`）两处调用 `:929, :986`
- `StepResultRead`（`schemas.py:1656`）/ `ChatResponse`（`schemas.py:1753`）/ `StepResult` dataclass（`multi_step_plan.py`）；SSE `_stepResultEvent`（`chat_stream.py:1027`）
- 落库：`session_message.chart_type/chart_option`（`models.py:792-793`）；截断闸 `_PERSIST_MAX_TABLE_ROWS=200`（`chat_chart_persist.py`，防御性，不治理）
- `FULL_DATA_THRESHOLD=100` 在 `data_summary.py:43`，消费点 `summarize_data:136`，调用方 `chat_service.py:1885`、`step_aggregator.py:80`（两处都是同步调用，阈值须由编排层现读后**可选参数透传**——魔数治理 hard tier 模式）
- `chart_thresholds.py:40` 的 `_readPositiveInt` 是 system_config 正整数读取的既有口径（不缓存/fail-open/非正返默认）
- alembic head：0106（`0106_wiki_link_metric_type.py` 未提交但已存在）⇒ 本特性用 **0107**
- 前端收窄 SSOT `chartContract.ts`（三消费层）；渲染门 `Boolean(chartType)`（`MessageItem.tsx`）；`ChartRenderer.tsx`（161 行）表格自取自足

## 任务分解

> **code 全集的 SSOT 在 Task 1 那一节**（含 params 列）；Task 8 那一节另有一份
> 「code × 双语文案」表。两处各自重复而非互相引用 —— 每份 brief 必须自足，
> 引用兄弟任务会让实现者去读整份计划。

### Task 1 — rationale 纯函数模块（后端）

Files: 新建 `backend/app/services/visual_rationale.py` + `backend/app/tests/unit/test_visual_rationale.py`

**code 全集（21 个）= `chart_decision` 的 18 个 ruleId + `chart_service` 的
`R_FORCED_CLIENT` + 2 个新常量。`params` 是插值变量，**不放文案**（文案在前端 i18n）。**

| code | 来源 | params | 何时产出 |
|---|---|---|---|
| R00_EMPTY_TABLE | chart_decision:292 | — | 无 plan / 无数据 / 无列 |
| R01_SINGLE_VALUE_KPI | :297 | — | 1 行 + 1 指标 |
| R01S_SINGLE_ROW_TABLE | :308 | — | 单行 + 多指标或多维 |
| R02_SHARE_DONUT | :314 | rows | 占比且行数 ≤ 阈值 |
| R03_SHARE_OVERFLOW_HBAR | :315 | rows | 占比且行数超阈值 |
| R04_TOPN_HBAR | :327/:330 | rows | TOP N + 1 维 1 指标 |
| R05_WATERFALL | :342 | — | 瀑布线索 + 可拆解 |
| R06_COMBO | :350 | — | 同比/环比 + 时间维 + ≥2 指标 |
| R07_TREND_LINE | :354 | — | 时间维 + ≥1 指标（歧义） |
| R08_RELATION_SCATTER | :358 | — | 2 指标 + 1 维 |
| R09_MULTIDIM_HEATMAP | :372 | — | 2 维 + 1 指标且矩阵够稠 |
| R10_MULTIDIM_BAR | :373 | — | 2 维 + 1 指标但矩阵太稀 |
| R11_HBAR_MANY_ROWS | :377 | rows | 1 维 1 指标且行数多 |
| R12S_QUESTION_SHARE_DONUT | :385 | — | 问句说「占比」但计划没算 formula，行数少 |
| R12S_QUESTION_SHARE_HBAR | :386 | — | 同上，行数多 |
| R12_CATEGORY_BAR | :390 | — | 1 维 + 1 指标（歧义） |
| R13_RAW_DETAIL_TABLE | :367 | — | 无聚合 + 非数值列 ≥2（明细） |
| R14_DEFAULT_TABLE | :392 | — | 其余；**未知 ruleId 的兜底** |
| R_FORCED_CLIENT | chart_service:41 | kind | 客户端/意图强制指定图型 |
| **DEGRADE_SPEC_INVALID**（新常量） | 本模块 | kind | `degradeReason` 非 None（spec 校验失败降级） |
| **SUMMARY_TEXT_ONLY**（新常量） | 本模块 | — | 多步汇总步骤（由 `summaryTextOnlyRationale()` 产出） |

```python
@dataclass(frozen=True)
class VisualRationale:
    code: str                  # 上表 21 个 code 之一
    params: dict[str, Any]     # 插值变量（rows/kind），无文案

def buildVisualRationale(
    *, ruleId: str, kind: ChartType, rowCount: int,
    degradeReason: str | None = None,
) -> VisualRationale:
    """degradeReason 非 None → DEGRADE_SPEC_INVALID；否则 code=ruleId。
    params 按需带 rows（R02/R03/R04/R11）或 kind（R_FORCED_CLIENT/DEGRADE）。"""

def summaryTextOnlyRationale() -> VisualRationale:
    return VisualRationale(code="SUMMARY_TEXT_ONLY", params={})
```

- 用例：① 18 个 ruleId 各过一遍（code 原样、params 形状正确）；② degradeReason 非 None → DEGRADE_SPEC_INVALID + params.kind；③ R_FORCED_CLIENT → params.kind；④ 未知 ruleId 不炸（落 R14_DEFAULT_TABLE 兜底 + warning）；⑤ 返回对象不可变（frozen）
- 零 LLM、零 IO，纯函数

### Task 2 — FULL_DATA_THRESHOLD 迁 system_config（后端）

Files: `data_summary.py`（改）、`chart_thresholds.py`（改：`_readPositiveInt` → 公开 `readPositiveIntConfig` + 新增 `loadFullDataThreshold`）、`chat_service.py`（`_buildAnswerPrompt` 加参数）、`chat_usage.py`（`_generateAnswer` 读值传入）、`chat_stream_output.py`（`_streamAnswerWithFallback` 读值传入）、`step_aggregator.py`（`aggregate` + `_build_prompt` 加参数）、`chat_multistep.py`（非流式多步读值传入）、`chat_stream.py`（**流式多步**读值传入）、`scripts/seed_system_config.py`（加种子条目）、`app/tests/integration/test_seed_system_config.py`（扩守卫）、`backend/app/tests/unit/test_data_summary.py`

- **`aggregate` 有 2 个调用点，都要接**：`chat_multistep.py:613`（非流式）与 `chat_stream.py:875`（流式，同样在
  lambda 体里）。只接一个 = 流式/非流式分叉，正是本特性反复踩的那类坑
- **必须同步种子**：`scripts/seed_system_config.py` 的 `CHART_CONFIG_SEEDS` 由 `_CHART_*_DEFAULT` 派生，
  并有 `test_seed_system_config.py::test_every_governed_default_is_seeded` 守卫「源码默认值 ↔ 种子条目」不漂移。
  **但那条守卫断言的是硬编码的 4 个 CHART key**，所以新增的 key 缺席**不会打红** —— 必须手工补：
  种子里加 `FULL_DATA_THRESHOLD` 条目（值取 `data_summary.FULL_DATA_THRESHOLD`，SSOT 不抄字面量），
  并把守卫测试的期望字典扩成含它，否则下一轮同样的静默漂移会重演

- `chart_thresholds._readPositiveInt` 改名公开 `readPositiveIntConfig`（**自有 3 处内部调用**同步：`chart_thresholds.py:68`、`:73`、`:80`；定义在 `:40`），它是该读取口径的 SSOT，不复制第四份
- `data_summary.py`：**常量名保持不变** —— `FULL_DATA_THRESHOLD = 100` 已被两个测试文件在 6 处 import
  （`test_data_summary.py:261,271`、`test_chat_service.py:1621,1658`），改名等于白白制造 6 处 ImportError。
  `chart_thresholds.loadFullDataThreshold` 直接 import 它作为默认值（SSOT，别抄一遍字面量 100）。
  新增关键字参数 `summarize_data(data, *, fullDataThreshold: int | None = None)`，None 用该默认。
  **该模块保持零 IO 纯函数** —— 不要在这里放 async DB 读取
- **async 读取函数放 `chart_thresholds.py`**（它已是「从 system_config 读可视化阈值」的 SSOT 模块）：`async def loadFullDataThreshold(session) -> int`，key=`FULL_DATA_THRESHOLD`，复用 `readPositiveIntConfig`
- **接线形态（签名已逐个核实）**：三处 `summarize_data` 都在**纯函数/静态 prompt builder** 里，
  共同改法是「静态方法只多收一个关键字参数 `full_data_threshold: int | None = None`，
  **读值一律发生在持有 session 的 async 调用方**」：
  - `chat_service._buildAnswerPrompt(question, sql, data, history="")` —— **`@staticmethod`，无 session**。
    它内部 `summarize_data(data)` 处加上述关键字参数。两个生产调用方都在持 session 的 async 方法内，
    各自 `await loadFullDataThreshold(session)` 后传入：`chat_usage._generateAnswer`（`chat_usage.py:245`）、
    `chat_stream_output._streamAnswerWithFallback`（`chat_stream_output.py:95`）
  - `step_aggregator._build_prompt` —— 也是 **`@staticmethod`**，内部 `summarize_data(r.data)` 同改。
    它的调用方 `StepAggregator.aggregate()` **同样没有 session** ⇒ `aggregate` 再加一个同名关键字参数
    往下透传；**读值在 `chat_multistep.py:610`**（那里有 session）
  - ⚠️ **`aggregate` 是在 lambda 体里被调用的**（`chat_multistep.py:610` 的
    `lambda cfg: self._stepAggregator.aggregate(...)`）—— `await` 不能写进 lambda，
    必须在 lambda **外面**先算好再捕获
- 用例：① 缺 key → 100；② 合法值生效（summarize_data 按传入阈值切全量/摘要）；③ 非正/非法/DB 异常 → 默认 + warning；④ 不传参行为与旧版一致（回归）

### Task 3 — ChartBuild 装配 tableOption + rationale（后端）

Files: `chart_service.py`（改）、新建 `backend/app/services/visual_payload.py` + 两个单测文件

- 新纯函数 `assembleTableOption(*, specKind, columns, data, fullDataThreshold) -> dict | None`：
  图形类（非 TABLE/KPI）→ `{"columns": columns, "rows": data[:threshold], "truncated": len(data) > threshold}`；TABLE/KPI → None（TABLE 的表在 chartOption 里、KPI 决策 4 不附表）。
  ⚠️ `specKind` 必须传 **`coerceSpec` 之后**的最终 `spec.kind`（它同时就是发出去的 `chartType`）——
  降级成 TABLE 时 `tableOption` 必须为 None，否则前端同一份数据会拿到两份表
- `ChartBuild` 加 `tableOption: dict | None` + `rationale: VisualRationale`（frozen dataclass 加字段）。
  **该类型全仓只有 2 个构造点，都在 `chart_service.py`**：`:127`（正常出口）与 `:152`（`_emptyResult`）；测试不构造它
- `buildChart` 内：`from app.services.chart_thresholds import loadFullDataThreshold` 后 `fullDataThreshold = await loadFullDataThreshold(session)`。
  ⚠️ **`buildVisualRationale` 的 `kind` 要传「决策引擎选出的 kind（降级前的意图）」，不是降级后的** ——
  否则 `DEGRADE_SPEC_INVALID` 的文案会渲染成「数据结构不满足 **table** 的绘图要求」，是语病；
  同理 `R_FORCED_CLIENT` 传的是被强制的那个 kind。`rowCount` 传 `len(data)`。
  degrade 路径把 `degradeReason` 一并传入；`_emptyResult` 带 `ruleId="R00_EMPTY_TABLE"` 的 rationale
  （它是 `@staticmethod`，拿不到 session，也不需要 —— 空数据零成本）
- 用例（组合矩阵，双向断言）：① 图形类 → chartType=kind + option 是 ECharts + tableOption 非空 + rationale.code=ruleId；② TABLE → tableOption 为 None；③ KPI → tableOption 为 None；④ 超阈值 → truncated=True 且 rows 数 == 阈值；⑤ spec 降级 → rationale.code=DEGRADE_SPEC_INVALID 且 chartType=TABLE；⑥ 空数据 → R00

### Task 4 — 契约贯穿：schemas + StepResult + SSE + 单步响应（后端）

Files: `schemas.py`（StepResultRead:1656、ChatResponse:1753）、`multi_step_plan.py`（StepResult）、`chat_usage.py:177`（_chartStep）、`chat_service.py:635,704`、`chat_stream.py:615,1027`、`chat_multistep.py:359,390`、集成测试 `test_chat_stream_api.py` / `test_chat_api.py`

- `StepResultRead` / `ChatResponse` 加 `table_option: dict | None = None`、`visual_rationale: dict | None = None`（CamelModel 出 `tableOption`/`visualRationale`；visual_rationale 形状 `{"code": str, "params": dict}`）
- ⚠️ **`VisualRationale`（Task 1 产出）没有 `to_dict()`** —— 线上形状由本任务定义。
  **`params` 里装的是 `ChartType` 枚举成员**（Task 3 已用 `params["kind"] is ChartType.HEATMAP` 钉住），
  `ChartType` 是 `(str, Enum)` —— 它的**真值 `"heatmap"` 与 `str()` 出来的 `"ChartType.HEATMAP"` 不是一个东西**。
  所以**不要依赖 pydantic 对裸 dict 里枚举成员的隐式处理**：在出口显式归一成原始值
  （`{k: (v.value if isinstance(v, Enum) else v) for k, v in r.params.items()}` 这类），
  线上形状就是 `{"code": str, "params": {k: 原始值}}`，`params.kind` 必须是 `"heatmap"` 这种值。
  **不是中文名** —— 中文名由前端用既有的 `chatPanel.chartTypes.<kind>` 解析（Task 8）。
  这是决策 3「结构化 code + 前端 i18n」的必然结果：后端只出语义值，出文案的活全在前端
- `StepResult` dataclass 加同名字段（默认 None）
- `_chartStep` 返 7-tuple（+tableOption, rationale）；两调用点 + `_stepChart` 透传写进 StepResult
- `_stepResultEvent` 加两字段；单步 `chart` 事件负载加 `tableOption`/`visualRationale`；单步 ChatResponse 组装带两字段
- **已核实事实（派单前逐个读源码，避免实现者返工）**：
  - `ChatResponse` 全仓有 **约 45 个构造点**（`chat_domain.py` 占绝大多数）⇒ 新增两字段**必须带默认值 `None`**，否则 45 处一起红。真正要填值的只有单步查询这一条路径
  - **单步路径也构造 `StepResult`**：`chat_service.py:700` 的 `ChatResponse(steps=[_step_result_to_read(StepResult(...))])`（2026-08-16 起单步也铺 steps，前端 `MultiStepPlanCard` 常驻渲染）⇒ 单步的图/表/依据要**同时**落顶层 `chartType/chartOption/tableOption/visualRationale` **和** `steps[0]` 那一份，漏一处就是「计划卡里没依据」
  - 单步 SSE 的 chart 事件在 **`chat_stream.py:626`**（`yield StreamEvent(EVENT_CHART, {"chartType": ..., "chartOption": option, "data": data})`），不是 `_reportChartEvent`（那是多步的，Task 5 删）
  - `_chartStep` 在 `chat_usage.py:177`，其返回值被 `chat_service.py:635`、`chat_stream.py:615` 两处解包，`chat_multistep._stepChart` 是第三处
- 用例：① 流式 step_result 帧含 tableOption/visualRationale，且 **`visualRationale.params.kind` 逐字等于 `"bar"` 这类枚举值**
  （**不是 `"ChartType.BAR"`** —— `ChartType` 是 `(str, Enum)`，pydantic 对嵌套在裸 `dict` 里的枚举成员怎么序列化必须**实测钉死**，
  错了不报错、只会让前端渲染出「不满足 ChartType.BAR 的绘图要求」）；② 非流式 ChatResponse 同口径
  （防上次「收窄只做一条路径」分叉）；③ 单步 `steps[0]` 与顶层两字段一致；④ 失败步骤两字段为 None

### Task 5 — 多步去顶层继承图 + 汇总步 rationale（后端，决策 1）

Files: `chat_multistep.py`（597-598, 637-638, 662-677, 696-722, 724-738 删改）、`chat_stream.py`（929, 986, 1009-1024 删；done 事件加字段）、集成测试

- 删 `last_chart_type/last_chart_option` 变量及其全部传递；**锚点 `last_plan/last_sql/last_data` 保留不动**（追问链路依赖）
  - ⚠️ **两个文件都有**（已逐点核实；Task 4 会改动 `chat_stream.py` ⇒ **按符号定位，别按行号**）。全量站点：
    - `chat_multistep.py`：声明 ×2（`~:598-599`、`~:740-741` 形参）、落库实参（`~:641-642` 逐步、`~:775-776` 降级收尾）、响应实参 ×2（`~:673-674`、`~:720-721`）、赋值（`~:697-698`）、降级实参（`~:706-707`）
    - `chat_stream.py`：声明 ×2（`~:835-836`）、落库实参（`~:912-913`）、`_reportChartEvent` 实参 ×2（`~:935` 正常、`~:992` 降级）、赋值（`~:976-977`）、降级实参（`~:986-987`）
  - 漏一处的症状**只出现在一条路径上**（「顶层还挂着图」），用例④（SSE 不再出现多步 chart 事件）正是抓它的反向守卫 —— 两个终点（正常 `:944` / 降级 `:1001`）都要断言，别只测正常终点
  - `_finalizeMultiStepDegrade` 的**两个形参**（`chat_multistep.py:728` 起的 def）连同其内部 `_storeSessionMessages` 调用一并删
  - `_multiStepResponse`（定义 `chat_helpers.py:113`，两个调用点 `chat_multistep.py:667`、`:714`）是**删掉 chart 两实参 + 加 `visualRationale` 实参**的唯一处
- 汇总步落库（`_storeSessionMessages` 调用点）：把原先传的 `chart_type=last_chart_type, chart_option=last_chart_option` 改成 `chart_type=None, chart_option=None`（纯删除）。**本任务不碰新的落库列参数** —— `visual_rationale` 的落库由 Task 6 在**同一行**一并接线；同一行被两个任务各改一半会让合并顺序变成隐式依赖
- `_multiStepResponse` 加 `visualRationale` 参数。**它的定义在 `chat_helpers.py:113`**，是多步响应**唯一**的构造点
  （正常汇总与降级收尾共用，当初抽出来就是为了防这两条路径漂移）⇒ 只需改这一处签名，
  两个调用点都传 `summaryTextOnlyRationale()` + `chartType=None, chartOption=None`
- 删 `_reportChartEvent` 函数（`chat_stream.py:1009-1024`）及其**两处**调用：`:934`（正常终点）、`:991`（降级终点）
- 多步 done 事件加 `visualRationale`：**只有 `:944`（正常）与 `:1001`（降级）两个终点**。
  ⚠️ `_streamMultiStep` 里还有第三个 `EVENT_DONE` 在 **`:800`** —— 那是「超步数上限拒绝」的终点，
  既不带图也不带 rationale，**不要动它**
- 同步 `_finalizeMultiStepDegrade`（`chat_multistep.py:724` 起）的 `last_chart_type`/`last_chart_option`
  两个形参，以及它内部的 `_storeSessionMessages` 调用
- 用例：① 多步顶层 chartType 为 None 且 visualRationale.code=SUMMARY_TEXT_ONLY；② 每个数据步仍各带自己的 chartType+tableOption+rationale（回归）；③ 降级收尾同样无顶层图；④ SSE 不再出现多步的 chart 事件；⑤ 超步数上限拒绝路径（`:800`）不受影响（回归）

### Task 6 — 落库 alembic 0107 + 回放读取（后端，决策 5）

Files: 新建 `backend/alembic/versions/0107_session_message_visual_payload.py`、`app/domain/models.py`（chart 两列在 `:788-793`）、`chat_context.py:420`（_storeSessionMessages）、`chat_chart_persist.py`、回放 read schema = **`app/domain/schemas.py:1962` 的 `ChatMessageRead`**（容器是 `:1989` `SessionMessagesResponse`）、集成测试

- 0107：`session_message ADD COLUMN table_option JSONB NULL, visual_rationale JSONB NULL`（prod 5433 与 test 5434 同一 migration；部署时 prod 先备份）
  - ⚠️ **`down_revision = "0106"`（不是 0105）** —— 本分支开工前 `0106_wiki_link_metric_type` 已合入，0106→0107 才是链尾（核实方式：遍历 versions 目录算 `revision` 减去被引用为 `down_revision` 的集合，唯一头 = 0106）
- ORM 加两列；`_storeSessionMessages` 加两参数并写库；`chat_chart_persist` 加 `boundedTableOption`（复用 `_PERSIST_MAX_TABLE_ROWS=200` 闸，返回新 dict）
  - ⚠️ **两参数必须带默认值 `None`**：`_storeSessionMessages` 全仓 **28 个调用点**（`chat_domain.py` ×7、`chat_service.py` ×7、`chat_stream.py` ×11、`chat_multistep.py` ×3、`chat_l4.py` ×1、集成测试 ×1；其 docstring 自述「既有调用点（27 处）」），只有下面列出的少数几个真的要写值 —— 0105 的 `chart_type/chart_option` 就是这个先例。另：`test_chart_stream_api.py:172` 注释写「四个 `_storeSessionMessages` 调用点」，该数字已陈旧，别照抄
  - **要写值的站点（已逐个核实，按符号定位别按行号）**：
    | 场景 | 站点 | 写什么 |
    |---|---|---|
    | 单步（非流式） | `chat_service.py:669` | `table_option=build.tableOption`、`visual_rationale=build.rationale.to_dict()` |
    | 单步（流式） | `chat_stream.py:694` | 同上 |
    | 多步汇总步 | `chat_multistep.py:641`、`chat_stream.py:917` | `table_option=None`、`visual_rationale=summaryTextOnlyRationale().to_dict()` |
    | 多步降级收尾 | `chat_multistep.py:762` | 同汇总步 |
    | 其余 ~23 个站点 | — | 不传，走默认 `None` |
    ⚠️ **多步「每个数据步」不是落库站点**（我原先在此处写错过，由实现者指出并经我复核）：`chat_multistep.py:384` / `chat_stream.py:550` 是 **`StepResult(...)` 构造**，不是 `_storeSessionMessages` 调用。多步一轮只落**一行** assistant 记录（汇总或降级），各数据步的图/表/依据是通过 `StepResult` → `step_result`/`steps[]` 走**响应**，不进 `session_message`。故那两处**无需也不该**接线（它们已由 Task 4 填了 `table_option`/`visual_rationale` 字段）。
    后三行的 `chart_type=None` 是 Task 5 刚改的；本任务在**同一调用**上补 `visual_rationale`/`table_option`（Task 5 刻意留给本任务，避免同一行被两个任务各改一半）
  - 别把 **读侧**当成写侧：`chat_helpers.py:177`、`session_history_service.py:254/517/605` 都是 `chart_type=` 出现在**读映射**里，不在本任务范围
  - `boundedTableOption` 与 `boundedChartOption` 的两道闸**平时不叠加**：Task 3 的 `assembleTableOption` 已按 `FULL_DATA_THRESHOLD`（默认 100）截过一轮并置 `truncated`，100 ≤ 200 ⇒ 落库闸只在运维把 `FULL_DATA_THRESHOLD` 调到 >200 时才生效（纵深兜底，不是死代码 —— 但别写「必然截到 200」的断言，用例②要在 helper 上直接喂 >200 行）
- 历史回放的消息 read schema 加 `tableOption`/`visualRationale` 透传
  - `ChatMessageRead.chart_type` 是 **`str | None`**（`:1981`），不是 `ChartResponse` 那种 `ChartType` 枚举（`:1768`）—— 回放路径存的就是列里的原始字符串。新加的两字段照此用 **`dict | None = None`** 裸 dict 透传，别引入新枚举包装
  - 回放读的是 `session_message` 两列 → ORM 属性名 `table_option`/`visual_rationale`（CamelModel 出 `tableOption`/`visualRationale`）
- **迁移写法照抄 `0105_session_message_chart.py`**（同型先例：也是给 `session_message` 加呈现负载列）：模块级 `revision: str = "0107"` / `down_revision: str | None = "0106"`；`op.add_column` 用 `postgresql.JSONB(astext_type=sa.Text())`；`downgrade()` **逆序** DROP。docstring 按 0105 的体例写清「触发 / 变更 / 为什么单独两列而非塞进 `chart_option` envelope（决策 5）/ 幂等性 / 两库同步」
- ⚠️ **绝不要手工跑 `alembic upgrade head`！** 已核实链路：`alembic/env.py` 用 `getSettings().databaseUrl`（**不是** 读 alembic.ini，那里 `sqlalchemy.url` 是空的），而 `app/config.py:31-34` 的默认值是
  `postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5432/qa_metadata` —— **5432 的 `qa_metadata` 就是生产库**。
  所以「不带 `DATABASE_URL` 手跑 alembic」= 把 0107 直接打到生产。
  测试侧**不需要你动手**：`_pg_support._ensureSchema`（`:52-70`）会先 `env["DATABASE_URL"] = <测试库 URL>` 再在子进程跑 `alembic upgrade head`（幂等，进程内只跑一次）。
  ⇒ **验证迁移只需跑集成测试**，不要执行 alembic CLI。
- 用例：① 写入→读回两字段一致；② 超长表截断到 200 且 truncated=True；③ 存量行（NULL）回放不炸

### Task 10 — 前置修复：0105 遗留的 JSONB 序列化崩溃（**必须在 Task 9 部署前完成**）

> 编号说明：用 10 而不是 6b/6F 是因为 `task-brief` 用 `Task[ \t]+N([^0-9]|$)` 抽任务，
> 「Task 6b」会被当成 Task 6 的标题**覆盖掉 Task 6 的 brief**。位置放在 Task 6 之后
> 是因为它同属 `chat_chart_persist` 的职责；**执行顺序是 Task 6 → Task 10 → Task 7**。

Files: `backend/app/services/chat_chart_persist.py`（`boundedChartOption` 归一化）、`backend/app/tests/unit/test_chat_chart_persist.py`

**缺陷（Task 6 实现者发现，控制器端到端复核确认；非本特性引入，是 0105 的遗留）**：

`session_message.chart_option` 是 `JSON().with_variant(JSONB)`，无自定义 `json_serializer`
（`database.py:54` 裸 `create_async_engine`）⇒ SQLAlchemy 用 `json.dumps` 序列化 ⇒ **遇到 `Decimal` 抛 `TypeError`**。
而 TABLE 类负载存的是**原始行**：`chart_renderer._buildTable` 返回 `{"columns": …, "rows": list(data)}`
（不转字符串），`boundedChartOption` 只截行不做类型归一，`data` 来自
`business_db_pool.py:541` 的 `[dict(r) for r in result.mappings()]` —— NUMERIC 列在此就是 `Decimal`
（已实测：`SELECT 1.5::numeric` → `Decimal('1.5')`，`json.dumps` 确报 `TypeError`）。

⇒ **任何 TABLE 类且含数值列的负载**（R13/R14 明细清单、R01S 单行多指标、R00）在落库时抛异常；
`_storeSessionMessages` 内无 try/except，异常直接冒泡 ⇒ 该轮**整轮不落库**（不止图丢失）。
0105 上线以来一直没有测试覆盖（既有 `test_chat_chart_persist.py` 全用 `int`，而 int 是可序列化的）。

**修法**：
- `boundedChartOption` 出口接 `_jsonSafe`（Task 6 已为 `boundedTableOption` 写好的同一个 helper，`Decimal→float` 与渲染器 `toNumber` 同口径）
- ⚠️ **但不能无条件套用 —— 有对象同一性契约**。已核实：`test_chat_chart_persist.py:78,83` 断言
  `boundedChartOption(option) is option`（**是 `is`，不是 `==`**）。Task 6 实现者当初说「修它会破坏 identity 契约测试」**是对的**（我一开始以为这是托辞，核实后确认属实）。而 `_jsonSafe` 现在**无条件重建容器**（dict/list 一律返回新对象）⇒ 直接套上必让这两条红。
- **推荐做法**：把 `_jsonSafe` 改成**「无变化则返回原对象」**（先递归探测，确实存在非安全值才重建；否则原样返回入参）。
  这样两个调用点都获得同一性保持，无条件包一层也安全，且 `boundedTableOption` 顺带受益。
  比「加一个 `_needsNormalization` 谓词」更好：谓词 + 归一 = 两趟遍历且两份规则，容易漂移。
- 归一后**已是安全类型的值必须原样保留**（str/int/float/bool/None 直接返回），否则既有落库内容与既有测试都会变
- **必须同步修 docstring**（Task 6 评审 Finding 3，Minor，同函数）：`boundedTableOption` 的 docstring 现写着
  `boundedChartOption`「吃的是渲染器已归一成 float 的 chartOption」（`:62-63`）—— 这只对 ECharts 类成立
  （`chart_renderer` 对 bar/line/pie 走了 `toNumber`），**对 TABLE 类为假**，而那正是本崩溃的藏身处。
  修完行为后这句注释就成了**错误的化石**，会误导下一个读者以为 `chart_option` 永远 JSON-safe。改为一句话点明两路差异。

- 用例：① 含 `Decimal` 的 TABLE 负载经 `boundedChartOption` 后被归一为 `float` 且可 `json.dumps`（**必须用真实 `Decimal`，不能用 int** —— 这正是它长期隐身的原因）；② **安全负载 `is` 同一**（钉住 `:78/:83` 的既有契约，防回归）；③ `datetime/date` 同样归一；④ 未知类型落 `str(value)` 不丢值（保留 Task 6 的既定语义）；⑤ 嵌套结构里的 `Decimal`（`{"rows": [{"deep": {"v": Decimal}}]}`）也要归一 —— 只测顶层会漏掉递归分支

**控制器已跑过等价实现验证（两种写法的判定结果，非推断）**：

| 断言 | 现 `_jsonSafe` | 「无变化返回原对象」 |
|---|---|---|
| `:78` `boundedChartOption(option) is option` | **False ⇒ 会红** | True |
| `:83` 同上 | **False ⇒ 会红** | True |
| `:127` `boundedTableOption(option) == option` | 靠 `==` 侥幸过 | True |
| `:130` Decimal → float | 新对象（正确） | 新对象（正确） |

⇒ 结论：改动**必须**带「无变化返回原对象」语义；且 `:127` 今天是**侥幸通过** —— Task 6 的
`boundedTableOption`（`_jsonSafe(_boundedRows(...))`，`:74`）**已经**不保同一性，只是没人用 `is` 断言它。
**顺手把 `:127` 的 `==` 收紧为 `is`**（同一性在该函数上同样成立，已验证）—— 否则这两个 helper
的契约会长期不一致，而「安全的那个」随时可能被下一次改动打破且无人察觉。

### Task 7 — 前端契约收窄（**全路径同口径**）

Files: `types/chat.ts`、`utils/chartContract.ts`、`api/chat.ts`、`stores/chatStore.ts`、对应单测

- 两个新类型（放 `types/chat.ts`）：`interface TablePayload { columns: string[]; rows: Record<string, unknown>[]; truncated?: boolean }`、`interface VisualRationale { code: string; params: Record<string, unknown> }`
- **类型面已逐个核实（接口清单，别只改「看起来相关」的那一个）** —— 注意 `StepResultView`/`StreamChartData`/`StreamSummary` **定义在 `api/chat.ts` 而不是 `types/chat.ts`**（我原先的说法不准）：

  | 文件 | 接口 | 加什么 |
  |---|---|---|
  | `types/chat.ts:106` | `StepResultRead` | 两字段 |
  | `types/chat.ts:125` | `MultiStepStep` | 两字段 |
  | `types/chat.ts:150` | `ChatResponse` | 两字段 |
  | `types/chat.ts:233` | `ChatMessage`（**store/UI 行**） | 两字段 |
  | `types/chatHistory.ts:14` | `ChatMessageRead`（**回放线类型**） | 两字段，但**必须声明为 `unknown`**（系统边界原始 JSON，照它既有的 `chartType?: unknown` 先例），不是 `TablePayload`/`VisualRationale` |
  | `api/chat.ts:43` | `StepResultView` | 两字段 |
  | `api/chat.ts:170` | `StreamChartData`（`chart` 事件） | 两字段 |
  | `api/chat.ts:177` | `StreamSummary`（`done` 事件） | **只加 `visualRationale`** —— done 帧不带 `tableOption`（多步顶层无表；单步表走 chart 事件） |
  | `types/chat.ts:40` | `ExtractedEntities` | **不要动** —— 它是**问句意图抽取**侧（从问句里抽出的 chartType），不是响应负载载体 |
  | `types/chat.ts:94` | `ChatRequest` | **不要动** —— 客户端**指定**图型，同上 |

  后两个虽然也有 `chartType`，但语义是「输入意图」而非「输出负载」，加了字段没人读、只会让下一个人以为该填。
  漏改某个响应侧接口的后果是**静默丢字段**（对象字面量经 patch/spread 组装时 TS 不做 excess-property 检查），不是编译错。

  ⚠️ **`ChatMessageRead` 极易漏，且漏了会在最晚的环节才炸**（控制器读源码抓出，原表未列）：
  - **它是回放链路的真实读取类型**：`chatStore.ts:136` 的 `function toChatMessage(read: ChatMessageRead): ChatMessage` ⇒ 历史回放那一处（`:149-150`）读的就是 `read.tableOption`；`utils/collectExportCharts.ts:18` 也 import 它。
  - **会炸但不早炸**：漏了它，`:149` 的 `read.tableOption` 是**编译错**（属性不存在）—— 可 `npm test` 是 `vitest run`，**esbuild 只剥类型不做类型检查**，**测试会全绿**；直到 `npm run build`（= `tsc -b && vite build`）才暴露，也就是 **Task 9 部署时的 `docker compose build --no-cache frontend`**。
  - ⇒ **Task 7 必须跑一次 `npm run build`**（= `tsc -b && vite build`，即 Docker 构建实际执行的那条），不能只跑 vitest 就报绿。这与本仓「前端必须 compose build 才算数」是同一条教训。
    （别用 `tsc -b --noEmit`：`tsconfig.json` 虽 `noEmit: true`，但它 `references` 的 `tsconfig.node.json` 是 `composite: true`，build 模式下语义不同；直接用仓库既有的 `build` 脚本最忠实、无歧义。）
  - **两个类型的 `unknown` 分工别搞反**：`ChatMessageRead.tableOption/visualRationale` 用 **`unknown`**（边界原始 JSON，落库的可能来自更早的后端）；`ChatMessage.tableOption/visualRationale` 用**收窄后的** `TablePayload | null` / `VisualRationale | null`。收窄动作发生在 `chatStore` 的映射里，与既有 `chartType: unknown → ChartType | null` 完全同构。
- `chartContract.ts`：`asTablePayload`（columns/rows 都是数组才放行）、`asVisualRationale`（code 非空 string + params 普通对象；**不内置 code 白名单** —— 后端可先发新 code，前端 i18n 缺 key 时显示 code 原文兜底，不静默吞。这与 `VALID_CHART_TYPES` 的严格白名单**刻意不同**：图型白名单漏同步的症状是「图静默消失」，而 rationale 漏一个 code 只是这行说明退化成英文码，代价不对称）
- **接入点已核实为 9 处**（不是「四条路径」—— 上次「收窄只做一条路径」已分叉过一次，这里的漏点只会表现为「某些路径没有依据/没有表」这种不报错的缺口）。`chartContract` 的导入者只有 3 个模块：
  - `api/chat.ts`（4 处）：`normalizeStepResult`(:86)、`normalizeChatResponse`(:97)、SSE `chart` 事件 handler(:321)、**SSE `done` 事件 handler(:335)**
  - `stores/chatStore.ts`（5 处）：历史回放(:149-150)、SSE `chart`→消息(:366-367)、SSE `step_result`→步骤(:402-403)、非流式响应→消息(:485-486)、非流式 `steps[]`→步骤(:527-528)
  - ⚠️ **但别把这 5 处误当成「已经在走 `chartContract`」**（控制器已逐行读过）：`chatStore.ts` 里**只有历史回放一处**（`:149-150`）调了 `normalizeChartType`/`asChartOption`；另外 4 处是**直接**从已定型对象上取值 —— `chart.chartType`(:366) / `result.chartType ?? null`(:402) / `res.chartType ?? null`(:485) / `s.chartType ?? null`(:527)。这句「`chartContract` 的导入者只有 3 个模块」说的是**模块级 import**，不是「9 处都已在收窄」。
  - **本任务的范围裁定**：9 处都要加**两个新字段**的收窄（`asTablePayload`/`asVisualRationale`，对新字段而言 9 处全是新增）。**不要**顺手给那 4 处补 `normalizeChartType`/`asChartOption` —— 那是既有的**另一处**路径不一致，改了会扩大爆炸半径：白名单不认识的新图型会从「能渲染」变成「静默消失」，而图型白名单漏同步正是本仓记过的危险失败模式。改为**记入 ledger 的 deferred Minor**，本任务不碰。用例④因此只断言**两个新收窄函数**在 9 处结果一致。
  - `utils/collectExportCharts.ts`（:48-55）：**确认无需改动**，但要在报告里写明确认过 —— 它只按 `chartType` 决定是否截屏、按 `chartOption` 渲染图；设计决策 6 已定「rationale 不进 PDF」，`tableOption` 也不进（PDF 的原生表格走既有 `chartOption` 路径）
- ⚠️ **`done` 事件必须收窄 `visualRationale`（但不需要 `tableOption`）** —— 这条我先前判反过，已按源码更正：
  **多步汇总步不发 `step_result`**（`chat_stream.py:986` 的 `_stepResultEvent(run.result)` 在数据步循环内；汇总步走 `EVENT_TOKEN` 流文字 + 终结帧 `EVENT_DONE`，正常终点携带 `steps` 后 `return`，降级终点在其后）。
  ⇒ 汇总步的 `SUMMARY_TEXT_ONLY` 依据**只能**经 `done` 帧到前端；漏了它就**正是用户明确要求的那条「最后的总结也要给判断逻辑」没实现** —— 不报错、只是那行说明永远不出现。
  单步流式的图/表/依据仍走 `chart` 事件（`chat_stream.py:627`）。
- 用例：① 非法 tableOption（rows 非数组）→ null；② rationale 缺 code → null；③ 未知 code 保留不吞；④ **9 处接入点各喂同一份负载，断言收窄结果一致**（用一个共享 fixture 循环，别写 9 份复制断言 —— 复制断言会在加第 10 处时漏掉）；⑤ **多步汇总消息最终渲染出 SUMMARY_TEXT_ONLY 文案**（端到端，防「`done` 收窄了但没 patch 进消息」）

### Task 8 — 前端渲染 + i18n（决策 2/3/4）

Files: `ChartRenderer.tsx`、`MessageItem.tsx`、`MultiStepPlanCard.tsx`、`i18n/zh-CN.ts`、`i18n/en-US.ts`、单测

- `ChartRenderer`：图形 kind → 图 + 下方 antd Collapse「数据表」（`defaultActiveKey=[]`，**默认折叠**；表用既有自取自足 Table 渲染 tablePayload）+ rationale 说明行（次要色）
- **rationale 插值要先把 `params.kind` 本地化**：`DEGRADE_SPEC_INVALID` / `R_FORCED_CLIENT` 两条模板含 `{kind}`，
  而后端传的是枚举值（`"bar"`），直接插会渲染成「数据结构不满足 **bar** 的绘图要求」。
  取值链是：`i18n.t(chatPanel.chartTypes.${params.kind})` 拿到本地化图型名，再当作 `kind` 传给
  `i18n.t(\`chat.visual.${code}\`, {...params, kind: 本地化名})`。`chatPanel.chartTypes` 已覆盖全部 11 种，
  **本任务不要新增图型名 i18n**，复用即可
- TABLE/KPI 形态不变；rationale 行所有形态都渲染（含「为什么不生成图表」）
- `MessageItem`：消息级 `visualRationale`（多步汇总）渲染为答案下方次要色一行
- i18n：`i18n/zh-CN.ts` 与 `i18n/en-US.ts` 各加一套 `chat.visual.<code>` 模板，
  **21 个 code 一个不少**（缺 key 时前端回退显示 code 原文，不炸但难看）：

| code | zh-CN | en-US |
|---|---|---|
| R00_EMPTY_TABLE | 查询无结果，不生成图表 | No rows returned; no chart generated |
| R01_SINGLE_VALUE_KPI | 单一聚合值，以指标卡呈现 | Single aggregate value shown as a KPI card |
| R01S_SINGLE_ROW_TABLE | 仅单行结果，以表格呈现（单点数据不构成图形） | Single-row result shown as a table (one point is not a chart) |
| R02_SHARE_DONUT | 占比数据（{rows} 项），以环形图呈现，附数据表 | Share breakdown ({rows} items) as a donut chart, with data table |
| R03_SHARE_OVERFLOW_HBAR | 占比项数较多（{rows} 项），以横向柱状图呈现，附数据表 | Share breakdown with many items ({rows}) as a horizontal bar chart, with data table |
| R04_TOPN_HBAR | TOP {rows} 排名，以横向柱状图呈现，附数据表 | Top {rows} ranking as a horizontal bar chart, with data table |
| R05_WATERFALL | 构成拆解，以瀑布图呈现，附数据表 | Composition breakdown as a waterfall chart, with data table |
| R06_COMBO | 同比/环比对比，以柱线组合图呈现，附数据表 | Period-over-period comparison as a combo chart, with data table |
| R07_TREND_LINE | 含时间维度，以折线图呈现趋势，附数据表 | Time dimension detected; trend shown as a line chart, with data table |
| R08_RELATION_SCATTER | 双指标关系，以散点图呈现，附数据表 | Two measures related; shown as a scatter chart, with data table |
| R09_MULTIDIM_HEATMAP | 双维度交叉，以热力图呈现，附数据表 | Two-dimension cross-tab shown as a heatmap, with data table |
| R10_MULTIDIM_BAR | 双维度对比，以分组柱状图呈现，附数据表 | Two-dimension comparison as a grouped bar chart, with data table |
| R11_HBAR_MANY_ROWS | 类目较多（{rows} 项），以横向柱状图呈现，附数据表 | Many categories ({rows}) shown as a horizontal bar chart, with data table |
| R12S_QUESTION_SHARE_DONUT | 问句指向占比，以环形图呈现，附数据表 | Question asks for a share; shown as a donut chart, with data table |
| R12S_QUESTION_SHARE_HBAR | 问句指向占比且项数较多，以横向柱状图呈现，附数据表 | Question asks for a share with many items; shown as a horizontal bar chart, with data table |
| R12_CATEGORY_BAR | 单维度对比，以柱状图呈现，附数据表 | Single-dimension comparison as a bar chart, with data table |
| R13_RAW_DETAIL_TABLE | 明细清单（无聚合），以表格呈现，不生成图表 | Raw detail list (no aggregation); shown as a table, no chart |
| R14_DEFAULT_TABLE | 数据形态不适合图形，以表格呈现 | Data shape is not chart-friendly; shown as a table |
| R_FORCED_CLIENT | 按指定图型（{kind}）呈现，附数据表 | Rendered as the requested chart type ({kind}), with data table |
| DEGRADE_SPEC_INVALID | 数据结构不满足{kind}的绘图要求，降级为表格 | Data does not meet the requirements for {kind}; downgraded to a table |
| SUMMARY_TEXT_ONLY | 汇总为文字结论，各步骤图表见上方 | Summary given as text; per-step charts are shown above |

  **插值用 `{ }` 单花括号**（**不是** i18next 默认的 `{{ }}`）——`rows`/`kind`
  两个变量名必须与后端 `params` 的 key 逐字一致。
  - ⚠️ **本节原文写的是「双花括号 `{{ }}`」，那是错的，已于 Task 8 更正**：本仓 `src/i18n/i18n.ts:18-21`
    显式把 i18next 的 `interpolation.prefix/suffix` 覆写成 `"{"`/`"}"`（注释：「保持与原有自定义 hook 一致的占位符语法：{name}」）。
    在**本仓配置下实跑**验证（i18next 实例，同一 `interpolation` 配置）：
    ```
    single-brace  {rows}   => "Share (7 items)"
    double-brace  {{rows}} => "Share ({{rows}} items)"   ← 不插值，原样输出
    ```
    ⇒ 照原文写会**上线可见的 `{{rows}}` 字面量**（「占比数据（{{rows}} 项）」）。
    想用双花括号得改 `i18n.ts` 的 prefix/suffix，那会打断**既有几百个 `{name}` key** ⇒ 不改。
  - 缺 key 时 i18next 返回的是**带命名空间的完整 key**（`"chat.visual.R99_X"`），**不是**裸 code
    ⇒ 「回退显示 code 原文」必须由渲染层显式兜（`utils/visualRationale.ts` 已这么做），不能指望 i18next。
- 用例：① 图形类渲染图 + 折叠表 + rationale 行；② TABLE 形态不重复出表；③ 汇总消息显示 SUMMARY_TEXT_ONLY 文案；④ 切英文文案正确；⑤ i18n 缺 key 显示 code 原文不炸

### Task 9 — e2e + 文档 + 部署

Files: `scripts/e2e_smoke/chart_report_e2e.mjs`、`Harness/wiki/chart-rendering.md`、`Harness/changes/2026-10-01-visual-output-strategy/summary.md`（新建）、`wiki/log.md`、`wiki/index.md`

- e2e 断言更新：多步最终回答**无图**、每个数据步有图+折叠表、rationale 行可见；单步图+表+依据三件套
- chart-rendering.md：契约一节加 tableOption/visualRationale、汇总步行为反转（0105 → 本次）写明原因
- 部署（决策 7 同批）：prod 备份 → `alembic upgrade head`（0107）→ 后端镜像重建 → `docker compose build --no-cache frontend` → 起容器 → e2e 8 步全绿

## 验证

1. 后端 unit 层：**整层跑 `pytest app/tests/unit -q`**（187 个文件；`app/tests/unit/conftest.py` 强制真实 PG、禁 sqlite，故需 `TEST_DATABASE_URL` 指向 qa-pg-a1/5434 的 `qa_metadata_test`）。
   - 内层快循环可用 3 个点名文件：`pytest app/tests/unit/test_visual_rationale.py app/tests/unit/test_visual_payload.py app/tests/unit/test_data_summary.py`（三者均已核实存在）—— 但**不能只用它代替整层**：本特性还改了 `chart_service.py`（`R_FORCED_CLIENT` rationale）与 `chat_chart_persist.py`，其用例在 `test_chart_service.py` / `test_chat_chart_persist.py` 等**未点名的文件**里。这与上面第 3 条前端假绿是**同一类**缺陷，别在两处各犯一次。
2. 后端集成层：`test_chat_stream_api.py` / `test_chat_api.py` / 多步套件（qa-pg-a1 / 5434 / **串行**）；**不要与 unit 层同进程混跑**（TRUNCATE 会抹掉 `ontology_class`）。
3. 前端：`cd frontend && npx vitest run`（**裸跑，别按目录过滤** —— 见下方更正）
   - ⚠️ **原命令是错的，会给出假绿**：原先写 `npx vitest run src/utils src/api src/stores src/components/chat`。
     前端的用例**几乎全在 `src/tests/`**（139/159 个测试文件），`src/pages/__tests__` 另有 15 个；
     `src/api`、`src/stores`、`src/components/chat` 下**一个测试文件都没有**。控制器实跑该命令：**只收集到 2 个文件 / 13 个用例**，
     而真实规模是四位数 —— 即它会「跑完、全绿、什么都没验」。`chatStore.test.ts` / `chatStoreDocQa.test.ts`
     也都在 `src/tests/` 下，Task 7 要改的正是这两个。
   - `vitest.config.ts` 的 `exclude` 已含 `e2e/**`、`node_modules/**`、`dist/**`、`.worktrees/**`
     ⇒ **裸跑即为正确范围**，不需要（也不该）手工列举目录。
   - 注意 `coverage.thresholds` 是 80%（lines/functions/branches/statements）—— 本仓既有记忆记录该门槛**本来就已失败**（原因在其他特性的 0% 覆盖）。故**只跑用例不上 `--coverage`**，覆盖门槛另案处理，别把既有红当成本次引入。
4. 红基线口径 —— **用控制器实跑建立的数字，不用「印象」**。
   - ⚠️ **计数会随每个任务变动，任何写进文档的通过/失败数都只是当时的锚点，不是断言**：
     Task 7 新增 16 条 ⇒ 前端从 `2 failed | 1365 passed (1367)`／159 文件 → `2 failed | 1381 passed (1383)`／160 文件；
     Task 8 还会再变。⇒ **失效的是计数，稳定的是「哪几条既有红」**。判定一律**现测**，别拿文档里的数当预期。
   - **前端既有红（两条，身份稳定，逐字）**：
     ① `chatStore > 流式 plan 事件回填 ReAct 查询计划（Phase E）`（`src/tests/chatStore.test.ts`，`expected undefined to be '各供应商的收货数量汇总'`）
     ② `EntityMappingPage > 点击新建并提交调用 createMapping（camelCase payload）`（jsdom `getComputedStyle` 伪元素未实现，见 memory `qa-system-jsdom-getcomputedstyle-shim`）
     ⇒ **①就在 Task 7 改的文件里**（Task 7 已完成，未弱化该断言）。
   - **后端既有红 —— 两个层，两个数，别互相代用**（同名症状不同作用域，本仓栽过一次）：
     - **unit 层：49 红**（`pytest app/tests/unit -q` ⇒ `49 failed, 3361 passed`，187 文件）。成因：chat 套件陈旧假替身 + 空计划闸门 `PLAN_EMPTY` 夹死、`test_dependencies.py` `'Header' has no attribute 'lower'`、`test_datasource_pool.py` Oracle 引号、`test_query_plan_generation.py` `pop from empty list`。**已用 BASE/HEAD 失败 ID 集合 diff 证实与 Task 10 无关（49 条逐字节相同）。**
     - **chat 集成套件：16 红**（`PLAN_EMPTY`，同一批陈旧假替身），已与 `5e5beb1` 基线逐字 diff 为空。
   - **判定方式**：拿失败 **`文件::用例`** 集合做 diff，**不按名字子串统计**（本仓栽过：`grep -c "multi_step"` 数到的是别的套件里名字含该词的用例）。
   - 基线不是 HEAD 而是别的提交时，前端要**从 `frontend/` 目录**跑（别从仓库根，会误选 `.worktrees/` 副本）；后端建基线用 worktree + 软链 `.venv`（见 memory `qa-system-worktree-symlink-baseline`）。
5. 反向守卫：多步场景确认顶层**不再**出现 chart 事件/字段（防「改完还发」）；汇总步 rationale 必须可见（防「去图连依据也丢了」）

## 依赖顺序

Task 1 → Task 3 → Task 4 → Task 5（rationale 贯穿主线）
Task 2 独立（阈值治理），Task 3 依赖它的 `loadFullDataThreshold`
Task 6 依赖 Task 4/5 的字段名
Task 7 依赖 Task 4 的契约形状；Task 8 依赖 Task 7；Task 9 最后
