# NL2SQL 引擎（Phase 3 / ReAct 两阶段 + 多轮对话）

## 流程（ReAct 两阶段）

1. 从会话上下文加载已绑定的本体（Class/Property/Metric）与数据源。
2. **第一阶段——生成查询计划**：`generateValidatedPlan` 调 LLM 产出结构化 `QueryPlan` JSON（target/selectedClasses/selectedProperties/conditions/aggregations/groupBy/joins/sortBy/rowLimit）；`validatePlan`（纯代码，不调 LLM）校验引用是否在本体 schema 中，失败时注入具体差异（"表 X 不在本体"）让模型重试，最多 `maxPlanAttempts=2` 次；`_finalizePlan`（统一出口）补充 JOIN + 校验连通性 + 应用**范围感知行数限制**（见下）。
3. **第二阶段——生成 SQL**：`generateSql(..., plan)` 把已校验的计划注入 System Prompt，模型仅按计划产出 SQL，避免单次生成+盲重试的语义漂移。
4. SQL Guard 校验合法性（仅 SELECT，无 DDL/DML，无多语句），且对只读有双重保障（`_assert_read_only`）。
5. 返回 SQL 与计划；执行成功后把计划/SQL/结果列持久化到会话状态。

## 多轮对话状态（Phase C）

- `session_query_state` 表（JSONB）：`last_question / last_plan / last_sql / last_result_columns / turn_count`，每次成功查询后 UPSERT。
- **意图识别**：5 类活跃意图 `query / new_query / refine / follow_up / clarify / chitchat`，规则匹配（不调 LLM）；`REFINE`/`FOLLOW_UP` 需上一轮已有状态（`hasPriorState`）。后端枚举另预留 `define/map/metric` 未接入。
- REFINE/FOLLOW_UP 轮次把上一轮状态经 `_sanitizeContext` 转义后包成 `<previous_query_state>` 注入两阶段 prompt，实现跨轮上下文传递。

## REFINE 捷径（Phase D）

`applyRefineDirect(sql, plan, question)` 纯代码改写上一轮 SQL（行数/排序/简单等值筛选），命中时**零 LLM 调用、零 token 成本**；无法安全识别返回 `None` 退回 LLM 两阶段。安全约束：

- 排序列/筛选列必须来自上一轮 `plan.selectedProperties` 且匹配 `_SAFE_IDENT_RE` 白名单（`^[A-Za-z_][A-Za-z0-9_]*$`）。
- 筛选值拒绝 `' " ; -- \`（防字符串闭合/注释/转义逃逸）；数值不加引号，其余按字符串字面量。
- 行数钳制到 `_REFINE_MAX_LIMIT`；方言未知时无行数子句不追加（退回 LLM）。
- 排序改写定位"最后一个 ORDER BY"（避免误改子查询内层）。

**已知缺口**（see changes/feat-scope-aware-row-limit/summary.md §4.1）：REFINE 捷径无法判断新问题的"范围"，追加筛选条件时保留上一轮行数限制。带时间范围的追问因 `_REFINE_CMP_RE` 要求"列 运算符 值"结构命中不了捷径，自然退回两阶段拿到正确行为；唯一残留缺口（"日期 BETWEEN a AND b"）用 `test_refine_shortcuts.py::test_refine_keeps_existing_row_limit_when_filter_added` 固化。**正解**（P2）：`state.last_plan.rowLimit == nl2sqlNoScopeRowLimit` 且新问题有 scope 时跳过捷径退回两阶段，而非改写 SQL 文本。

## 流式计划下发（Phase E）

SSE 新增 `plan` 事件（在 `sql` 之前），携带 `QueryPlan` 字典；前端流式路径 `onPlan` 回填 `queryPlan`，完成后配合 `isStreaming=false` 展示可折叠 `QueryPlanCard`。非流式响应在 `ChatResponse.queryPlan` 字段下发。

## SQL Guard（`infrastructure/security/sql_guard.py`）

- `sqlparse` 解析。
- 仅允许首 token 为 `SELECT`。
- 黑名单：`INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE/EXEC/UNION`。
- 拒绝多语句（`;`）。
- **行数兜底：`fetchmany(queryRowLimit=5000)` 上限**（实际值见 `QUERY_ROW_LIMIT`），并非 SQL 字符串改写——这一点对 SQL Guard 边界很重要。
- 连接级 `statement_timeout`。
- 连接 URL 校验，拒绝 `file://`，可选主机白名单。
- 审计日志：session、datasource、SQL、耗时。

