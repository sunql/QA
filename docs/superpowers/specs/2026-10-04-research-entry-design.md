/# 研究型 Agent 入口 — 设计文档

> **For agentic workers:** 本设计文档是研究型新入口的 SSOT 实现契约。落 SSOT 摘要于 `Harness/changes/feat-research-entry/summary.md`。
>
> 状态：设计草案，待用户最终审阅。
> 日期：2026-10-04
>
> **脑暴来源：** 2026-10-04 用户提案——复用现有 ontology+wiki 资产，新增「Agent 模式」研究入口。

## 1. 目标

新增「研究型 Agent 入口」—— 与现有 chat（短问答）**完全并列、不互相干扰**。复用现有 ontology / wiki / nl2sql / chart / hypothesis / evidence 资产，新增三大组件：

- **Enterprise Semantic Layer (ESL)** — 把研究问题拆到 Business Object / Metric / Knowledge 三臂
- **Report Planner** — 把 step_results + findings + claim 拼成 Text/Table/Chart 最终报告
- **ResearchAgentService** — 调度主，负责 turn 状态机、checkpoint 暂停/恢复、Token 计量

## 2. 关键决策（用户已确认）

| 决策点 | 选择 |
|---|---|
| 产物形态 | 混合：流式 SSE 进展 + 最终归档报告 |
| 架构路线 | 新服务 `ResearchAgentService`，研究会话独立（5 张新表）|
| 主动确认节点 | 固定 3 个 + 动态触发（混合）|
| 段落 mode | `research` / `attribution` / `compare`（MVP 三个）|
| 文字块生成 | 单 finding / 单 exec_summary 各一次 LLM call |
| chart/table LLM | 数据值仍读 StepResult；LLM 只生成解读文本与数字标注 |
| 侧边栏对比 | 支持（新增功能）|
| 菜单入口 | 走 `menu-config-seed` 自动注入，不硬编 |
| SSE 端点 | 完全独立 `/api/v1/research/stream`（不与 chat 共享通道）|

## 3. 数据模型（5 张新表，全部独立 schema）

> **关键边界**：不触碰 `chat_session` / `session_message` / `analysis_hypothesis` / `evidence` 任何字段。
> 只读访问 `knowledge_claim` / `wiki_page`，不写。

```sql
-- 1) 会话主表
research_session
─────────────────
id              uuid PK
title           text
mode            text default 'research'   -- 预留 research/attribution/comparison
status          text                       -- running | awaiting_user | done | failed | aborted
created_by      bigint FK user.id
updated_at      timestamptz
input_seed      text                       -- 首轮原始问题

-- 2) 轮次（一次用户输入 + 一次完整流水 = 1 turn）
research_turn
─────────────────
id              uuid PK
session_id      uuid FK → research_session
turn_index      int
role            text                       -- user | agent | checkpoint_awaiting
content         jsonb                      -- 不同 role 不同结构（见 §4.3）
created_at      timestamptz

-- 3) 暂停点（用户主动确认节点）
research_checkpoint
─────────────────
id              uuid PK
session_id      uuid FK
turn_id         uuid FK
phase           text                       -- intent | planning | hypothesis | runtime_dynamic
status          text                       -- pending | confirmed | modified | rejected
options         jsonb                      -- 结构化选项
user_choice     jsonb
decided_at      timestamptz

-- 4) 发现（Hypothesis+验证 落定）
research_finding
─────────────────
id              uuid PK
session_id      uuid FK
turn_id         uuid FK
claim_text      text
supporting_sql  text
supporting_data jsonb
confidence      numeric(0–1)
created_at      timestamptz
-- 不引用 analysis_hypothesis（完全自闭环，避免双向耦合）

-- 5) 报告（1 session 1 份当前 published + 多份 superseded）
research_report
─────────────────
id              uuid PK
session_id      uuid FK → research_session  -- 不是 UNIQUE（保留多份重跑）
version         int
payload         jsonb                       -- {sections:[...], findings_ref:[...]}
rendered_md     text
status          text                        -- draft | published | superseded
created_at      timestamptz
-- 部分唯一索引：同 session 同时只能有 1 份 published
CREATE UNIQUE INDEX uq_research_report_session_published
  ON research_report (session_id) WHERE status = 'published';
```

### 3.1 数据模型取舍

| 决策 | 原因 |
|---|---|
| `research_finding` **不挂** FK 到 `analysis_hypothesis` | 自闭环；chat 那边的假设表生命周期不对齐，避免双向耦合 |
| `research_report` **1 session 1 份最新版**，重跑版本自增 | 保留历史可对比（用户可在 UI 切换 version）|
| `research_turn.content` 用 **jsonb 不分类型表** | 用户输入 / Agent 事件 / Checkpoint 选项形态差太大，分表成 4 张表收益小 |
| `research_checkpoint.options` **结构化 jsonb** | 给前端可渲染的「卡片式选项」（hypothesis 候选卡、plan 候选卡）|

