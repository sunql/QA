# NL2SQL 引擎（Phase 3 / ReAct 两阶段 + 多轮对话）

## 流程（ReAct 两阶段）

1. 从会话上下文加载已绑定的本体（Class/Property/Metric）与数据源。
2. **第一阶段——生成查询计划**：`generateValidatedPlan` 调 LLM 产出结构化 `QueryPlan` JSON（target/selectedClasses/selectedProperties/conditions/aggregations/groupBy/joins/sortBy/rowLimit）；`validatePlan`（纯代码，不调 LLM）校验引用是否在本体 schema 中，失败时注入具体差异（"表 X 不在本体"）让模型重试，最多 `maxPlanAttempts=2` 次；`_finalizePlan`（统一出口）补充 JOIN + 校验连通性 + 应用**范围感知行数限制**（见下）。
3. **第二阶段——生成 SQL**：`generateSql(..., plan)` 把已校验的计划注入 System Prompt，模型仅按计划产出 SQL，避免单次生成+盲重试的语义漂移。
4. SQL Guard 校验合法性（仅 SELECT/WITH，无 DDL/DML，无多语句、无侧信道函数），且对只读有双重保障（`_assert_read_only`）。
5. 返回 SQL 与计划；执行成功后把计划/SQL/结果列持久化到会话状态。

### 计划解析契约（2026-09-26，M3）

`QueryPlan.from_dict` 依旧是「**绝不抛错**」的容错解析（历史 JSONB / LLM 回复都可能损坏），但不再静默：

- `QueryPlan.from_dictWithReport(payload) -> (plan, drops)`：语义与 `from_dict` **逐字节相同**（`from_dict` 内部即委托给它），额外返回 `tuple[PlanDrop, ...]`——按 `(字段, 原因, 原始类型)` 聚合的丢弃报告，`PlanDrop` 是 frozen dataclass 且**不存原始值**（避免把模型输出带进日志）。`from_dict` 仍是唯一被业务代码调用的入口除了下面两个观测点。
- 原因常量集中在 `app/domain/plan_drop.py`（`DROP_*`），domain 层保持无日志、无 IO；格式化单点在 `formatPlanDrops`（单行、同类聚合计数如 `selectedProperties:DROP_ITEM_NOT_STR(int)x2`）。
- 两个调用点各写**一条** `reason=` 日志（沿用 `_fallbackRecall` 的单点日志风格）：
  - `nl2sql_service._parsePlanOutcome`：失败原因分类为 `PLAN_REPLY_EMPTY` / `PLAN_REPLY_NO_JSON` / `PLAN_REPLY_TOO_LARGE` / `PLAN_REPLY_JSON_INVALID` / `PLAN_EMPTY`；成功但有内容级丢弃 → `PLAN_DEGRADED`（**不失败**）。
  - `chat_service._statePlan`（DB JSONB 历史路径）：`PLAN_HISTORY_DEGRADED`，**只上报不收紧**——历史计划可能来自旧版本，收紧会让历史会话整段失败。
- **全空计划判失败（行为变更）**：`target`（strip 后）为空、且 selectedClasses/selectedProperties/conditions/aggregations/groupBy/joins/sortBy/partitionBy/rowLimit/perGroupLimit 全空 ⇒ 视同解析失败进入既有重试，重试耗尽后落到「无法回答」。判据取**最窄口径**，且 `target="无法回答"` 的合法空计划不受影响。判定在 service 调用点（`_isEmptyPlan`）而非 domain，故不影响 `_statePlan`（`chat_service` 的 REFINE 直写闸门以 `plan is None` 判成败，不能被误判死）。
- 动因：空计划无任何引用可校验 ⇒ 能通过 `validatePlan` ⇒ 直接进 SQL 生成，模型可自由编造表名（报错被包装成「服务内部错误」）；此前它既不入重试也不写日志，是观测与重试的**双重旁路**。

## 多轮对话状态（Phase C）

- `session_query_state` 表（JSONB）：`last_question / last_plan / last_sql / last_result_columns / turn_count`，每次成功查询后 UPSERT。
- **意图识别**：13 类意图 `query / new_query / refine / follow_up / clarify / chitchat / define / map / metric / supplier_360 / supplier_risk / graph_reasoning / agent_run`，规则匹配（不调 LLM）；`REFINE`/`FOLLOW_UP` 需上一轮已有状态（`hasPriorState`）。`DEFINE / MAP / METRIC` 已接入流水线（`chat_service._handleDefineClass / _handleDefineMetric / _handleShowMetric / _handleMapProperty`，详见 `chat_service.py:2551-2568`）；`SUPPLIER_360 / SUPPLIER_RISK / GRAPH_REASONING / AGENT_RUN` 是 4 条领域拦截路径，跳过 NL2SQL 走专项服务。
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