## 4-Layer Routing Architecture (Phase 5)

NL2SQL requests are dispatched through a 4-layer cascade. Each layer has a specific trigger condition, cost profile, and failure fallback.

### Decision Flow

```
question
  │
  ▼
L1: KpiSemanticMatchService (Jaccard on kpi_catalog.semantic_keywords)
  │ match found?
  │   ├─ YES → return KPI SQL (cost: ~0ms, 0 tokens)
  │   └─ NO
  │       ▼
L2: LLM single SQL (existing ReAct two-phase, optionally CTE)
  │ execution ok?
  │   ├─ YES → return
  │   └─ NO (execution error, timeout, empty result)
  │       ▼
L3: ChainedStep CTE chain (prior_cte injection across multiple plans)
  │ execution ok?
  │   ├─ YES → return
  │   └─ NO
  │       ▼
L4: LangGraph Agent Loop (5 NL2SQL tools, iterative)
  │       │
  │       ▼
  return best effort or "cannot answer"
```

### Layer Trigger Conditions

| Layer | Trigger | Cost Profile |
|-------|---------|--------------|
| **L1** | `KpiSemanticMatchService.match(question)` Jaccard ≥ threshold | ~0ms, 0 tokens |
| **L2** | L1 miss; default for all other NL2SQL questions | 1× LLM call (plan + SQL) |
| **L3** | L2 execution fails; question involves multi-step/CTE composition | 1 + N× LLM calls |
| **L4** | L2/L3 exhaust all retries; complex multi-join requiring iterative tool use | N× LLM calls + tool overhead |

### Each Layer Detail

#### L1 — KPI Semantic Match (`KpiSemanticMatchService`)

- Jaccard similarity on `kpi_catalog.semantic_keywords` (stored as `TEXT[]` in PG, or JSON array).
- Keyword extraction: tokenize question, remove stopwords, compute set intersection / union.
- Match threshold: configurable (default 0.4). Below threshold → L1 miss → fall through to L2.
- On match: return pre-defined SQL from `kpi_catalog.sql_template` directly.
- **Code**: `app/services/kpi_semantic_match_service.py`

#### L2 — LLM Single SQL (existing ReAct two-phase)

- Phase 1: `generateValidatedPlan` → `QueryPlan` JSON (target/selectedClasses/selectedProperties/conditions/aggregations/groupBy/joins/sortBy/rowLimit).
- Phase 2: `generateSql(..., plan)` → SQL string.
- SQL Guard校验.
- Optional CTE enhancement: when `plan.requiresCte=True`, injects prior CTE as `WITH prior_cte AS (...)`.
- **Code**: `app/services/nl2sql_service.py:_planAndGenerateSql`

#### L3 — ChainedStep CTE Chain

- Used when a question requires joining results from multiple plans (e.g., "first query X, then use X's result to filter Y").
- `prior_cte` is built from the previous step's SQL result and injected as a `WITH` clause into the next step.
- Each step in the chain is validated independently before chaining.
- **Code**: `app/services/multi_step_plan.py` (ChainedStep class)

#### L4 — LangGraph Agent Loop

- LangGraph `StateGraph` with `AgentLoopState` (question, generated_sql, tool_calls, iterations, cost_so_far_usd).
- 5 NL2SQL tools registered: `list_tables`, `describe_table`, `sample_rows`, `execute_sql`, `list_joins`.
- Each iteration: LLM chooses tool → tool executes → result fed back → next iteration or final answer.
- Cost cap: `max_cost_usd=5.0` (configurable); loop exits if `cost_so_far_usd >= max_cost_usd`.
- Final SQL still passes SQL Guard before execution.
- **Code**: `app/services/agent_loop.py` ( `_runL4AgentLoop`)

