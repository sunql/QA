# feat-research-entry — 研究型 Agent 入口

> **SSOT 摘要**：完整设计见 `docs/superpowers/specs/2026-10-04-research-entry-design.md`
> 状态：设计草案，待用户最终审阅
> 日期：2026-10-04

## 一句话

新增「研究型 Agent 入口」与现有 chat 完全并列；复用 ontology/wiki/nl2sql/chart/hypothesis/evidence 资产；新增 `ResearchAgentService` + `EnterpriseSemanticLayer` + `ReportPlanner` 三个组件 + 5 张新表。

## 关键决策

| 维度 | 选择 |
|---|---|
| 产物形态 | 流式 SSE 进展 + 最终归档报告 |
| 架构路线 | 新服务 + 独立会话（5 张新表）|
| 主动确认节点 | 固定 3 个 + 动态 4 信号 |
| 段落 mode | research / attribution / compare（MVP 三 mode）|
| chart/table 数据值 | 确定性读 StepResult，LLM 只生成解读文本 |
| 菜单入口 | menu-config-seed 自动注入，不硬编 |
| SSE | 完全独立 `/api/v1/research/stream` |

## 数据边界

- **新增 5 表**：`research_session` / `research_turn` / `research_checkpoint` / `research_finding` / `research_report`
- **不触碰**：`chat_session` / `session_message` / `analysis_hypothesis` / `evidence` 任何字段
- **只读访问**：`knowledge_claim` / `wiki_page`（不写）

## 新增组件

| 组件 | 文件 | 备注 |
|---|---|---|
| `ResearchAgentService` | `services/research_agent_service.py` | 调度主；turn 状态机 + checkpoint pause/resume + Token 计量 |
| `EnterpriseSemanticLayer` | `services/enterprise_semantic_layer.py` | 3 臂拆解（BO/Metric/Knowledge）；**不调 LLM** |
| `ReportPlanner` | `services/report_planner.py` | 段落装配三 mode；文字块单 finding 单 LLM call |

## 关键复用（service 级公开方法，不穿透 mixin 私有）

- `IntentService.classifyResult()`
- `OntologyService.searchByKeyword / listJoins / listMetrics`
- `KpiSemanticMatchService.match()`
- `WikiVectorService.searchSemantic()`
- `StepQueryPlanner.plan() / rule_based_split()`
- `Nl2SqlService.generateSql() + executeSql()`
- `ChartService.buildChart()`
- `HypothesisMixin.runHypotheses()`（待验证签名）
- `EvidenceRecordService.recordFindingEvidence()`（**待新增**面向 finding 的公开方法）

## SSE 事件（新命名空间）

`research.intent / esl / checkpoint / plan / step.* / hypothesis / finding / report / done / error`
仅走 `/api/v1/research/stream`，**不与 chat SSE 互通**。

## 前端

- 路由：`/research` + `/research/sessions/:id` + `/research/sessions/:id/report` + `/research/sessions/:id/compare`
- 4 个核心组件：`ResearchTimeline` / `CheckpointCard` / `ReportRenderer` / `ResearchCompareView`
- store：`useResearchStore`（zustand，与 `useChatStore` 并列）
- i18n：`research.*` 命名空间

## 固定 Checkpoint（3 个）

| 序号 | 触发时机 | 用户回答什么 |
|---|---|---|
| #1 | ESL 输出后 | 三臂是否齐全 / 是否换词 |
| #2 | Plan 生成后 | 分几步 / 查哪几张表 / 维度 |
| #3 | Hypothesis 候选出后 | 多选 / 排序 |

## 动态 Checkpoint（相位 × 信号两层）

checkpoint 的**相位**（落 `research_checkpoint.phase`，`research_agent_ports.py:43-47`）：

`intent` / `planning` / `hypothesis` / `runtime_dynamic` / `low_confidence_step`

其中 `intent` / `planning` / `hypothesis` 是固定 #1/#2/#3 的相位；`runtime_dynamic` 是 ESL
冲突的动态点（决策后直进 plan）；`low_confidence_step` 是执行步失败/空数据的动态点。

