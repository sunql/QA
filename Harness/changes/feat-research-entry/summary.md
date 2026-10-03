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

## 动态 Checkpoint（4 信号）

`metric_ambiguous` / `wiki_disagree` / `low_confidence_step` / `sql_validation_failed`

## 后续优化钩子

- ESL 后置 LLM 精化层（**不混进 ESL 本身**，挂在 Checkpoint #1 之前）
- `compare` 段落 mode 实现（MVP 后做）
- 多 turn 协同 / ACL（预留）

## 关联

- 完整设计：`docs/superpowers/specs/2026-10-04-research-entry-design.md`
- 上下文：复用 `Harness/changes/2026-09-30-wiki-ontology-link-fix/`、`Harness/changes/2026-09-30-chart-in-final-report/` 等现有能力
- 待办：「`HypothesisMixin` / `EvidenceRecordService` 公开方法签名验证」列为实施前 1-day 调研