### Fallback Chain

```
L1 miss → L2 → execution error → L3 → execution error → L4 → (best effort | cannot_answer)
```

Any layer that produces a valid, non-empty result that passes SQL Guard terminates the cascade.

### RoutingMetricsService

Aggregates `session_message.routing_layer` hits per layer for observability:

```python
# app/services/routing_metrics_service.py
async def aggregate(session_id: str) -> dict:
    rows = await db.fetch("""
        SELECT routing_layer, COUNT(*), AVG(latency_ms), SUM(token_cost_usd)
        FROM session_message
        WHERE session_id = $1 AND routing_layer IS NOT NULL
        GROUP BY routing_layer
    """, session_id)
    return {r["routing_layer"]: {...} for r in rows}
```

### MetricPromotionService (Cold Metric Auto-Promotion)

Scans frequently repeated L2 questions (identical md5 hash ≥ 3× per week), auto-creates `KpiCatalog` entry in `DRAFT` status for admin review:

```python
# app/services/metric_promotion_service.py
async def scan_and_promote():
    frequent = await db.fetch("""
        SELECT md5(question) as q_hash, question, COUNT(*) as cnt
        FROM session_message
        WHERE routing_layer = 'L2' AND created_time > now() - interval '7 days'
        GROUP BY q_hash, question
        HAVING COUNT(*) >= 3
    """)
    for row in frequent:
        await kpi_catalog.upsert({
            "code": f"AUTO_{row['q_hash'][:8]}",
            "question": row["question"],
            "status": "DRAFT",
            ...
        })
```

Admin reviews DRAFT KPI in `/admin/kpi-catalog`, approves → status becomes `ACTIVE`, next L1 hit promotes to fast path.

### Migration 0051

New columns on `session_message`:

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `routing_layer` | `VARCHAR(10)` | YES | L1/L2/L3/L4 |
| `latency_ms` | `INTEGER` | YES | End-to-end latency in ms |
| `token_cost_usd` | `FLOAT` | YES | Token cost in USD |

### Code Locations

| Component | File |
|-----------|------|
| Entry | `app/services/chat_service.py:_handleNl2SqlAgent` |
| L1 | `app/services/kpi_semantic_match_service.py` |
| L2 | `app/services/nl2sql_service.py:_planAndGenerateSql` |
| L3 | `app/services/multi_step_plan.py:ChainedStep` |
| L4 | `app/services/agent_loop.py:_runL4AgentLoop` |
| Metrics | `app/services/routing_metrics_service.py` |
| Promotion | `app/services/metric_promotion_service.py` |
| Migration | `migrations/versions/0051_add_routing_fields.py` |

## 派生指标 formula 必填硬约束（占比/比率/百分比）

`validatePlan`（`app/services/nl2sql_service.py:1387-1478`）在 aggregation 循环里加一条规则：**alias 命中派生指标关键词 → `formula` 必填**，否则 SQL 不会算百分比，占比沦为列别名，重试耗尽后整步被标"无法回答"被静默收纳（2026-08-17 真实回归：用户问"top10 物料的占比"时 LLM 倾向 `alias="占比"` 但无 formula → Step 失败，Step 3 因依赖被卡）。

关键词集合（中英文，大小写不敏感）：

| 关键词 | 场景 |
|---|---|
| `占比 / 比率 / 比例 / 百分比` | 中文派生指标最常见命名 |
| `ratio / percent / share / pct` | 英文派生指标命名 |

匹配策略：alias 经 `lower()` 后 `contains` 任一关键词。复合名（如 `总占比汇总`、 `RATIO_2025`）仍命中——确实应是派生指标，宁可过度保守（让 LLM 多写 formula），不让漏报。

报错文案（与 2026-08-14 sortBy 派生指标规则同款"带示例引导"风格）：

```
聚合别名 占比 是派生指标（占比/比率/百分比/比例/ratio/percent/share/pct），
必须使用 formula 表达式（窗口函数 SUM(x)/SUM(SUM(x)) OVER ()），
且 property 须填当前选中类的真实属性名；
若想用 SUM/COUNT/AVG 等基础聚合命名，请改用别名如 TOTAL_QTY/COUNT_NUM
```