## SQL Guard（`infrastructure/business_db_pool.py`）

> 更正（2026-09-26）：本节此前写的位置 `infrastructure/security/sql_guard.py` **不存在**，
> 且「仅允许首 token 为 `SELECT`」「连接级 `statement_timeout`」两条与代码不符。以下是按
> 代码核对后的版本；`architecture.md` / `agent-loop.md` 的同源漂移仍见 §P2-10。

- `sqlparse` 解析；**仅允许首 token 为 `SELECT` / `WITH`**。
- 黑名单（分三类，口径不同，**不可合并**）：
  - 动词：`INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE/GRANT/REVOKE/MERGE/CALL/EXEC/EXECUTE`；
  - 写/文件函数（`_FORBIDDEN_FUNCTIONS`，**裸名 + 引号归一 + 不看形态**）：`nextval/setval/dblink_exec/dblink_send_query/lo_export/lo_import/pg_read_file/pg_write_file/pg_read_binary_file/pg_write_binary_file/pg_ls_dir`；
  - 侧信道函数（`_FORBIDDEN_CALLS`，**须为调用形态**，即名字后紧跟 `(`）：`pg_sleep`/`pg_sleep_for`/`pg_sleep_until`/`pg_advisory_*`/`sleep`/`benchmark`/`get_lock`/`master_pos_wait`/`dblink*`/`httpuritype`/`pg_notify`/`load_file`/`pg_stat_file`/`pg_ls_*dir`/`lo_put`/`lo_create`/`lo_unlink`/`lowrite`/`pg_terminate_backend`/`pg_cancel_backend`/`pg_reload_conf`/`pg_rotate_logfile`；
  - 侧信道包（`_FORBIDDEN_PACKAGES`，**须为包名形态**，即名字后紧跟 `.`）：`utl_http/utl_inaddr/utl_tcp/utl_smtp/utl_mail/utl_file/dbms_lock/dbms_pipe/dbms_lob/dbms_ldap/dbms_scheduler/dbms_network_acl_admin`。
  - 「调用形态 vs 包名形态」必须分开：纯函数名只会以 `(` 调用，若把 `.` 也算命中，`SELECT sleep.col FROM foo sleep`（黑名单词作表别名）会被误杀。
  - 引号包裹的标识符（`"pg_sleep"(5)` / `` `nextval`('s') ``）与名字和括号间夹注释（`pg_sleep /*c*/ (5)`）都是**真实可执行的调用**，故判定前先剥引号、前瞻时跳过注释。
- 拒绝 `INTO`（PG 建表 / MySQL 写文件、变量赋值）与 `SHARE`（PG 行锁 / MySQL `LOCK IN SHARE MODE`）；拒绝多语句（`;`）。
- **行数兜底：`fetchmany(queryRowLimit=5000)` 上限**（实际值见 `QUERY_ROW_LIMIT`），并非 SQL 字符串改写——这一点对 SQL Guard 边界很重要。
- **超时只有客户端**：`asyncio.wait_for(queryTimeoutSeconds)`（默认 30s）；**没有**连接级/服务端 `statement_timeout`，也没有库侧只读账号 —— 即解析层黑名单是当前唯一闸门，属枚举式防御，见提案 `changes/2026-09-26-sql-guard-db-side-readonly-proposal.md`。
- 连接 URL 校验，拒绝 `file://`，可选主机白名单。
- 审计日志：session、datasource、SQL、耗时。
- **拒绝原因回注重试反馈（2026-09-26，M2）**：NL2SQL 重试循环把 `SqlSafetyError` 的原因文本（`str(exc)`，**不含被拒 SQL 本体**）拼进下一轮 `errors` 段，模型据此改写成合法 SQL 而非盲重试；该段与既有 `executionError` 段同做 `_sanitizeContext` 消毒并受 `_ERROR_SNIPPET_LIMIT` 截断。

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
L4: Pure Python async while loop Agent (5 NL2SQL tools, iterative)
  │       │
  │       ▼
  return best effort or "cannot answer"