动态**信号名**（落 `options["signal"]`，`research_agent_ports.py:57-63`）：

`fixed_scope` / `fixed_plan` / `fixed_hypothesis` / `empty_scope` / `low_confidence_step` / `sql_validation_failed`

`sql_validation_failed` 是 `low_confidence_step` 相位下的**信号名**，与错误码 `sql_validation_failed`
**同值**（`research_agent_ports.py:138` 有显式注释承认该同值刻意为之）——它是信号，不是相位；
5 个相位里没有任何一个是错误码。ESL 冲突种类（`metric_ambiguous` / `wiki_disagree`）是**第三套词汇**，
不属于 checkpoint 相位，也不属于信号。

## 错误码契约

`research.error` 的 `code` 单一事实来源是 `backend/app/services/research_agent_ports.py:159-195`
的 `ERROR_SPECS`（`ErrorSpec` frozen dataclass：`terminal` / `sessionStatus` / `payloadFields` /
`uiHint` / `summary`）。终态集合 `TERMINAL_ERROR_CODES` 由表派生（`:197`）。

| code | terminal | sessionStatus | payloadFields | uiHint |
|---|---|---|---|---|
| `turn_failed` | `True` | `failed` | `("phase",)` | `terminal` |
| `llm_unavailable` | `False` | `running` | `()` | `degraded` |
| `hypothesis_generation_failed` | `False` | `running` | `()` | `degraded` |
| `sql_validation_failed` | `False` | `awaiting_user` | `("stepIndex",)` | `degraded` |
| `step_failed` | `False` | `awaiting_user` | `("stepIndex",)` | `degraded` |

三条铁律：

1. 终态集合由 `ERROR_SPECS` **派生**（`TERMINAL_ERROR_CODES`，`:197`），任何站点**不得自持字面量集合**；
2. checkpoint **相位**词汇绝不用作错误码；
3. 前端按 `uiHint` 的**类**分支（`terminal` / `degraded`），**不按 code 逐个判断**。

线上形状：`research.error` payload = `{code, message, uiHint, ...该 code 的 payloadFields}`，其中
`code` / `message` / `uiHint` **恒在**，`uiHint` 由表**派生**、**不属于** `payloadFields`。

## 后续优化钩子

- ESL 后置 LLM 精化层（**不混进 ESL 本身**，挂在 Checkpoint #1 之前）
- `compare` 段落 mode 实现（MVP 后做）
- 多 turn 协同 / ACL（预留）

## 实现记录

本特性经 Task 1–12 分批落地（设计 → 5 张表迁移 → ESL 三臂 → 持久化/状态机/SSE/REST → 报告归档 →
步 SQL 接线与路由/计量 → 前端三页面 → 菜单 seed → 对比视图），逐任务 TDD + code-review 收口。

- **13a 客观自动回归**：后端 7 个研究套件 73 passed / 0 failed；单元基线两向空 diff（0 新增 / 0 修复）；
  覆盖率 9 模块全部 ≥80%（合计 91%）。发现 2 条本特性引入的集成红（`test_menu_config_api.py` 陈旧
  section 计数锚点 7→8），交 13c 修；另发现 Milvus/Neo4j 集成套件无 deadline 等待会 stall（遗留项）。
- **13b 真机验收**（本任务）：6 项行为检查通过；发现产品缺陷 P1——`buildResearchAgentService()`
  未注入 `nl2sql`，生产链路 step 数据事件（`research.step.sql/data/chart/done`）从不触发，已记录待裁定。
- **遗留/延后**：step NL2SQL 工厂接线（P1）；`test_menu_config_api.py` 计数锚点（13c）；集成套件
  stall 的 `pytest-timeout` / gRPC deadline；ESL 后置 LLM 精化层；`compare` 段落 mode。

## 关联

- 完整设计：`docs/superpowers/specs/2026-10-04-research-entry-design.md`
- 上下文：复用 `Harness/changes/2026-09-30-wiki-ontology-link-fix/`、`Harness/changes/2026-09-30-chart-in-final-report/` 等现有能力
- 待办：「`HypothesisMixin` / `EvidenceRecordService` 公开方法签名验证」列为实施前 1-day 调研