## 4. 组件清单

### 4.1 新增（3 个）

| 组件 | 文件 | 职责 |
|---|---|---|
| `ResearchAgentService` | `services/research_agent_service.py` | 调度主；turn 状态机、checkpoint 暂停/恢复、Token 计量 |
| `EnterpriseSemanticLayer` | `services/enterprise_semantic_layer.py` | 3 臂拆解（BO / Metric / Knowledge），**不调 LLM**，输出置信度与冲突标记 |
| `ReportPlanner` | `services/report_planner.py` | 段落装配（research / attribution / compare 三 mode）|

### 4.2 复用（service 级公开方法调用，**不穿透 mixin 私有**）

| 资产 | 现有服务 |
|---|---|
| Intent 分类 | `IntentService.classifyResult()` |
| BO / Property / Metric / Join / Relation 检索 | `OntologyService.searchByKeyword / listJoins / listMetrics` |
| KPI 匹配 | `KpiSemanticMatchService.match()` |
| Wiki 语义检索 | `WikiVectorService.searchSemantic()` |
| 多步计划 | `StepQueryPlanner.plan() / rule_based_split()` |
| NL→SQL 执行 | `Nl2SqlService.generateSql() + executeSql()` |
| 图表生成 | `ChartService.buildChart()` |
| Hypothesis | `HypothesisMixin.runHypotheses()`（公开方法，待验证）|
| Evidence | `EvidenceRecordService.recordFindingEvidence()`（新增面向 finding 的公开方法）|
| Knowledge 编译 | `KnowledgeCompilerService.compileObject()` |
| 知识提取 | `WikiQaService._loadClaims()`（已在 wiki_qa_service 中）|

### 4.3 关键流程：单 turn 状态机

```
[user_input]
     │
     ↓
[1. Intent 分类]            ← IntentService.classifyResult (复用)
     │ mode=research?
     ↓
[2. ESL 拆 3 臂]            ← EnterpriseSemanticLayer.extract (新)
     │ 输出 {business_objects, metrics, knowledge, confidence_by_arm, conflicts}
     ↓
[3. ★ FIXED CHECKPOINT #1: 范围确认]
     │ 三臂是否齐全 / 是否要加 / 是否换词
     ↓
[4. Analysis Planner]       ← StepQueryPlanner.plan() (复用)
     ↓
[5. ★ FIXED CHECKPOINT #2: 计划确认]
     │ 分几步 / 查哪几张表 / 维度
     ↓
[6. 每步循环执行]            ← 复用现有 _executeDataStep
     │ ★ 每步可选 DYNAMIC CHECKPOINT（4 信号触发）
     ↓
[7. Hypothesis 显式跑]       ← HypothesisMixin.runHypotheses() (复用)
     ↓
[8. ★ FIXED CHECKPOINT #3: 假设挑选]
     │ 多选 / 排序
     ↓
[9. Evidence 验证]           ← EvidenceRecordService.recordFindingEvidence (新加)
     ↓
[10. Result Analyzer]       ← ChartService.buildChart() 复检
     ↓
[11. Report Planner 拼报告]  ← ReportPlanner.compose() (新)
     ↓
[12. 归档 research_report]   (version++, status='published')
     ↓
[done]
```

### 4.4 Pause/Resume 落库动作

| 节点 | 落表 |
|---|---|
| Agent 暂停 | `research_checkpoint(status=pending, options)` + `research_turn(role=checkpoint_awaiting, content=options)` |
| 用户响应 | `research_checkpoint(status=confirmed/modified/rejected, user_choice)` + `research_turn(role=user, content=choice)` |
| Agent 继续 | checkpoint 标记 consumed，继续推到下一阶段 |

### 4.5 SSE 事件（独立端点 `/api/v1/research/stream`）

```
event: research.intent           {intent, mode}
event: research.esl              {business_objects, metrics, knowledge, conflicts}
event: research.checkpoint       {checkpoint_id, phase, options, prompt}
event: research.plan             {steps[]}
event: research.step.start       {index, description}
event: research.step.sql         {index, sql}
event: research.step.data        {index, data}
event: research.step.chart       {index, chart_type, chart_option}
event: research.step.done        {index, summary}
event: research.hypothesis       {candidates[]}
event: research.finding          {finding_id, claim, supporting_sql, supporting_data}
event: research.report           {report_id, version, sections_preview}
event: research.done             {report_id, version}
event: research.error            {code, message}
```

### 4.6 Enterprise Semantic Layer（ESL）