**双层防御**（与 sortBy 派生指标规则同款设计思路）：

1. **prompt 引导**（`_buildPlanSystemPrompt`）：示例属性名用显式占位 `<当前选中类的真实属性名>`（不再是裸 `数量`），规则 4 改"formula 可选"为"问题含占比/比率/百分比时 formula **必填**"。
2. **代码层兜底**：validatePlan 校验 + `maxPlanAttempts=2` 重试注入 hint → LLM 第二次自愈。

反例守约：

- `alias="占比"` + 合法 formula `SUM(QTY)/SUM(SUM(QTY))OVER()` → 通过（已有 `test_formula_with_valid_properties_passes` 守约）
- 普通聚合 `alias="TOTAL_QTY"` / `alias="COUNT_NUM"` → alias 不含派生关键词 → 不被拦
- `alias="两年价格之差"`（语义派生但命名非典型）→ 不在关键词集合 → 不被拦；由 sortBy 派生规则守约（2026-08-14）

详见 `changes/fix-nl2sql-derived-metric-formula-required/summary.md`。

## 范围感知行数限制（scope-aware row limit）

`_finalizePlan` 出口处对 `plan.rowLimit` 做最后一次覆盖（frozen dataclass `replace`），按问题范围决定行数（详见 changes/feat-scope-aware-row-limit/summary.md）：

| 场景 | `rowLimit` |
|---|---|
| `plan.isUnanswerable` | 原样 |
| 用户表达条数/最值（前 N / top N / 最高 / 排名 / 最新） | 保留模型值 |
| 问题含时间范围 或 `plan.conditions` 非空 | `null`（不截断） |
| `plan.aggregations` / `groupBy` 非空 | 保留模型值 |
| 其余（无范围明细全表查询） | `NL2SQL_NO_SCOPE_ROW_LIMIT`（默认 100） |

**多步流水线**通过 `scopeQuestion` 透传主问题，子问题被 `rule_based_split` 切掉的年份会被并集还原。**prompt 同步引导**：计划 prompt 增加规则 7；SQL prompt 在有 plan 时附加"行数限制以查询计划为准"，避免 LLM 自行追加 LIMIT 让"有范围不限制"失效。

**回滚开关**：`NL2SQL_NO_SCOPE_ROW_LIMIT=0` 关闭兜底注入（规则 2 "有范围不限制" 仍生效）。

## 复合问题隐式多步拆解（无显式分步信号）

`ChatService` 入口（`processMessage` + `processMessageStream`）走三层判定决定走单步还是多步（详见 `changes/fix-compound-question-implicit-decomposition/summary.md`）：

| 层 | 触发器 | 作用 |
|---|---|---|
| **L1** | `is_explicit_multi_step` 命中关键词（"首先/其次/最后/分步"等） | 显式多步信号 → 直接走 `_resolveExplicitMultiStep` |
| **L1.5** | `_looks_like_compound_question` 启发式（**新增**） | 并列复合问题（用 +/和/，连接多个查询动词）→ 调 `_resolveExplicitMultiStep` 让 LLM 拆步 |
| **L2** | `_resolveExplicitMultiStep` 内部 plan() LLM | 拆步判定：返回 plan → 走 `_executeMultiStep`；返回 None → 退回单步 |
| **L3** | `_ANSWER_SYSTEM_PROMPT` 硬约束（**新增**） | 兜底：禁止 LLM 输出"询问继续"拟人化追问 |

### L1.5 启发式（`_looks_like_compound_question`）

**目的**：用户用 `+/和/，` 并列连接多个子查询（如"查询 A、查询 B、查询 C"），无任何显式分步信号，**不能走单步路径**（只生成覆盖 1/N 的 SQL，answer LLM 拿到残缺数据后编造"Step 2/3 暂无数据 + 询问是否继续"）。

**双模式触发**（任一命中即触发）：

1. **多动词并列**：≥2 段都含查询动词
   - 例：`统计供应商数量、计算订单总额、分析采购趋势`