```

> ⚠️ **L3 已于 2026-09-27 删除**（M5，见 `chat-service-assessment.md` §2.3 / §15）：
> 它作为**独立层**从未被生产调用（`_executeChainedSteps` 零调用者），且含**未计量** LLM 调用。
> 保留下来的是**能力**而非层 —— `prior_cte`（WITH-less CTE 片段）注入路径，见下方
> 「L3 —— 已删除，仅保留 `prior_cte` 能力」。

### Layer Trigger Conditions

| Layer | Trigger | Cost Profile |
|-------|---------|--------------|
| **L1** | `KpiSemanticMatchService.match(question)` Jaccard ≥ threshold | ~0ms, 0 tokens |
| **L2** | L1 miss; default for all other NL2SQL questions | 1× LLM call (plan + SQL) |
| **~~L3~~** | ~~L2 execution fails; question involves multi-step/CTE composition~~ **该层不存在**（2026-09-27 删；见下） | — |
| **L4** | **由意图触发**：`IntentType.AGENT_RUN` → `_handleAgentRun` → `_runL4AgentLoop`（`chat_service.py:842`/`:886`）。⚠️ **不是**「L2/L3 耗尽后自动升级」——生产代码里**没有**从 L2 失败升到 L4 的路径（本行原文有误，2026-09-27 更正）。多步链由 `_executeMultiStep` 承担，与 L4 无关 | N× LLM calls + tool overhead |

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
- ⚠️ **本行原文的「`plan.requiresCte=True` 时注入 prior CTE」是设计文档遗留、代码中不存在**（`requiresCte` 全树零命中）。真实的 `prior_cte` 是一条**显式形参**：`generateSql(..., prior_cte=...)`，契约见下一节。
- **Code**: `app/services/nl2sql_service.py:_planAndGenerateSql`

#### L3 —— 已删除，仅保留 `prior_cte` 能力（2026-09-27）

- **已删除**：`ChatService._executeChainedSteps` / `_executeSingleChainedStep`（原 `chat_service.py:2455-2495` / `:2498-2543`）及其整份测试 `test_l3_chained_steps.py`。删除理由：**零生产调用者**（唯一引用是它自己的测试）+ **含未计量 LLM 调用**（违反核心约束 #3），见 `chat-service-assessment.md` §2.3 M5 / §15。
- **保留的是能力，不是层**：`prior_cte` 形参（`nl2sql_service.py:2104`）接受一段 **WITH-less** 的 CTE 片段
  （形如 `cte1 AS (SELECT …), cte2 AS (SELECT …)`），校验后由 `generateSql` **补上唯一一个**前导 `WITH`
  （`:2195` `f"WITH {prior_cte}\n{sql}"`）；纯函数 `render_prior_cte`（`app/domain/chained_step_plan.py:64`）负责渲染该片段。
- **契约（唯一合法形态，2026-09-27 钉死）**：
  1. **WITH-less** —— 片段**不得自带** `WITH`，否则拼装出 `WITH WITH …`（曾是一条真地雷：`_assert_read_only` 只看首个 token，`WITH` 在白名单 ⇒ **放行**，到库侧才报语法错，再被宽 `except Exception` 吞成「查不出来」）；
  2. 入参经 `_assertPriorCteSafe`（`nl2sql_service.py:488`）校验：**拒绝**自带 `WITH` + 按**拼接后的真实形态**做只读校验
     （不能复用 `_assert_read_only(prior_cte)` —— WITH-less 片段的首个 token 是 CTE 别名，不是 `WITH`）；
  3. 常量 `MSG_PRIOR_CTE_SELF_WITH`（`:479`）给出可操作消息。
- **当前状态（如实标注）**：`prior_cte` 的调用方已随 L3 引擎一并删除 ⇒ **本能力当前无生产调用者**，由契约测试
  `app/tests/unit/test_prior_cte_contract.py` 钉死。保留是**刻意决策**（用户口径：删引擎、保能力）；
  若长期不接线，应连同 `app/domain/chained_step_plan.py` 一并评估删除。
- **Code**: `app/services/nl2sql_service.py`（`_renderPriorCtePart:461`、`_assertPriorCteSafe:488`、`generateSql:2084`）+ `app/domain/chained_step_plan.py`（`render_prior_cte:64`）

#### L4 — Pure Python async while loop Agent

> ⚠️ **更正（2026-09-27）**：本文节原写「LangGraph `StateGraph`」是设计文档遗留，**实际代码是纯 Python async while loop**（`agent_runtime_service.py:601` 注释明示否决 LangGraph）。`AgentState` TypedDict 保留为 LangGraph 升级占位（`agent_state.py`）。

- **触发**：`IntentType.AGENT_RUN`（显式意图），非「L2 失败升级」。
- 纯 Python `while state["iterations"] < max_iterations` 循环；`AgentLoopState` 是 TypedDict（保留为 LangGraph 升级占位）。终止条件 4 类：`answered` / `max_iterations` / `cost_cap` / `error`。
- 5 NL2SQL tools registered: `list_tables`, `describe_table`, `sample_rows`, `execute_sql`, `list_joins`.
- Each iteration: LLM chooses tool → tool executes (`_executePendingToolCalls`) → result fed back → next iteration or final answer.
- Cost cap: `cost_budget_usd=0.5`（默认；调用方覆盖）；`cost_per_1k_input` / `cost_per_1k_output` 由调用方传入（**默认 0 会让成本被静默低估**，见 `agent_runtime_service.py:599` 注释）。loop exits if `total_cost_usd >= cost_budget_usd`.
- Final SQL still passes SQL Guard before execution.
- **Code**: `app/services/agent_runtime_service.py:593`（`run_agent_loop`），由 `chat_service.py:892 _runL4AgentLoop` 调用

### Fallback Chain

```
L1 miss → L2（含同模型瞬态重试，M4）→ 多步链 `_executeMultiStep`（独立路径，写 routing_layer="L2"）
L4 由 `IntentType.AGENT_RUN` 单独触发（无自动升级）
```

任何一层产出「非空且过 SQL Guard」的结果即终止。

⚠️ **`routing_layer` 实际只会写 L1 / L2 / L4 三个值**（`chat_service.py:808` L1、多处 L2、`:1005` L4）——
**从不写 `L3`** ⇒ 监控页的 L3 桶恒为 0（前端仍把 L3 标为「多步链式推理」，属待清理的展示漂移，
见 `chat-service-assessment.md` §2.5）。

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
| Entry | `app/services/chat_service.py:_handleNl2SqlAgent`（`:862`，由 `:817` 调用） |
| L1 | `app/services/kpi_semantic_match_service.py` |
| L2 | `app/services/nl2sql_service.py:_planAndGenerateSql` |
| ~~L3~~ | **已删除**（2026-09-27）；仅保留 `prior_cte` 能力：`app/services/nl2sql_service.py`（`generateSql:2084`、`_assertPriorCteSafe:488`）+ `app/domain/chained_step_plan.py:render_prior_cte:64` |
| L4 | `app/services/agent_runtime_service.py:593`（`run_agent_loop`；**不是在 `app/services/agent_loop.py`，该文件不存在**），由 `chat_service.py:892 _runL4AgentLoop` 调用 |
| Metrics | `app/services/routing_metrics_service.py` |
| Promotion | `app/services/metric_promotion_service.py` |
| Migration | `alembic/versions/0051_add_routing_metrics_fields.py`（**原文写 `migrations/…:0051_add_routing_fields.py`，路径与文件名均已更正**） |

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

## 跨类属性引用校验补可操作 hint（属性归属 + schema 不存在）

`validatePlan`（`app/services/nl2sql_plan.py`）的三个分支（`selectedProperties` / `aggregations` else / `groupBy` 兜底）共用同款可操作重试 hint：`_propertyOwnerHint(prop, propsByClass)`。背景是 2026-09-16 真实回归：用户问「近五个月供货量最大的供应商」时偶发报"选中的属性 供应商名称 不属于选定的任何类"，复现确认是低概率上下文相关 LLM 偏差（上一轮是 PurchaseOrder COUNT → 意图被判为 FOLLOW_UP → statePrompt 注入不相关上轮 → 模型偶发引用跨类属性），旧反馈只说"不属于选定的任何类"无可操作指引，重试两次仍犯同错 → `maxPlanAttempts=2` 耗尽 → 整轮失败。

hint 真源为 `propsByClass`（已经 `_classRefNames` 展开过的业务名 + 别名 + 物理列 + 表限定名 + 类限定名），口径与属性/分组/JOIN 列校验一致：

- 属性在 schema 中**有归属类** → 列出归属类（截断到 `_OWNER_HINT_MAX_CLASSES=3`，避免 schema 类多时提示过长挤占重试 token），引导"把对应类加入 selectedClasses 并按 JOIN 目录关联后再引用"
- 属性在 schema 中**完全不存在**（含别名/物理列口径比对）→ 如实说明防 LLM 重试继续幻觉同一属性名

`groupBy` 分支优先取 `_timeBucketGroupHint`（粒度词命中），命中不到才走 `_propertyOwnerHint`，与粒度词提示保持正交；`aggregations` 的 `formula` 分支保留 2026-08-14 的"property 应填真实属性 / 别名引用写在 formula 内"风格，不重复插入，避免覆盖原有可操作指引。注意：`公式中的属性 X 不属于选定的任何类` 这一支原本**没有** hint，2026-10-01 起语句形态由下节「语句形态 formula 的校验口径」接管。

反例守约：

- `selectedProperties=("供应商名称",)`、`selectedClasses=("ReceiptDetail",)`、`classes=[Receipt, ReceiptDetail, Supplier, PurchaseOrder, PurchaseInvoice]` → 反馈含 `BPSUPPLIER / PurchaseOrder / PurchaseInvoice`，引导把对应类加入 selectedClasses + JOIN 关联（生产回归：用户问「供货量最大供应商」时偶发失败场景）
- `selectedProperties=("NONEXISTENT",)`、`classes` 中无任何类含该属性 → 反馈如实说明含"本体 schema 中不存在"
- `groupBy=("供应商名称",)` 同 selectedProperties 路径，分组属性同样带 hint
- 普通聚合 `SUM("收货数量")`、`groupBy=("订单日期",)` → 不触发 hint

测试守约：`test_query_plan_validation.py` 48 用例（含 3 新增）全过；NL2SQL 单测 161 + chat 集成 102 合计 263 回归全绿；已通过 `deploy_backend.sh` 部署。

详见 `changes/fix-cross-class-property-owner-hint/summary.md`。

## 语句形态 formula 的校验口径（不得逐 token 当属性）

`Aggregation.formula` 的**语句结构豁免**。背景是 2026-10-01 线上回归：用户问「5月份供货量最多的三家供应商所供货物总量占5月份总供货量的比例是多少」，LLM 把**整条 SELECT** 放进 `Aggregation.formula`（该问题要「先取前三家、再算占比」，单条窗口函数表达不了），其 schema 名（`THBI`）、表名（`DWD_GOODS_RECEIPT_DTL` / `DIM_IMATERIAL`）、表别名（`d2` / `m2`）、`ONLY`（来自 `FETCH FIRST 3 ROWS ONLY`）被逐 token 误报成属性幻觉（用户侧 6 条），重试反馈无指向 → 模型原样重犯 → `maxPlanAttempts` 耗尽 → 整轮失败。

`validatePlan`（`app/services/nl2sql_plan.py`）对带 formula 的聚合走**三路口径**：

| 形态 | 判定 | 处理 |
|---|---|---|
| CTE | `isCteFormula`（`^\s*WITH\b`，容许前导空白） | **不做**属性存在性校验（CTE 内部标识符不是本体属性） |
| 语句结构 | `formulaHasSqlStructure`（剥掉字符串字面量后 **`SELECT` 与 `FROM`/`JOIN` 同时**出现） | 报**一条**可操作引导（`_STRUCTURAL_FORMULA_HINT`，置于 issues 首位并去重），不逐 token 报属性 |
| 纯聚合表达式 | 以上皆否 | 逐 token 做属性存在性校验（原逻辑，如期拦真幻觉），并拼 `_propertyOwnerHint` + `_FORMULA_PROPERTY_HINT` 可操作引导 |

**判据刻意不看首词**：线上错误文本只列出被误报的 token，无法区分「整条 `SELECT`」与「表达式里嵌子查询」（如 `SUM(a)/(SELECT SUM(b) FROM t)`）——两者首词不同（`SELECT` vs `SUM`）但都含 `SELECT` + `FROM`，故一个判据覆盖两种形态。

**必须两个条件同时满足**：`EXTRACT(MONTH FROM d)` / `TRIM(BOTH ' ' FROM X)` 里的 `FROM` 是**函数实参分隔符**，不是语句子句。只看 `FROM` 会把这类公式误拒，且提示语内容不实（说它是整条 SQL 语句）—— 而 `EXTRACT` 那条的 token 全是真实属性（只剩 `QTY`/`到货日期`），**改前是能通过校验的**。code review HIGH，已复现并修正。

**为什么是「拒绝 + 引导」而不是豁免**：`Aggregation.formula` **没有确定性渲染器** —— `planToText`/`_aggText`（`app/domain/query_plan.py`）只把它拼成 `"{formula} AS {alias}"` 喂给 SQL 生成 prompt。豁免语句形态会让 SQL 阶段收到 `SELECT ... FETCH FIRST 3 ROWS ONLY AS 占比` 这种畸形聚合行，把早期响亮的失败换成晚期安静的失败。CTE 形态被豁免是因为它至少是**有结构的草稿**。

**误伤边界**：`owned` 只含属性名/别名，不含 schema 名与表名。**含 `SELECT` 的**公式要通过校验，必须其 schema 名、表名、表别名全部恰好等于某个属性名 —— 近乎不可能，故该分支只改变「今天已经在失败」的公式的报错内容。**不含 `SELECT` 的** `EXTRACT`/`TRIM` 类公式落回逐 token 校验，行为与改动前**完全一致**。（初版论证漏掉了后者，被 code review 证伪后修正。）

**提示语必须置首**：`_buildPlanUserPrompt` 把这批 issues 用「；」拼起来后按 `_ERROR_SNIPPET_LIMIT=200` 从**尾部**截断。`_STRUCTURAL_FORMULA_HINT` 占 150 字符，按「追加」顺序会被前面的 issue 挤出预算（实测被砍成 `… ② CTE 形式 WITH a AS (SELECT ...) SELEC`），故实现把它 **insert 到 issues 首位并去重** —— 单独 150 < 200 必然存活，N 条语句公式也只占一份预算。

**纯表达式分支的报错同样必须带方向**（2026-10-01 真机第二轮，也是第一轮修复的覆盖缺口）：该支原先不拼任何可操作提示（同类缺口另有分区属性分支 `分区属性 X 不属于选定的任何类`，属 perGroupLimit 场景，未动）。真机实测：模型面对「Top-N 占比」先写占位符 `SUM(CASE WHEN SUPPLIER_CODE IN (TOP3) THEN RCV_QTY_PUU ELSE 0 END) / SUM(RCV_QTY_PUU)`，收到光秃秃的「公式中的属性 TOP3 不属于选定的任何类」后，把 `TOP3` **就地展开成子查询** —— 第 2 轮输出是第 1 轮的精确回应，表达式结构分毫未动。**模型是照着反馈改的，只是反馈没给它方向。** 故该支现在也拼 `_propertyOwnerHint` + `_FORMULA_PROPERTY_HINT`（96 字符：禁用占位符/子查询 + Top-N 占比用 CTE），引导置 issues 首位并去重。

**触发条件必须收窄到「全 schema 都不存在」的 token**（`p not in allPropNames`）：未知有两种成因，方向相反 —— 跨类引用（真实列，只是不在 selectedClasses 里，如 `SUM(NAME)` 而 NAME 属 `BPSUPPLIER`）该走 `_propertyOwnerHint`「把该类加入 selectedClasses」；占位符/幻觉（全 schema 无此属性，如 `TOP3`）才配得上 Top-N 引导。**给跨类引用叠 Top-N 提示是错误方向，比没方向更糟**（code review MEDIUM，已复现）。这两个分支与 `_propertyOwnerHint` 内部的「有归属 / 不存在」两支同源，口径一致。长度实测：主路径 171 完整；**边界场景会超 `_ERROR_SNIPPET_LIMIT=200`**（单 unknown + 有归属 203、两个 unknown 280），被砍的是排在后面的属性报错行尾部 —— **引导恒完整，这正是置首位的意义**：方向优先于逐条点名。

`_STRUCTURAL_FORMULA_HINT` 的措辞必须涵盖两种形态：真机撞上的是「**表达式里嵌子查询**」，而只说「不能是整条 SQL 语句」会让模型认为与自己无关，引导因此打折 —— 故措辞写明「不能是整条 SQL 语句，**也不得在表达式里嵌子查询**」。

**顺带修掉的既有缺陷**：`isCteFormula` 取代 `formula.strip().upper().startswith("WITH ")`，后者要求 `WITH` 后紧跟**一个空格**，对 `WITH\n` / `WITH\t` 漏判 —— CTE 逃生门本身是脆的。`isCteFormula` 是 SSOT：`parseFormula` 用它路由、`validatePlan` 用它判豁免。

**取证**：`generateValidatedPlan` 在校验失败时记 `logger.warning`，含 attempt + `formatPlanFormulas(plan)`（formula 原文，截断 500 字符）+ issues。此前校验失败**零日志**，`session_message` 也无 detail 列 ⇒ 线上报障时 formula 原文无法回看。

**关键字集**：`ONLY` 补进 `formula_parser._SQL_KEYWORDS` 与 `nl2sql_refs._FORMULA_SQL_KEYWORDS`（与 2026-09-28 补 `ASC/DESC` 同类漏项；纵深防御，非承重修复）。守卫 `TestSqlKeywordSetsStayInSync` 是**单向**子集断言（`_SQL_KEYWORDS ⊆ _FORMULA_SQL_KEYWORDS`），两处同加即保持绿。

反例守约（`test_query_plan_validation.py::TestValidatePlanFormulaShape` / `TestFormulaStructurePredicate`）：

- 整条 `SELECT ... FROM ... FETCH FIRST 3 ROWS ONLY` → 1 条引导（**修前实测 9 条**「公式中的属性 …」）
- `SUM(QTY)/(SELECT SUM(QTY) FROM THBI.DWD_X)` → 同上（首词不是 `SELECT` 也覆盖）
- `SUM(NONEXISTENT)/SUM(SUM(NONEXISTENT)) OVER ()` → 仍报「公式中的属性 NONEXISTENT」（防过度修复）
- `SUM(CASE WHEN EXTRACT(MONTH FROM 到货日期) = 5 THEN QTY ELSE 0 END)/SUM(SUM(QTY)) OVER ()` → **通过**（`FROM` 是函数实参分隔符；code review HIGH 的回归守卫）
- `CASE WHEN BPSNUM = 'FROM' THEN ...` → 字面量里的 `FROM` 不算语句结构
- `(SELECT SUM(x) FROM T)` → 仍判 True（双向）
- CTE 形态 → 仍豁免
- 提示语截断判别器：同时含幻觉属性与语句公式时，`_buildPlanUserPrompt` 输出仍含**完整**提示语（改回「追加」顺序即失败）
- 端到端 `app/tests/integration/test_nl2sql_structural_formula_retry.py`：引导进入第 2 次 prompt，且模型据此改写后通过校验

**已知非目标**：表达式里的限定别名（`SUM(t.QTY)`）以及纯表达式内出现的表名仍会被报 —— `_extractFormulaProperties` 无上下文感知（只做字面量剥离 + 函数名剥离 + 关键字过滤）。本次不动。

详见 `changes/2026-10-01-nl2sql-structural-formula-guard/summary.md`。

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

### 步数硬限（`MAX_PLAN_DATA_STEPS=4`，2026-09-28 A6/M2a）

数据步（不含自动汇总步）超过 **4 步**的计划一律**拒收**，一个数据步都不执行：

| 项 | 值 / 位置 |
|---|---|
| 上限常量 | `app/domain/multi_step_plan.py`：`MAX_PLAN_DATA_STEPS = MAX_MULTI_STEP - 1`。**派生**自 `MAX_MULTI_STEP=5` 而非另写字面量——两个语义不同的 5（含汇总 vs 纯数据步）日后极易被"顺手统一"成一个，从而破坏循环守卫 |
| 守门谓词 | `chat_multistep._isOversizedPlan`：`len(plan.data_steps) > MAX_PLAN_DATA_STEPS`（汇总步必然排在最后，不占额度） |
| 拒收动作 | `chat_multistep._rejectOversizedPlan`——落库 + 存状态，**不做任何执行**；文案 `MSG_PLAN_TOO_MANY_STEPS`（`messages_zh.py`）报出**真实**步数与上限 |
| 两条路径 | 非流式 `_executeMultiStep` 顶部；流式 `_streamMultiStep` 顶部（在读 cache multiplier **之前**，拒收不需要它）。流式事件序列与非流式同型：概览 + 起始 → token → `step_result` 收尾（不收尾前端那张卡永远停在"待执行"） |
| 单步卡片 | `_oversizedStepResult` 是唯一出处，流式/非流式共用。空 `steps` 会让前端不挂载 `MultiStepPlanCard`，固定文案就变成一段没有归属的裸文字 |

**为什么限制放在执行缝而不是 planner**：planner 必须**如实上报**步数，否则拒收文案说不出真实步数——planner 若自行截断，N 恒等于上限值，文案退化成"需要 4 步，超出 4 步上限"这种废话。原先 `_plan_by_llm` 在出口静默 `steps[:MAX]`，用户拿到"12 问里的 4 问"却不自知；system prompt 里"最多拆 4 个子步骤"的自限也一并删除，因为它让 LLM 在 planner 之前就合并/丢弃子问题，使**拒收分支永远不可达**。

**规则快路径同时堵上**：`rule_based_split`（"第X步"标号）此前**完全无上限**，5 个标号即 5 个数据步无条件执行。

**代价（有意为之）**：需要 5+ 步的问题从"拿到 4 步部分答案"变成"被拒收"，用户可见覆盖面下降。`MAX_PLAN_DATA_STEPS` 是一行常量；上线后观察 info 日志 `拆步超限，按上限拒收` 的频次，过高时优先调 prompt 的"只拆彼此独立的子问题"措辞，而不是直接抬上限。

**未做**：A6 原计划第三条「分级落地（§5.3：2–3 步走模板校验）」刻意未做——它是 planner 成本优化，不改步限，待 Planner 有独立需求时另立变更。详见 `changes/2026-09-28-planner-step-limit/summary.md`。

## 多步上下文强注入（实体列表作为筛选条件）

`StepExecutionContext.inject_to_prompt`（**`app/domain/multi_step_plan.py:197`** —— 原文写的 `multi_step_plan.py` / `app/services/multi_step_plan.py` **两处路径都不对**，该模块在 `app/domain/` 下）把前序 `StepResult` 渲染为可注入 plan + sql 两个阶段 prompt 的文本片段。2026-08-17 修复 Bug 4（Step N 引用 Step N-1 实体列表作为 WHERE IN 筛选条件）后，渲染按数据类型自动分档 + plan/sql prompt 共用 WHERE IN 强指令（详见 `changes/fix-multistep-context-strong-injection/summary.md`）。

### 渲染分档（`app/domain/multi_step_plan.py`）

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

**为什么不用"仅作参考"？**——`app/domain/multi_step_plan.py` 的旧措辞"仅作参考数据"是 REFINE/FOLLOW_UP 语境的语义（"上一轮状态只作上下文"）。（原文引的 `multi_step_plan.py:155` 路径与行号**均已失效**；该措辞现已从代码中移除，`app/domain/multi_step_plan.py:209` 的 docstring 记录了这次改法。）**多步语境下语义完全相反**：子问题用"这top10物料"指代前序 ID 时，前序结果数据**必须**作为 WHERE IN 筛选值。强指令文案明确"WHERE IN 必填"。

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
- **方言规则的注入机制（SSOT：`app/services/nl2sql_dialects.py`）**：`SqlDialect` 是 `frozen dataclass`，每个「规则字段」承载一段注入 System Prompt 的规则文本；`nl2sql_prompts.py::_buildSystemPrompt`（服务层包装 `nl2sql_service.py:387`）按 `identifierRule → nullOrderingRule → timeBucketRule → aggregateRule` 顺序追加，**序号从 10 起动态编号**（避免跳号）。新增一条方言规则＝加字段 + 加常量 + 在目标方言实例上赋值，**不必改 prompt 拼装逻辑**。

  | 字段 | 生效方言 | 触发的数据库症状 | 规则要点 |
  |---|---|---|---|
  | `identifierRule` | Oracle | ORA-00923 | 列/表别名不得以数字开头，否则加双引号 |
  | `nullOrderingRule` | Oracle、PostgreSQL | top-N 取到 NULL 行 | `ORDER BY … DESC NULLS LAST` |
  | `timeBucketRule` | 三者各一版 | 按原始时间戳分组 | 月/年/季度截断表达式（方言写法不同） |
  | `aggregateRule` | Oracle | **ORA-00937** | SELECT 列表中聚合函数与标量子查询不得并列；Top-N 占比把分子分母都写成标量子查询、外层 `FROM DUAL` |

  PostgreSQL/MySQL 不注入 Oracle 特有规则（如 `identifierRule`、`aggregateRule`）——两者都允许相应写法，注入只会是噪音。规则「按方言注入、而非全局注入」是本表的成立前提。

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

**降级路径同口径（H5，2026-09-26）**：检索不可用/无命中时走 `_fallbackRecall`
（`mode=fallback`），同样 ODS 过滤 → 层优先排序 → 上限截断，日志 `本体类回退降级
reason=… total=… kept=… odsFiltered=… truncated=…`。此前降级是 `return list(allClasses)`
裸回退，Milvus/embedding 一挂就把 2026-09-19 ODS_BPARTNER 事故（LLM 在贴源备份表上
幻觉属性名）连同「表越多越选错」原样放回来 —— 降级保的是「不报错」，不是「放弃裁剪」。
唯一例外：全库只有 ODS 业务表时保留原列表（空 schema 会让所有问题变成「无法回答」）。

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