**输入**：`{question: str, intent: IntentResult, session_ctx?: ResearchSessionContext}`
**输出**：`ESLResult`（frozen dataclass）

```python
@dataclass(frozen=True)
class ESLExtraction:
    business_objects: list[BusinessObjectRef]
    metrics: list[MetricRef]
    knowledge: list[KnowledgeRef]
    confidence_by_arm: dict[str, float]   # 三个臂各 0–1
    conflicts: list[ESLConflict]

@dataclass(frozen=True)
class BusinessObjectRef:
    class_id: int; class_name: str; source_table: str
    matched_alias: str; confidence: float
    related_joins: list[int]

@dataclass(frozen=True)
class MetricRef:
    metric_id: int | None
    kpi_code: str | None
    display_name: str
    formula: str | None
    confidence: float

@dataclass(frozen=True)
class KnowledgeRef:
    claim_id: int | None
    page_id: int | None
    title: str
    snippet: str
    semantic_score: float

@dataclass(frozen=True)
class ESLConflict:
    kind: str                 # 'metric_ambiguous' | 'wiki_disagree' | 'bo_join_missing'
    arm: str                  # 'metric' | 'knowledge' | 'business_object'
    detail: str
    candidates: list[dict]
```

**内部流水线：**
```
ESL.extract(question, intent, ctx)
  ├─ 1. BO 臂: OntologyService.searchByKeyword → class_name/alias 二次匹配 → listJoins
  ├─ 2. Metric 臂: KpiSemanticMatchService.match → 低置信(<0.6) 二次查 OntologyMetric
  └─ 3. Knowledge 臂: WikiVectorService.searchSemantic → _loadClaims(pageId)
```

**硬约束：**
- **不调 LLM**（避免置信度信号污染 + 单臂不可观测）
- **不写库**（只读 ontology / wiki）
- **无状态**（session 信息由 ResearchAgentService 注入）

**失败/降级：**
- 三臂都空 → `EmptyResearchScopeError` → Checkpoint #1 强制用户改写问题
- 单臂空 → `confidence_by_arm[arm]=0`，Checkpoint #1 主动展示
- Wiki 超时 → `KnowledgeRef=[]`，Checkpoint #1 文字提示

### 4.7 Report Planner

**输入**：`{sessionId, turnId}`
**输出**：`ReportPayload` (JSON, 存档用) + `rendered_md` (可下载)

**段落 mode 模板：**

| Mode | 段落顺序 |
|---|---|
| `research` | ①执行摘要 ②数据章节（每 step 一段 chart+table+bullet）③引用知识 ④方法学 |
| `attribution` | ①结论（领先假设 + 置信度）②假设验证表 ③每个被验假设的 chart+数据 ④备选假设（未验）⑤方法学 |
| `compare` | ①对比维度表 ②每个对象的数据章节 ③差异分析（LLM 生成）④方法学 |

**段落形状：**

```python
@dataclass(frozen=True)
class ReportSection:
    id: str; kind: str; title: str
    blocks: list[ReportBlock]

@dataclass(frozen=True)
class ReportBlock:
    type: str                              # text | chart | table | bullet_list
    content: str | dict | list
    source_refs: list[SourceRef]

@dataclass(frozen=True)
class SourceRef:
    kind: str                              # step | finding | wiki | kpi
    ref_id: str
    label: str
```

**内部流水线：**
```
compose(sessionId, turnId)
  ├─ 1. 数据收集（只读 step_results / findings / checkpoints / session.seed）
  ├─ 2. LLM 文字生成（仅文字块，单 call/块）
  │     ├─ executive_summary  ← PromptFenceNeutralize + LlmFenceStripping
  │     ├─ finding_commentary ← 每 finding 一段，结合 supporting_data 数字
  │     └─ chart/table 解说文字（数据值仍读 StepResult.data，LLM 只标注数字 + 解读）
  ├─ 3. 确定性块构建：chart_block / table_block（不走 LLM）
  ├─ 4. 段落装配（按 mode 模板插入数据）
  ├─ 5. MD 渲染（jinja2-like 模板）
  └─ 6. 写库：version=max+1，旧版 superseded
```

**约束：**
- chart/table **数据值**绝不调 LLM（防幻觉污染数字）
- LLM 文本**必须能解析回 SourceRef**（防 LLM 改数，渲染时数字替换回表格块）
- 文字块单 finding 单 call（不一次性塞所有数据）

### 4.8 动态 checkpoint 触发信号（4 条）