2. **头+列表**：≥3 段且首段含查询动词（动词隐式作用于所有尾段）
   - 例：`查询3月份采购订单数量、Top 10物料占比、Top 10物料在4月份的订单数量`（用户场景）

**严格守约（不误拆单条查询）**：

- "对比 2024 和 2025 年的销售额" → 单动词 → **不触发**（避免误拆"对比"型单步查询）
- "统计各供应商的收货数量，按金额降序" → 第 2 段"按金额降序"无动词 → **不触发**（修饰语）
- "查询 A 和 B" → 第 2 段"B"无动词 → **不触发**（保守：宁可让 LLM 多调一次）
- 含禁用短语（`不要拆 / 不要分 / 用一条 / 单条查询 / 一条 SQL`）→ **不触发**

**性能**：单条查询零额外 LLM 调用；复合问题 +1 次（`_resolveExplicitMultiStep` 内部 plan()）。

### L3 answer 硬约束（`chat_stream_output.py`）

`_ANSWER_SYSTEM_PROMPT` 追加禁止反向追问段：

> **禁止反向追问**：不要询问用户'需要继续查询吗 / 是否需要进一步分析 / 还需要看其他吗'。
> 如查询结果不足以回答问题，直接说明当前结果能回答什么、不能回答什么即可。

即使 L1+L2 漏判，answer LLM 也不会输出"询问继续"的拟人化回复。

### L1 vs L1.5 触发顺序

```
L1 命中? ──yes──→ _resolveExplicitMultiStep（规则拆步，零 LLM）
       │no
       ↓
L1.5 命中? ──yes──→ _resolveExplicitMultiStep（plan LLM）
       │no
       ↓
走单步路径（_planAndGenerateSql）
```

L1 走纯规则（节省 LLM 成本），L1.5 调 plan() LLM。两者复用同一个 `_resolveExplicitMultiStep`，避免代码重复。

## 多步上下文强注入（实体列表作为筛选条件）

`StepExecutionContext.inject_to_prompt`（`multi_step_plan.py`）把前序 `StepResult` 渲染为可注入 plan + sql 两个阶段 prompt 的文本片段。2026-08-17 修复 Bug 4（Step N 引用 Step N-1 实体列表作为 WHERE IN 筛选条件）后，渲染按数据类型自动分档 + plan/sql prompt 共用 WHERE IN 强指令（详见 `changes/fix-multistep-context-strong-injection/summary.md`）。

### 渲染分档（`multi_step_plan.py`）

| 形态 | 判定 | 渲染策略 | 单项字符上限 |
|---|---|---|---|
| **ENTITY_LIST** | `len(data) ≤ 50` 且至少 1 个**字符串类型**列 | `列名: 值1, 值2, ...`（按列分别列值），`[entity_list]` 标签包裹 | 2000（默认 `injection_char_limit_entity`） |
| **AGGREGATE** | 其他（行数 >50 或全数值列或空数据） | JSON 摘要，`[aggregate]` 标签包裹 | 600（保持兼容 `injection_char_limit`） |

判定函数 `_detect_step_data_shape(data)`：纯函数，**非空 + ≤50 行 + 至少 1 个字符串列**才返回 `ENTITY_LIST`；其余一律 `AGGREGATE`。

**为什么按列分别列值？**——LLM 看到 `MATERIAL_ID: M001, M002, ..., M010` 比看到 `[{"MATERIAL_ID":"M001",...}, ...]` JSON 数组**更直接**生成 `WHERE MATERIAL_ID IN ('M001', ..., 'M010')`，无需二次解析。

### 容量分档理由

- **实体列表 2000 char/项**：Top 10（10×30 char ≈ 300 char）远小于 2000，完整可见；Top 30（30×30 char ≈ 900 char）也能完整传
- **聚合数值 600 char/项**：保留现状防 prompt 爆炸（4 前序步 × 600 = 2400 char ≈ 600 token）

### Plan + SQL prompt 强指令（`nl2sql_service.py:_renderStatePart`）

plan 与 sql 两个阶段共用 `_renderStatePart(priorState)` 模块级函数，措辞改一处两边同步。强指令分两类指引：