| 信号 | 触发条件 |
|---|---|
| `metric_ambiguous` | KpiCatalog + OntologyMetric 双源给出候选，分差 < 0.1 |
| `wiki_disagree` | ≥2 page 共主题且 claim_text 字符 Jaccard < 0.5 |
| `low_confidence_step` | StepResult.data 为空 或 chart_type='null' | 加 LLM 二次精化层（**后置 ESL，非 ESL 内部**）  ← 后续优化钩子 |
| `sql_validation_failed` | SQL 校验失败（非 GatewayException）|

## 5. 前端集成

### 5.1 路由（与 chat 完全并列）

```
/research                                  # 列表（左侧进行中 / 已归档）
/research/new                              # 新建
/research/sessions/:sessionId              # 会话详情（turn 时间线 + 流式）
/research/sessions/:sessionId/report        # 最终报告
/research/sessions/:sessionId/archive      # 历史版本切换
/research/sessions/:sessionId/compare      # 侧边栏勾选对比视图（≥2 sessions）
```

### 5.2 核心组件

| 组件 | 职责 |
|---|---|
| `<ResearchTimeline>` | 左时间线：所有 turn / checkpoint / finding / report 节点；点击跳锚 |
| `<CheckpointCard>` | 暂停卡片：phase + options + 用户选/拒/改 UI；点击发新 turn |
| `<ReportRenderer>` | 报告渲染：按 ReportSection.blocks 顺序渲染 text/chart/table/bullet |
| `<ResearchCompareView>` | 侧边栏勾选 ≥2 → 并排对比 report（标题 / 执行摘要 / 关键指标 / findings）|

### 5.3 边界（不与 chat 前端互串）

- 路由独立 `/research/*`，chat 不引 ResearchTimeline
- store 新建 `useResearchStore`（zustand），与 `useChatStore` 并列
- API client 新建 `api/research.ts`，与 `api/chat.ts` 并列
- i18n key `research.*` 命名空间
- 菜单走 `menu-config-seed` 自动注入，**绝不硬编**导航栏

### 5.4 SSE

- 完全独立 SSE 端点 `GET /api/v1/research/stream?sessionId=xxx`
- 不与 chat 共享通道（部署/性能/调试隔离）

### 5.5 构建约束（沿用 CLAUDE.md）

- 改前端必须 `docker compose build --no-cache frontend`
- 前端测试 vitest + RTL ≥ 80% 覆盖
- i18n zh/en 双语
- 不裸 `docker build`，否则 tag 前缀差 `system-` 容器拉旧镜像

## 6. 测试策略

| 层 | 工具 | 重点 |
|---|---|---|
| Unit | pytest | ESL / ReportPlanner / InteractiveLoop 状态机 |
| Integration | pytest + 真实 PG (qa_metadata_test, localhost:5434) | 新表 + checkpoint 持久化 |
| E2E | Playwright（Vercel Agent Browser） | 流式 + checkpoint 交互 + 报告归档 |
| Frontend | vitest + RTL | 4 个核心组件 + useResearchStore |

覆盖率 ≥ 80%（CLAUDE.md 红线）。

## 7. 实施顺序

1. **数据迁移**：alembic 新增 5 张表（**不手工跑 upgrade head**，CI 自动）
2. **ESL + Report Planner + ResearchAgentService** 三个核心服务（TDD）
3. **SSE 端点 + Checkpoint 持久化**（集成测试）
4. **前端 4 个组件 + useResearchStore + menu-config-seed 注入**
5. **侧边栏对比视图**
6. **真实跑通研究型完整 turn**：意图分类 → ESL → Checkpoint → Plan → Step → Hypothesis → Finding → Report → 归档

## 8. 待办 & 后续优化钩子

- [ ] **ESL 后置 LLM 精化层**（不混进 ESL 本身）：当 ESL 召回质量差时启用，挂在 Checkpoint #1 之前
- [ ] **HypothesisMixin / EvidenceRecordService 公开方法签名验证**（依赖现有代码细节）
- [ ] **`compare` 段落 mode 实现**（MVP 先做 research + attribution）
- [ ] **`probeDeadEdges` Oracle LIMIT 兼容**等独立小缺陷（沿用既有）
- [ ] **侧边栏对比**：版本切换下报告 diff
- [ ] **多用户协同**：当前未设计；预留 `research_session.created_by` 后续加 ACL

## 9. 风险

| 风险 | 缓解 |
|---|---|
| 新服务绕开现有 chat 行为（可能漏 token 计量 / 审计） | ResearchAgentService 入口先调 Token 计量中间件 + audit_history 写入钩子 |
| LLM 文字块改数（幻觉污染报告） | 渲染时数字替换回表格块 + SourceRef 反向追溯 |
| 多 turn checkpoint 状态在内存中丢失 | 每个 checkpoint 必落库；resume 时从 DB 重建状态 |
| 报告重跑覆盖用户收藏 | version 自增，旧版 status='superseded' 不删 |