```
- 实体列表类结果（[entity_list] 标签，物料/客户/订单等主键列表）：
  当子问题用「这/这些/上述/前述/上一步/top N」指代前序步骤的实体时，
  必须从前序结果中提取对应主键列的取值列表，作为 WHERE <列> IN (...) 筛选条件使用，
  不要重新计算或忽略前序 ID。
- 聚合值类结果（[aggregate] 标签，数值/统计）：
  作为参考数据用于对比与展示，不要复用其数值作为新查询的输入。
```

**标签必须用方括号**（2026-08-17 复测回归修复）：`inject_to_prompt` 产物会作为 priorState 进入 `_renderStatePart`，后者对全文做 `_sanitizeContext`（`<` -> `&lt;`）--尖括号标签会被转义成 `&lt;entity_list&gt;`，结构化标记被破坏。方括号不受转义影响。组合链路守约：`test_nl2sql_service.py::test_composition_inject_to_prompt_survives_state_part`。

**关键指代词**：「这/这些/上述/前述/上一步/top N」——LLM 看到这些词 + `[entity_list]` 标签时**必须**走 WHERE IN。

**显式分步问题不得判为 REFINE/FOLLOW_UP**（2026-08-17 第三轮复测修复）：含 "top10" 的多步问题命中 `_REFINE_LIMIT_RE`（`top\s*\d`），在会话有历史状态时被误判为 REFINE，整体绕过多步入口（ChatService 多步入口仅 NEW_QUERY/QUERY 触发）。`IntentService._isRefine` / `_isFollowUp` 顶部有 `_isExplicitMultiStep` 守卫（复用 `StepQueryPlanner.rule_based_split`，≥2 个「第X步」/序数副词标号才命中）--显式分步一律 NEW_QUERY。单标号引用（"把第一步的结果按金额降序排序"）仍是 REFINE。守约：`test_intent_service.py::test_explicit_multi_step_not_refine_even_with_topn` 等 4 条。

**为什么不用"仅作参考"？**——`multi_step_plan.py:155` 旧措辞"仅作参考数据"是 REFINE/FOLLOW_UP 语境的语义（"上一轮状态只作上下文"）。**多步语境下语义完全相反**：子问题用"这top10物料"指代前序 ID 时，前序结果数据**必须**作为 WHERE IN 筛选值。强指令文案明确"WHERE IN 必填"。

### 渲染示例

**用户原问题**：
> "第一步统计3月份采购订单数量，输出列表，第二步统计3月份主要top10采购物料的占比，输出饼图，第三步分析这top10物料在4月份下的订单数量信息分析"

**Step 2 → Step 3 注入产物**（Top 10 物料 → ENTITY_LIST）：

```
前序步骤结果（可作为后续步骤的筛选条件使用；详见 [entity_list] / [aggregate] 标签说明）：
步骤 2：统计3月份主要top10采购物料的占比
  子问题：统计3月份主要top10采购物料的占比
  SQL：SELECT MATERIAL_ID, SUM(QTY) ...
  摘要：Top 10 物料采购量占比 0.85
  [entity_list]
    MATERIAL_ID: M001, M002, M003, M004, M005, M006, M007, M008, M009, M010
    占比: 0.18, 0.15, 0.12, 0.10, 0.08, 0.06, 0.05, 0.04, 0.03, 0.02
  [/entity_list]
```

**Step 3 LLM 看到 `[entity_list]` + WHERE IN 强指令 + 子问题"这top10物料"** → 生成 `WHERE MATERIAL_ID IN ('M001', ..., 'M010')`。

### 反例守约

- **单行全数值合计**（订单数 + 总数量）→ `_detect_step_data_shape` 返回 `AGGREGATE`（无字符串列）→ `[aggregate]` 标签，**不会被误判为实体列表**
- **>50 行**（即使有字符串列）→ `_detect_step_data_shape` 返回 `AGGREGATE`（>50 行熔断）→ `[aggregate]` 标签，**不切分档**
- **单步路径**（priorState 为 None）→ plan/sql prompt 不含 `<previous_query_state>` 段，**不污染单步路径**

### 关联

- 历史变更：`Harness/changes/fix-compound-question-implicit-decomposition/summary.md`（Bug 3，复合问题隐式拆解）——本修复与 Bug 3 互补：Bug 3 解决"是否被识别为多步"，本修复解决"多步之间上下文是否真的串联"
- 复用现有：`_sanitizeContext` 转义、`_clip_text` / `_clip_json` 截断工具、`StepExecutionContext.with_step` 不可变更新
- 不动：`_executeMultiStep` 控制流、`_planAndGenerateSql` 入口、L1/L2/L3 answer 硬约束（Bug 3）、REFINE/FOLLOW_UP 路径

## 多数据源

- `data_source` 表注册数据源，凭据 Fernet 加密。
- 动态 SQLAlchemy 引擎池：`dict[datasource_id, AsyncEngine]`，懒加载，更新/删除时 dispose。
- 默认 `is_read_only=True`。
- **多方言（#66）**：NL2SQL System Prompt 按 `datasource.type` 注入方言规则——Oracle 用 `FETCH FIRST N ROWS ONLY`，MySQL/PostgreSQL 用 `LIMIT N`；schema 前缀提示与 JOIN 示例仅对 Oracle 生效并使用 `datasource.username`（username 即 schema owner，不再硬编码 `ZJTH.`），MySQL/PG 不限定前缀、JOIN 示例为通用表名。未知/缺省类型回退 Oracle 方言。

## 准确性增强

- 注入数据库 ER 图描述（从 Ontology Service 动态拉取）。
- 初期限单表查询（SELECT...WHERE...GROUP BY），NL2SQL 通过测试后再放开 JOIN。

## 类召回窗口与规模化风险（已知限制，待优化）

> 记录于 2026-09-16。背景：ReceiptDetail 召回落榜事件（详见 memory `qa-system-milvus-ontology-vector-drift`）暴露的机制性限制，当前规模（96 类）实测够用，**类库增长到数百个后需要升级**。

### 机制（现状）

`chat_service._selectRelevantClasses` 每次提问独立执行（窗口是**每问一次**的，不是全局的）：

1. **向量召回 topK=15**（`_CLASS_FILTER_TOP_K`）：从全部类中按语义相似度挑 15 个候选；
2. **1-hop 扩边**（`_expandByJoinNeighbors`）：命中类沿 JOIN 目录把相邻表拉进来；
3. **上限 30**（`_CLASS_FILTER_MAX_CLASSES`）：命中 + 邻居合计截断，截断时记日志 `类召回扩边截断`。

窗口大小不随类库增长，但**挑选竞争加剧**。

### 风险表（类库增长后）

| 风险 | 机制 | 当前缓解 |
|---|---|---|
| 召回漏选 | topK 固定 15，类库越大相关表挤不进前 15 的概率越高 | 无（ReceiptDetail 事件即此类的实例） |
| 孤立漏选无法扩边 | 扩边只补「被命中类的邻居」；头表与明细表都落榜时无从谈起 | 无 |
| 扩边截断 | 命中 15 + 邻居稠密时达 30 上限被截 | 有日志（类召回扩边截断） |

### 升级路径（按成本从低到高，出现真实漏选案例后再做，勿提前）

1. **调大常量**：`_CLASS_FILTER_TOP_K` / `_CLASS_FILTER_MAX_CLASSES`（改两个常量，注意 prompt 长度代价）；
2. **多路召回**：问题改写为 2-3 个子查询分别召回再合并去重（对「供货量→收货明细+到货明细」类问题有效）；
3. **两阶段检索**：宽召回（topK≈50）后用 LLM/cross-encoder 精排到 30。

### 验证方法

出现「问了 A 却没看到表 B」时，先看后端日志 `类召回扩边 hits=X expanded=Y total=Z`：

- `Z=30` → 截断问题（调上限即可）；
- 相关表不在 15 个 hits 里 → 召回问题（走多路召回）。

配套可观测性：`/chat` 响应已带 `classRecall` 诊断字段（mode/hitCount/classCount/truncated），前端在截断/降级时向用户展示提示（2026-09-16 落地）。
