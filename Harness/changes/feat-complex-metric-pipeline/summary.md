# 变更：feat-complex-metric-pipeline

- **日期**：2026-09-10
- **作者**：Claude（基于与用户的方案确认）
- **Phase**：0–5（5 个 Phase + 治理收尾）
- **状态**：draft
- **目标 SSOT**：本文件
- **触发问题**：「采购订单完成率」（AVG of ratios）这类需要两层嵌套聚合的指标无法在当前 NL2SQL 中表达

---

## 1. 需求

### 1.1 业务背景

用户提出业务场景：

> 「**采购订单完成率**」：统计非零库存供应商的采购订单完成率。每个订单行分别计算 `入库合格数量 / 采购数量`，然后对所有订单行的比率取算术平均。统计时间是每月 26 日至次月 26 日。

这是一个**典型「先除后平均（AVG of ratios）」**指标。其数学结构为：

```sql
-- 目标（算术平均 of ratios）
completion_rate = AVG(per_line_ratio)
where per_line_ratio = received_qualified_qty / purchase_qty

-- ≠ 这个（这是加权平均）
completion_rate_alt = SUM(received_qualified_qty) / SUM(purchase_qty)
```

两者数学上不等价（辛普森悖论），**不能互相替代**。

### 1.2 现状（能力盘点）

| 层级 | 现状 | 卡点 |
|---|---|---|
| **NL2SQL 聚合函数枚举** | `SUM / AVG / COUNT / MAX / MIN`（5 个） | LLM 只能从这 5 个选 |
| **派生指标 formula** | 仅支持窗口函数 `SUM(x)/SUM(SUM(x)) OVER ()` | CTE 形式被 `_aliasRequiresFormula` 报错信息硬性拒绝 |
| **KpiCatalog 在 NL2SQL 流水线中的角色** | **完全不存在** | chat_service 中无任何 KpiCatalog 查询 |
| **Feature 层** | 已有 5 条 Feature（含 AVG of 已聚合 ratio 的先例） | PO 完成率未登记 |
| **多步执行** | `_executeMultiStep` 线性执行 | 无 DAG、中间结果靠 prompt 注入 |
| **Agent Loop** | 无 tool calling、无循环 | BaseLlmClient 仅支持单次 complete |
| **SQL Guard** | `_READ_ONLY_VERBS = {"SELECT", "WITH"}` | CTE 形式**已合法**，无需改 Guard |

### 1.3 目标

实现一个**4 层路由架构**，让复杂指标（特别是 AVG of ratios）按以下优先级处理：

```
L1 语义匹配（KpiCatalog/Feature 已注册）   ← 零 LLM 调用，确定性
        ↓ miss
L2 LLM 直接生成 CTE 单条 SQL              ← 1 次 LLM，Prompt 引导
        ↓ 复杂多步 / Agent 模式触发
L3 多 Plan DAG 编排（CTE 串联）           ← N 次执行，依赖追踪
        ↓ 探索/异常兜底
L4 Agent Loop（LangGraph + 5 个工具）      ← 自适应循环
```

并附带：
- **冷指标晋升 L1 机制**：L2/L3 频繁命中的指标自动提议固化为 KPI
- **全程 Token/Cost 计量**：每层路由的代价独立记录（`purpose` 字段）
- **完整测试覆盖**：单测 + 集成测试 + 真实 DB 冒烟，覆盖率 ≥ 80%

### 1.4 验收标准

#### 功能验收

| Phase | 验收 |
|---|---|
| **Phase 0** | PO 完成率作为 FeatureDefinition 注册成功，FeatureComputeService 计算并 upsert 到 feature_value，Supplier 360 视图能展示该指标 |
| **Phase 1** | 5 条高频 KPI（OTD/Defect Rate/Price Variance/Risk Score/PO Completion）能在 0 LLM 调用下被 L1 命中并返回结果 |
| **Phase 2** | 用户问"完成率（算术平均）"时，LLM 能生成带 WITH 子句的 SQL 通过 validatePlan，并通过 SQL Guard 执行成功 |
| **Phase 3** | 用户问"先按月聚合再算环比"等多步问题时，引擎能编排子 SQL 串联执行 |
| **Phase 4** | Agent Loop 能自主调用 list_tables / describe_table / sample_rows / execute_sql / list_joins 5 个工具，自主完成"未见过"的探索性问题 |
| **Phase 5** | 监控面板能看到 L1/L2/L3/L4 命中率与 token 成本；冷指标晋升流程跑通 |

#### 质量验收

- 单测覆盖率 ≥ 80%（TDD 强制）
- 集成测试用真实 PostgreSQL（参考 `Harness/rules/测试规范.md`，禁止 sqlite 内存库）
- 每次 commit 后 `code-reviewer` + `security-reviewer` 通过
- 无 CRITICAL/HIGH 安全问题

#### 性能验收

| 指标 | 目标 |
|---|---|
| L1 命中延迟 | < 50ms（纯 DB + 内存匹配） |
| L2 命中延迟 | < 3s（1 次 LLM 调用） |
| L3 命中延迟 | < 10s（3 步以内） |
| L4 命中延迟 | < 30s（5 个 iteration 以内） |

---

## 2. 设计评审

### 2.1 方案选择

| 候选方案 | 选择 | 理由 |
|---|---|---|
| **L1 入口位置** | ✅ `processMessage` 第 402 行（意图分类前） | 0 LLM 开销，命中即返回，最清晰 |
| ❌ `_planAndGenerateSql` 之前 | 已有部分上下文构建开销 | |
| **L1 索引表** | ✅ **扩展 `kpi_catalog` 加 `semantic_keywords`** | 不新建表，复用现有 12 条种子 |
| ❌ 新建 `semantic_metric` | 增加迁移成本 | |
| **L2 formula 字段** | ✅ **复用现有 `formula: str` 字段承载 CTE** | 无 schema 迁移 |
| ❌ 新增 `cteExpression` | 重复 | |
| **L3 实现复杂度** | ✅ **Phase 3 先做 CTE 串联（不落表），Phase 5 后视情况上 DAG** | 投入产出比更高 |
| ❌ 直接做完整 DAG | 第一版工作量大 | |
| **L4 框架** | ✅ **LangGraph** | 状态机 + 工具调用成熟，避免自实现循环 bug |
| ❌ 自实现 ReAct 循环 | 容易出错 | |

### 2.2 多视角意见

#### 安全性视角
- L4 Agent Loop 必须有**工具白名单 + 调用次数上限 + sandbox 隔离**
- 所有 LLM 生成的 SQL 必须经 SQL Guard（已有 `_assert_read_only`）
- 新增 `execute_sql` 工具要复用现有连接池，禁止 raw SQL 透传

#### 一致性视角
- 4 层路由共用 `purpose` 字段：`l1_match` / `l2_cte` / `l3_dag` / `l4_agent`
- 统一错误处理：失败时降级到下一层
- 统一返回结构：`RoutingResult(layer, sql, data, token_used, cost, latency_ms)`

#### 可维护性视角
- 每层独立 Service，独立单元测试
- Prompt 模板集中到 `Harness/skills/nl2sql-prompt/SKILL.md`
- 索引脚本独立（`seed_kpi_semantic_index.py`）

#### 性能视角
- L1 匹配缓存（启动时加载到内存，变更时失效）
- L3 DAG 中间结果用 prompt 注入，**不落表**
- L4 工具结果截断（`_DATA_SAMPLE_LIMIT=20`）

### 2.3 最终决定

按 5 个 Phase 推进，每个 Phase 独立可发布：

```
Phase 0: PO 完成率 Feature 落地（基础，证明 SQL 可行）
Phase 1: L1 语义匹配（最大复用价值）
Phase 2: L2 LLM CTE（消除 LLM 退化）
Phase 3: L3 CTE 串联多步（轻量 DAG 替代）
Phase 4: L4 Agent Loop（兜底能力）
Phase 5: 治理收尾（晋升机制 + 监控 + 文档）
```

---

## 3. 数据模型变更

### 3.1 Phase 1：扩展 `kpi_catalog`

**Alembic migration**（新建 `alembic/versions/xxxx_add_semantic_keywords_to_kpi_catalog.py`）：

```python
def upgrade() -> None:
    op.add_column(
        "kpi_catalog",
        sa.Column("semantic_keywords", sa.ARRAY(sa.String(64)), nullable=True),
    )
    op.add_column(
        "kpi_catalog",
        sa.Column("match_threshold", sa.Numeric(3, 2), nullable=False, server_default="0.75"),
    )
    # GIN 索引加速 array contains 查询
    op.create_index(
        "ix_kpi_catalog_semantic_keywords",
        "kpi_catalog",
        ["semantic_keywords"],
        postgresql_using="gin",
    )
```

**Model 变更**（`backend/app/domain/models.py:343-389`）：

```python
class KpiCatalog(Base, TimestampMixin):
    # ... 已有字段 ...
    semantic_keywords: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(64)), nullable=True
    )
    match_threshold: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, default=Decimal("0.75")
    )
```

**种子脚本**（新建 `backend/scripts/seed_kpi_semantic_index.py`）：

```python
# 为 12 条 KPI 填充 semantic_keywords
KPI_KEYWORDS = {
    "KPI_SUPPLIER_OTD": ["准时交付", "OTD", "on-time", "按时", "准时率"],
    "KPI_SUPPLIER_DEFECT_RATE": ["来料不良", "不良率", "defect", "不合格率"],
    "KPI_PURCHASE_PRICE_VARIANCE": ["价格偏差", "price variance", "价差"],
    # ... 共 12 条
}
```

### 3.2 Phase 3：可能新增 `plan_dag_node` 表（**可选**）

**仅在 Phase 3 选择 DAG 方案时使用**，CTE 串联方案不需要：

```python
class PlanDagNode(Base):
    __tablename__ = "plan_dag_node"
    id: Mapped[int]
    plan_id: Mapped[str]                  # UUID 标识一次完整 DAG 执行
    node_id: Mapped[str]                  # "step_0", "step_1a"
    depends_on: Mapped[list[str]]         # ARRAY<String>
    sql_text: Mapped[str]
    status: Mapped[str]                   # pending / running / done / failed
    result_json: Mapped[dict | None]
    latency_ms: Mapped[int | None]
```

> **MVP 不上**：优先用 prompt 注入中间结果。

### 3.3 Phase 4：可能新增 `agent_loop_log` 表（**可选**）

```python
class AgentLoopLog(Base):
    __tablename__ = "agent_loop_log"
    id: Mapped[int]
    session_id: Mapped[str]
    iteration: Mapped[int]
    tool_name: Mapped[str | None]
    tool_input: Mapped[dict | None]
    llm_response: Mapped[str | None]
    total_tokens: Mapped[int]
    cost_usd: Mapped[Decimal]
    created_at: Mapped[datetime]
```

> **MVP 不上**：先用结构化日志。

---

## 4. 接口契约变更

### 4.1 Phase 1：KPI 搜索端点

**新建端点**（`backend/app/api/v1/kpi_catalog.py`）：

```python
@router.get("/kpi-catalog/search")
async def search_kpis(q: str = Query(..., min_length=2)) -> list[KpiCatalogSearchResult]:
    """按问题文本模糊搜索匹配的 KPI。"""
```

**请求**：`GET /api/v1/kpi-catalog/search?q=完成率`
**响应**：
```json
[
  {
    "kpi_code": "KPI_SUPPLIER_PO_COMPLETION_RATE",
    "kpi_name": "采购订单完成率",
    "matched_keyword": "完成率",
    "match_score": 0.95,
    "formula": "WITH ..."
  }
]
```

### 4.2 Phase 2：Chat 响应扩展

**`ChatResponse`** 新增字段：

```python
class ChatResponse(BaseModel):
    # ... 已有字段 ...
    routing_layer: Literal["l1_match", "l2_cte", "l3_dag", "l4_agent", "default"] | None
    routing_confidence: float | None
    l1_match_code: str | None  # 命中的 KPI code
```

### 4.3 Phase 3：新端点（可选）

```python
POST /api/v1/chat/execute-dag
Body: { plan_id: str, sub_question: str }
Response: { step_index, sql, data, summary }
```

> 仅在 Phase 3 选择前端可见 DAG 时暴露；MVP 不暴露。

### 4.4 Phase 4：Agent 端点

```python
POST /api/v1/agent/run
Body: { session_id, question, max_iterations: int = 5 }
Response: { final_sql, iterations, total_tokens, tool_calls: list[ToolCall] }
```

---

## 5. 实现要点

> **每 Phase 的 TDD 任务列表见 §6。本节仅写关键文件 / 算法 / 依赖。**

### Phase 0：PO 完成率 Feature 落地（半天）

**目标**：把"PO 完成率"作为 FeatureDefinition 登记并跑通端到端。

**关键文件**：
- 新建：`backend/scripts/seed_features.py`（追加）— `SUPPLIER_PO_COMPLETION_RATE` FeatureDefinition
- 新建：`backend/tests/integration/test_supplier_po_completion_rate.py`

**核心 SQL**（写入 `calculation_logic`）：
```sql
SELECT p.supplier_code AS entity_key,
  AVG(
    (NVL(g.received_qty, 0) - NVL(g.rejected_qty, 0) - NVL(g.returned_qty, 0))
    / NULLIF(p.order_qty, 0)
  ) AS value
FROM THBI.DWD_PURCHASE_ORDER_LINE p
LEFT JOIN THBI.DWD_GOODS_RECEIPT_LINE g
  ON g.po_no = p.po_no AND g.po_line_no = p.po_line_no
JOIN THBI.DIM_SUPPLIER s ON s.supplier_code = p.supplier_code
WHERE s.zero_stock_flag = 1  -- 非零库存供应商（注意：1=非零库存，2=零库存）
  AND p.order_date BETWEEN TO_DATE(:start_date, 'YYYY-MM-DD')
                       AND TO_DATE(:end_date,   'YYYY-MM-DD')
GROUP BY p.supplier_code
```

**关键决策**：
- **不放入 Phase 0 完成率本体逻辑**的卡点：
  - **26 日到次月 26 日**是滚动窗口还是自然月？需要业务确认
  - **「合格数量」定义**：直接 `received - rejected - returned` 还是只算质检合格部分？
  - **多 GR 行合并**：一张 PO 行可能多次收货，SQL 已用 LEFT JOIN + SUM 合并
- **风险点**：THBI 是 Oracle 源库，SQL 用 `NVL` / `TO_DATE` Oracle 语法；如未来切 PG 需适配

**冒烟测试**：调用 `FeatureComputeService.computeFeature()`，验证 feature_value 表写入。

### Phase 1：L1 语义匹配（1-2 天）

**目标**：用户问题命中 KpiCatalog/Feature 时，**0 LLM 调用**直接返回结果。

**关键文件**：
- 新建：`backend/app/services/kpi_semantic_match_service.py`（核心匹配服务）
- 新建：`backend/app/services/kpi_match_cache.py`（启动预热 + 写时失效缓存）
- 新建：`backend/app/schemas/kpi_match.py`（DTO）
- 修改：`backend/app/services/chat_service.py`（`processMessage` 第 402 行插入）
- 修改：`backend/app/services/kpi_catalog_service.py`（新增 `searchKpiCatalog()`）
- 修改：`backend/app/api/v1/kpi_catalog.py`（新增 `/search` 端点）
- 新建：`backend/scripts/seed_kpi_semantic_index.py`
- 新建：`backend/tests/unit/test_kpi_semantic_match_service.py`
- 新建：`backend/tests/integration/test_chat_l1_routing.py`

**核心算法**：

```python
# kpi_semantic_match_service.py
class KpiSemanticMatchService:
    def __init__(self, cache: KpiMatchCache):
        self._cache = cache
    
    async def match(self, question: str) -> KpiMatchResult | None:
        # 1. 精确 alias 匹配（FEATURE_NAME_RE 同款正则）
        code = self._matchExactAlias(question)
        if code:
            return KpiMatchResult(code=code, confidence=1.0, layer="l1_match")
        
        # 2. 中英文关键词匹配（semantic_keywords 数组 contains）
        keywords = self._extractKeywords(question)
        candidates = self._cache.findByAnyKeyword(keywords)
        if not candidates:
            return None
        
        # 3. 简单评分：Jaccard 相似度
        scored = [
            (kpi, jaccard(keywords, kpi.semantic_keywords))
            for kpi in candidates
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[0]
        if top[1] >= self._threshold:
            return KpiMatchResult(
                code=top[0].kpi_code, 
                confidence=top[1], 
                layer="l1_match"
            )
        return None
```

**插入点**（`chat_service.py` 第 402 行）：

```python
async def processMessage(self, dto: ChatRequest) -> ChatResponse:
    # ... 已有准备 ...
    
    # ★ Phase 1 插入：L1 KPI 语义匹配
    l1_match = await self._kpiMatch.match(dto.message)
    if l1_match:
        return await self._buildL1Response(dto, l1_match)
    
    # ... 原意图分类流程 ...
```

**复用现有组件**：
- `FEATURE_NAME_RE` 正则（`feature_query_service.py:40`）— 大写下划线匹配
- `_tryFeatureResponse` 拦截模式（`chat_service.py:2460`）— 跳过 SQL 生成
- `TokenUsageService.recordUsage` — 记录 `purpose="l1_match"`（零 LLM token）

**缓存策略**：
- 启动时从 DB 加载所有 `KpiCatalog` 到内存（仅 12 条种子）
- KpiCatalog CRUD 时通过事件总线失效缓存
- Milvus embedding 索引**Phase 5 后视情况接入**（当前关键词足够）

### Phase 2：L2 LLM 直接生成 CTE（1-2 天）

**目标**：用户问"完成率（算术平均）"时，LLM 能生成带 WITH 子句的 SQL 并通过校验。

**关键文件**：
- 修改：`backend/app/services/nl2sql_service.py`
  - L1871-1878（Plan Prompt 规则 4）：允许 CTE 形式
  - L1499-1505（`validatePlan` 报错）：报错信息允许 CTE
  - L1978（SQL Prompt 规则 8）：允许 CTE 形式
- 修改：`backend/app/services/formula_parser.py`
  - `_AGGREGATE_FUNCTIONS` 扩展（含 STDDEV, VARIANCE）
  - `parseFormula` 支持 WITH 子句解析
- 修改：`Harness/skills/nl2sql-prompt/SKILL.md`（文档同步）
- 新建测试：`backend/tests/unit/test_formula_parser_cte.py`
- 新建测试：`backend/tests/unit/test_nl2sql_validate_plan_cte.py`
- 新建测试：`backend/tests/integration/test_l2_cte_avg_of_ratios.py`

**Prompt 改写**（关键片段）：

```diff
# nl2sql_service.py L1871-1878
 4. aggregations 的 formula 字段**按需填写**——仅当问题需要派生指标
    （占比/比率/百分比/比例/ratio/percent/share/pct）且无法单用 function(property)
    表达时才填写；**问题含以上关键词时 formula 必填**，否则验证会被拒。
+   formula 支持两种形式：
+     (a) 窗口函数形式，如 SUM(x)/SUM(SUM(x)) OVER ()；
+     (b) CTE 形式，如
+         WITH tmp AS (SELECT 供应商, SUM(QTY) AS s FROM t GROUP BY 供应商)
+         SELECT 供应商, s/SUM(s) OVER () AS 占比 FROM tmp
+   当问题语义为「算术平均 of ratios」（关键词：完成率、合格率、及时率、一次通过率等）
+   时，**必须使用 CTE 形式**，禁止退化为 SUM(x)/SUM(y)（数学上不等）。
```

**validatePlan 放宽**：

```diff
# nl2sql_service.py L1499-1505
 if _aliasRequiresFormula(agg.alias) and not agg.formula:
     issues.append(
-        f"聚合别名 {agg.alias} 是派生指标（占比/比率/百分比/比例/ratio/percent/share/pct），"
-        f"必须使用 formula 表达式（窗口函数 SUM(x)/SUM(SUM(x)) OVER ()），"
+        f"聚合别名 {agg.alias} 是派生指标（占比/比率/百分比/比例/ratio/percent/share/pct），"
+        f"必须使用 formula 表达式，支持 (a) 窗口函数 SUM(x)/SUM(SUM(x)) OVER ()"
+        f"或 (b) CTE 形式 WITH tmp AS (...) SELECT ... FROM tmp，"
```

**formula_parser CTE 支持**：

```diff
# formula_parser.py L33-35
-_AGGREGATE_FUNCTIONS: frozenset[str] = frozenset(
-    {"SUM", "AVG", "COUNT", "MAX", "MIN"}
-)
+_AGGREGATE_FUNCTIONS: frozenset[str] = frozenset({
+    "SUM", "AVG", "COUNT", "MAX", "MIN", "STDDEV", "VARIANCE"
+})
+
+# 新增 CTE 模式识别
+_CTE_PATTERN = re.compile(
+    r"\s*WITH\s+\w+\s+AS\s*\(.*?\)\s+SELECT",
+    re.IGNORECASE | re.DOTALL,
+)

 def parseFormula(formula: str) -> ParsedFormula:
+    if _CTE_PATTERN.match(formula):
+        return _parseCteFormula(formula)
     # ... 原有逻辑 ...
```

**关键决策**：
- **不引入新的 QueryPlan 字段**：formula 字符串本身已可承载 CTE
- **不修改 SQL Guard**：`_READ_ONLY_VERBS` 已含 WITH
- **保留 `_aliasRequiresFormula` 行为**：仅放宽报错信息和允许形式

### Phase 3：L3 多 Plan CTE 串联（2-3 天）

**目标**：用户问"先 A 再 B"复合问题时，引擎拆解为子 SQL 用 CTE 串联。

**关键决策**：**Phase 3 先做 CTE 串联（不落表），完整 DAG 推到 Phase 5 之后视情况做**。

**关键文件**：
- 修改：`backend/app/services/step_query_planner.py`
  - LLM 拆步输出从「线性 plan」改为「CTE-aware plan」
- 修改：`backend/app/services/nl2sql_service.py`
  - `generateSql` 新增 `prior_cte: str | None` 参数
  - 渲染最终 SQL 时拼接 `WITH prior_cte SELECT ... FROM current_subquery`
- 修改：`backend/app/services/chat_service.py`
  - `_executeMultiStep` 改为 `_executeChainedSteps`
- 新建：`backend/app/domain/chained_step_plan.py`
- 新建测试：`backend/tests/unit/test_step_query_planner_cte.py`
- 新建测试：`backend/tests/integration/test_l3_chained_steps.py`

**核心数据结构**：

```python
# chained_step_plan.py
@dataclass(frozen=True)
class ChainedStep:
    step_index: int
    sub_question: str
    sql: str | None = None                  # 执行后填充
    result: StepResult | None = None
    error: str | None = None

@dataclass(frozen=True)
class ChainedPlan:
    steps: tuple[ChainedStep, ...]
    final_sql: str | None = None           # 拼装后的完整 SQL
```

**执行引擎**：

```python
# chat_service.py 改造片段
async def _executeChainedSteps(
    self, plan: ChainedPlan, datasource_id: int
) -> list[StepResult]:
    results = []
    prior_cte_parts = []
    
    for step in plan.steps:
        # 1. 把前序结果渲染成 CTE
        prior_cte = _renderPriorCte(prior_cte_parts)
        
        # 2. 生成该步 SQL（注入 prior_cte）
        sql_result = await self._nl2sql.generateSql(
            step.sub_question,
            prior_cte=prior_cte,
        )
        
        # 3. 执行
        result = await self._business_pool.execute_read_only(
            datasource_id, sql_result.sql
        )
        results.append(StepResult(step=step, sql=sql_result.sql, data=result))
        
        # 4. 供下一步使用
        prior_cte_parts.append(_buildCteFromResult(step, result))
    
    return results
```

**关键约束**：
- 不支持并行（避免并发问题，Phase 3 简化）
- 每步失败隔离（不阻断后续）
- 上限 5 步（避免无限拆分）

### Phase 4：L4 Agent Loop（1 周）

**目标**：用 LangGraph 实现 Agent Loop，LLM 自主探索 schema、试错、修复。

**关键文件**：
- 新建：`backend/app/services/agent_tools_nl2sql.py`（5 个工具）
- 修改：`backend/app/infrastructure/llm/base_client.py`（新增 `complete_with_tools()`）
- 修改：`backend/app/infrastructure/llm/openai_client.py`（实现 tool calling）
- 修改：`backend/app/services/agent_runtime_service.py`（新增 `run_agent_loop()`）
- 修改：`backend/app/services/chat_service.py`（新增 `_handleNl2SqlAgent()`）
- 新建：`backend/app/services/agent_state.py`（LangGraph 状态定义）
- 修改：`backend/pyproject.toml`（新增 `langgraph>=0.2.0`）
- 新建测试：`backend/tests/unit/test_agent_tools_nl2sql.py`
- 新建测试：`backend/tests/integration/test_l4_agent_loop.py`

**5 个工具**：

```python
TOOLS = [
    AgentTool("list_tables", "列出可用表名", handler=list_tables_handler),
    AgentTool("describe_table", "查看表的列结构", handler=describe_table_handler),
    AgentTool("sample_rows", "采样前 N 行", handler=sample_rows_handler),
    AgentTool("execute_sql", "执行只读 SQL", handler=execute_sql_handler),
    AgentTool("list_joins", "查看表关联关系", handler=list_joins_handler),
]
```

**LangGraph StateGraph**：

```python
from langgraph.graph import StateGraph

class AgentState(TypedDict):
    messages: list[BaseMessage]
    iterations: int
    tool_calls: list[ToolCall]
    final_sql: str | None

graph = StateGraph(AgentState)
graph.add_node("agent", agent_node)        # LLM 决策
graph.add_node("tools", tool_executor)     # 工具执行
graph.add_conditional_edges("agent", should_continue)
graph.add_edge("tools", "agent")
```

**安全约束**：
- `max_iterations=5`（防止无限循环）
- 所有 `execute_sql` 必须经 `_assert_read_only`
- 总 token 上限 `cost_usd <= 0.5`（超出强制结束）

### Phase 5：治理收尾（2-3 天）

**目标**：监控、文档、冷指标晋升。

**关键文件**：
- 新建：`backend/app/services/metric_promotion_service.py`
- 新建：`backend/app/services/routing_metrics_service.py`
- 新建：`frontend/src/pages/RoutingMetricsPage.tsx`
- 新建：`backend/scripts/promote_metrics.py`
- 修改：`Harness/wiki/nl2sql-engine.md`（新增"4 层路由"章节）
- 修改：`Harness/changes/feat-complex-metric-pipeline/`（完整收尾）

**冷指标晋升机制**：

```python
# metric_promotion_service.py
class MetricPromotionService:
    async def scanPromotionCandidates(self) -> list[PromotionCandidate]:
        """扫描最近 30 天：
        - L2 命中 ≥ 3 次
        - SQL 模式稳定（hash 一致）
        → 提议晋升为 KpiCatalog"""
        ...
    
    async def autoPromote(self, candidate: PromotionCandidate) -> None:
        """管理员审核通过后写入 KpiCatalog.formula"""
        ...
```

**监控面板**：

```python
# routing_metrics_service.py
class RoutingMetricsService:
    async def getLayerDistribution(self, since: datetime) -> dict:
        """返回各层命中率、平均 token、平均延迟"""
        return {
            "l1_match": {"count": 234, "hit_rate": 0.45, "avg_tokens": 0},
            "l2_cte":   {"count": 156, "hit_rate": 0.30, "avg_tokens": 1200},
            "l3_dag":   {"count": 23,  "hit_rate": 0.04, "avg_tokens": 3400},
            "l4_agent": {"count": 12,  "hit_rate": 0.02, "avg_tokens": 8500},
            "default":  {"count": 99,  "hit_rate": 0.19, "avg_tokens": 1500},
        }
```

---

## 6. 测试

### 6.1 测试策略

| 测试类型 | 工具 | 覆盖目标 |
|---|---|---|
| **单元测试** | pytest + pytest-asyncio | 80% 覆盖（强制） |
| **集成测试** | 真实 PostgreSQL（`qa_metadata_test`）+ THBI 源库 | 端到端 SQL 执行 |
| **E2E 测试** | Playwright（已有） | Chat 路径冒烟 |

### 6.2 各 Phase 测试用例

#### Phase 0 测试
- `test_seed_features_po_completion_rate`：seed 幂等写入
- `test_compute_feature_po_completion_rate`：FeatureComputeService 计算正确
- `test_supplier_360_includes_po_completion_rate`：Supplier 360 视图加载新 Feature

#### Phase 1 测试
- `test_match_exact_alias`：大写下划线精确匹配
- `test_match_keyword_overlap`：关键词 Jaccard 匹配
- `test_match_below_threshold_returns_none`：低分匹配拒绝
- `test_l1_match_skips_llm`：L1 命中时 token=0
- `test_l1_match_returns_chat_response`：完整 ChatResponse 结构
- `test_kpi_cache_invalidates_on_update`：KpiCatalog CRUD 时缓存失效
- `test_search_endpoint`：GET /search?q=... 路由

#### Phase 2 测试
- `test_formula_parser_cte_recognized`：CTE formula 解析
- `test_formula_parser_stddev_variance`：新聚合函数识别
- `test_validate_plan_cte_formula_passes`：CTE formula 通过 validatePlan
- `test_validate_plan_cte_alias_keyword`：占比 + CTE 组合
- `test_l2_cte_avg_of_ratios_e2e`：端到端 PO 完成率查询
- `test_l2_prompt_no_window_function_only`：prompt 解除窗口函数限制

#### Phase 3 测试
- `test_chained_step_cte_injection`：prior_cte 正确注入下一步
- `test_chained_step_isolation`：单步失败不阻断后续
- `test_chained_step_max_5`：超过 5 步拒绝
- `test_l3_chained_steps_e2e`：端到端多步问题

#### Phase 4 测试
- `test_agent_tool_list_tables`：工具调用
- `test_agent_tool_describe_table`：表结构返回
- `test_agent_tool_execute_sql_guard`：危险 SQL 被拦截
- `test_agent_loop_max_iterations`：超过 5 次强制结束
- `test_agent_loop_cost_cap`：超过 $0.5 强制结束
- `test_l4_agent_loop_e2e`：端到端探索性问题

#### Phase 5 测试
- `test_promotion_scan_finds_repeated_l2`：30 天内 ≥ 3 次命中
- `test_promotion_auto_register`：写入 KpiCatalog
- `test_routing_metrics_distribution`：各层分布统计

### 6.3 覆盖率门槛

- 单测覆盖率 ≥ 80%（CLAUDE.md 全局约束）
- 每 Phase 提交前必须跑 `pytest --cov=app/services/complex_metric_pipeline --cov-fail-under=80`
- 集成测试必须 100% 通过（真实 PostgreSQL）

---

## 7. 安全审查

### 7.1 安全风险点

| 风险 | Phase | 缓解措施 |
|---|---|---|
| L4 Agent 工具被滥用执行写操作 | 4 | 所有 execute_sql 强制经过 `_assert_read_only`；工具 handler 内置 guard |
| L4 Agent 无限循环刷成本 | 4 | max_iterations=5 + cost_usd_cap=$0.5 + timeout=30s |
| L2 prompt 注入导致 LLM 生成危险 SQL | 2 | SQL Guard 二次校验；prompt 隔离（user input 用 `\`\`\` `包裹） |
| KpiCatalog metric_id 悬空 | 1 | semantic_keywords 可空，但 match_threshold 不可空；写时校验 |
| Feature calculation_logic 注入 | 0 | 现有 `_assert_read_only` 已覆盖；新增 feature 必须 ACTIVE 状态 |

### 7.2 必查项（参考 `acl-security-review-pattern.md`）

- [ ] LLM 输出 mass-assignment 风险（Agent Tool 入参）
- [ ] 403 侧信道（KpiCatalog 权限）
- [ ] actor 派生（Agent Loop 调用方身份）
- [ ] 非 admin 集成测试（普通用户访问限制）

### 7.3 必触发 agent

每 Phase 完成时调用：
- `code-reviewer`：常规代码质量
- `security-reviewer`：L4 必须，其它可选

---

## 8. 部署验证

### 8.1 部署顺序

按 Phase 顺序部署，**每个 Phase 独立 feature flag**：

| Phase | Feature Flag | 默认值 |
|---|---|---|
| Phase 0 | 无（纯数据添加） | ON |
| Phase 1 | `ENABLE_L1_KPI_MATCH` | OFF → ON |
| Phase 2 | `ENABLE_L2_CTE_FORMULA` | OFF → ON |
| Phase 3 | `ENABLE_L3_CHAINED_STEPS` | OFF → ON |
| Phase 4 | `ENABLE_L4_AGENT_LOOP` | OFF → ON |
| Phase 5 | `ENABLE_AUTO_PROMOTION` | OFF → ON |

### 8.2 冒烟测试

每 Phase 上线后必须跑：

```bash
# Phase 0
docker exec qa-backend python -m scripts.seed_features
curl http://localhost:8000/api/v1/supplier-360/.../kpis | jq '.kpis[].name' | grep "采购订单完成率"

# Phase 1
curl -X POST http://localhost:8000/api/v1/chat -d '{"message":"供应商准时交付率"}' | jq '.routing_layer'  # 期望 "l1_match"

# Phase 2
curl -X POST http://localhost:8000/api/v1/chat -d '{"message":"按供应商算订单完成率"}' | jq '.sql' | grep -i "WITH.*AS.*SELECT"

# Phase 3
curl -X POST http://localhost:8000/api/v1/chat -d '{"message":"先按月聚合再算环比"}' | jq '.routing_layer'  # 期望 "l3_dag"

# Phase 4
curl -X POST http://localhost:8000/api/v1/agent/run -d '{"session_id":"test","question":"THBI 里有什么表？"}' | jq '.tool_calls[].tool_name'

# Phase 5
curl http://localhost:8000/api/v1/admin/routing-metrics | jq '.layer_distribution'
```

### 8.3 回滚预案

| Phase | 回滚命令 |
|---|---|
| Phase 0 | `docker exec qa-postgres psql -d qa_metadata -c "DELETE FROM feature_definition WHERE feature_name='SUPPLIER_PO_COMPLETION_RATE'"` |
| Phase 1 | `UPDATE system_config SET value='false' WHERE key='ENABLE_L1_KPI_MATCH'` |
| Phase 2 | `UPDATE system_config SET value='false' WHERE key='ENABLE_L2_CTE_FORMULA'` + git revert |
| Phase 3 | `UPDATE system_config SET value='false' WHERE key='ENABLE_L3_CHAINED_STEPS'` |
| Phase 4 | `UPDATE system_config SET value='false' WHERE key='ENABLE_L4_AGENT_LOOP'` |
| Phase 5 | `UPDATE system_config SET value='false' WHERE key='ENABLE_AUTO_PROMOTION'` |

---

## 9. 关联

### 设计文档

- 本文件 SSOT
- 关联：`Harness/changes/feat-local-import-evolution/summary.md`（Feature 注册模式参考）
- 关联：`Harness/changes/feat-feature-rule-config/summary.md`（Feature 模型参考）
- 关联：`Harness/changes/feat-agent-runtime-mvp/summary.md`（L4 Agent Runtime 复用）

### Wiki

- `Harness/wiki/nl2sql-engine.md`：新增「4 层路由架构」章节
- `Harness/wiki/metric-pipeline.md`（新建）：4 层详解
- `Harness/wiki/agent-loop.md`（新建）：L4 详解

### 规则

- `Harness/rules/测试规范.md`：真实 PostgreSQL 强制
- `Harness/rules/数据库环境使用规范.md`：两库策略
- `Harness/rules/开发流程规范.md`：TDD + 80% 覆盖率
- `Harness/rules/AI治理.md`：prompt 工程 + token 计量

### 探查报告（输入材料）

- L1 探查：`L1 KPI semantic match architecture`
- L2 探查：`L2 LLM CTE support architecture`
- L3/L4 探查：`L3 L4 plan DAG and agent loop`
- 早期探查：`Find aggregation operator support` / `PO completion rate ADS feasibility`

### 相关 PR / commit

- `b12fba2 docs(i18n): DQ adoptNotApplicable 文案明确 not_null 现状与下一步`
- `a42ca36 fix: NL2SQL 主子问题并集注入 —— scopeQuestion 渲染 <scope_hint> 段`
- 后续每个 Phase 的 commit 会回链本文件

---

## 附录 A：详细任务列表（TDD 风格）

> 每 Phase 独立可发布；Phase N 的 Task N.M 表示第 N 个 Phase 的第 M 个任务。

### Phase 0 Tasks

#### Task 0.1: 测试先行 — PO 完成率 Feature 计算正确性

**Files:**
- Create: `backend/tests/integration/test_supplier_po_completion_rate.py`
- Modify: `backend/scripts/seed_features.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_supplier_po_completion_rate_computes_avg_ratio`
- [ ] **Step 2**: 跑测试确认失败（feature_definition 表无此行）
- [ ] **Step 3**: 在 `seed_features.py` 追加 `SUPPLIER_PO_COMPLETION_RATE` FeatureDefinition（`calculation_logic` 用上文 SQL）
- [ ] **Step 4**: 跑测试确认通过
- [ ] **Step 5**: 跑 `seed_features.py` 写入 DB
- [ ] **Step 6**: Commit `feat(seed): add SUPPLIER_PO_COMPLETION_RATE feature definition`

#### Task 0.2: 测试先行 — Supplier 360 加载新 Feature

**Files:**
- Create: `backend/tests/integration/test_supplier_360_includes_new_feature.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_supplier_360_loads_po_completion_rate`
- [ ] **Step 2**: 跑测试确认失败
- [ ] **Step 3**: 验证 `_kpiSlotFeatureNames()` 自动包含新 Feature（无需改 Supplier360Service）
- [ ] **Step 4**: 跑测试确认通过
- [ ] **Step 5**: Commit `test: supplier 360 loads new feature`

### Phase 1 Tasks

#### Task 1.1: 测试先行 — semantic_keywords schema

**Files:**
- Create: `backend/alembic/versions/xxxx_add_semantic_keywords.py`
- Modify: `backend/app/domain/models.py:343-389`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_kpi_catalog_has_semantic_keywords_field`
- [ ] **Step 2**: 跑测试确认失败（字段不存在）
- [ ] **Step 3**: 写 Alembic migration 加 `semantic_keywords` + `match_threshold` 列
- [ ] **Step 4**: 跑 `alembic upgrade head`
- [ ] **Step 5**: 修改 Model 加字段
- [ ] **Step 6**: 跑测试确认通过
- [ ] **Step 7**: Commit `feat(db): add semantic_keywords to kpi_catalog`

#### Task 1.2: 测试先行 — KpiSemanticMatchService 关键词匹配

**Files:**
- Create: `backend/app/services/kpi_semantic_match_service.py`
- Create: `backend/tests/unit/test_kpi_semantic_match_service.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_match_exact_alias_returns_kpi_code`
- [ ] **Step 2**: 跑测试确认失败
- [ ] **Step 3**: 实现 `KpiSemanticMatchService.match()` + Jaccard 评分
- [ ] **Step 4**: 跑测试确认通过
- [ ] **Step 5**: 写更多测试：keyword overlap、below threshold、empty input
- [ ] **Step 6**: 跑全部测试
- [ ] **Step 7**: Commit `feat(service): KpiSemanticMatchService with Jaccard match`

#### Task 1.3: 测试先行 — KpiMatchCache 启动预热 + 写时失效

**Files:**
- Create: `backend/app/services/kpi_match_cache.py`
- Create: `backend/tests/unit/test_kpi_match_cache.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_cache_loads_on_startup`、`test_cache_invalidates_on_kpi_update`
- [ ] **Step 2**: 实现 `KpiMatchCache` + 事件订阅
- [ ] **Step 3**: 跑测试确认通过
- [ ] **Step 4**: Commit `feat(service): KpiMatchCache with startup preload + write invalidation`

#### Task 1.4: 测试先行 — chat_service L1 拦截

**Files:**
- Modify: `backend/app/services/chat_service.py:402`
- Create: `backend/tests/integration/test_chat_l1_routing.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_chat_l1_match_skips_llm`、`test_chat_l1_match_returns_chat_response`
- [ ] **Step 2**: 实现 `_buildL1Response()` + 在 `processMessage` 第 402 行插入 L1 匹配
- [ ] **Step 3**: 跑测试确认通过
- [ ] **Step 4**: Commit `feat(chat): L1 KPI semantic match routing`

#### Task 1.5: 测试先行 — seed_kpi_semantic_index 脚本

**Files:**
- Create: `backend/scripts/seed_kpi_semantic_index.py`
- Create: `backend/tests/integration/test_seed_kpi_semantic_index.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_seed_writes_semantic_keywords_for_12_kpis`
- [ ] **Step 2**: 实现 seed 脚本（`on_conflict_do_update` 模式，参考 `qa-system-seed-upsert-pattern.md`）
- [ ] **Step 3**: 跑测试确认通过
- [ ] **Step 4**: Commit `feat(seed): populate semantic_keywords for KPI catalog`

#### Task 1.6: 测试先行 — /search 端点

**Files:**
- Modify: `backend/app/api/v1/kpi_catalog.py`
- Create: `backend/tests/unit/test_kpi_catalog_api.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_search_endpoint_returns_matching_kpis`
- [ ] **Step 2**: 实现 GET /kpi-catalog/search?q=...
- [ ] **Step 3**: 跑测试确认通过
- [ ] **Step 4**: Commit `feat(api): KPI catalog search endpoint`

### Phase 2 Tasks

#### Task 2.1: 测试先行 — formula_parser 支持 CTE

**Files:**
- Modify: `backend/app/services/formula_parser.py`
- Create: `backend/tests/unit/test_formula_parser_cte.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_parse_cte_formula_extracts_columns`、`test_parse_cte_with_stddev`
- [ ] **Step 2**: 实现 CTE 解析 + 扩展 `_AGGREGATE_FUNCTIONS`
- [ ] **Step 3**: 跑测试确认通过
- [ ] **Step 4**: Commit `feat(formula-parser): support CTE and additional aggregates`

#### Task 2.2: 测试先行 — validatePlan 允许 CTE formula

**Files:**
- Modify: `backend/app/services/nl2sql_service.py:1499-1505`
- Create: `backend/tests/unit/test_nl2sql_validate_plan_cte.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_validate_plan_accepts_cte_formula_for_ratio_alias`
- [ ] **Step 2**: 修改报错信息允许 CTE
- [ ] **Step 3**: 跑测试确认通过
- [ ] **Step 4**: Commit `feat(validate): allow CTE form in ratio/percent aliases`

#### Task 2.3: 测试先行 — Prompt 规则 4 解除窗口函数限制

**Files:**
- Modify: `backend/app/services/nl2sql_service.py:1871-1878, 1978`
- Modify: `Harness/skills/nl2sql-prompt/SKILL.md`
- Create: `backend/tests/unit/test_nl2sql_prompt_template.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_prompt_rule_4_mentions_cte_form`
- [ ] **Step 2**: 修改 Prompt 模板（参考 §5 Phase 2 改写片段）
- [ ] **Step 3**: 同步更新 prompt 技能文档
- [ ] **Step 4**: 跑测试确认通过
- [ ] **Step 5**: Commit `feat(prompt): allow CTE formula in ratio/percent rules`

#### Task 2.4: 测试先行 — L2 端到端 AVG of ratios

**Files:**
- Create: `backend/tests/integration/test_l2_cte_avg_of_ratios.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_l2_generates_cte_sql_for_avg_of_ratios`
- [ ] **Step 2**: 用真实 PostgreSQL + THBI 源库（Oracle）跑通
- [ ] **Step 3**: 验证 SQL 含 `WITH ... AS` + `AVG(...)` 结构
- [ ] **Step 4**: Commit `test(integration): L2 CTE end-to-end`

### Phase 3 Tasks

#### Task 3.1: 测试先行 — ChainedStep 数据结构

**Files:**
- Create: `backend/app/domain/chained_step_plan.py`
- Create: `backend/tests/unit/test_chained_step_plan.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_chained_step_immutable`、`test_chained_step_prior_cte_render`
- [ ] **Step 2**: 实现 dataclass + `_renderPriorCte()` 工具函数
- [ ] **Step 3**: 跑测试确认通过
- [ ] **Step 4**: Commit `feat(domain): ChainedStep data model`

#### Task 3.2: 测试先行 — Nl2SqlService.generateSql 支持 prior_cte

**Files:**
- Modify: `backend/app/services/nl2sql_service.py`
- Create: `backend/tests/unit/test_nl2sql_prior_cte.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_generate_sql_injects_prior_cte`
- [ ] **Step 2**: 修改 `generateSql` 接收 `prior_cte` 参数
- [ ] **Step 3**: 拼装 SQL：`WITH prior_cte SELECT ... FROM current_subquery`
- [ ] **Step 4**: 跑测试确认通过
- [ ] **Step 5**: Commit `feat(nl2sql): generateSql supports prior_cte injection`

#### Task 3.3: 测试先行 — _executeChainedSteps 引擎

**Files:**
- Modify: `backend/app/services/chat_service.py`
- Create: `backend/tests/integration/test_l3_chained_steps.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_chained_steps_execute_sequentially`、`test_chained_steps_isolate_failures`、`test_chained_steps_max_5`
- [ ] **Step 2**: 实现 `_executeChainedSteps()` 替换 `_executeMultiStep()`
- [ ] **Step 3**: 跑测试确认通过（真实 DB）
- [ ] **Step 4**: Commit `feat(chat): chained steps execution engine`

### Phase 4 Tasks

#### Task 4.1: 测试先行 — BaseLlmClient 支持 tool calling

**Files:**
- Modify: `backend/app/infrastructure/llm/base_client.py`
- Modify: `backend/app/infrastructure/llm/openai_client.py`
- Create: `backend/tests/unit/test_llm_client_tool_calling.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_complete_with_tools_returns_tool_calls`
- [ ] **Step 2**: 在 `BaseLlmClient` 加 `complete_with_tools()` 抽象方法
- [ ] **Step 3**: 在 `OpenAiClient` 实现（透传 OpenAI tools API）
- [ ] **Step 4**: 跑测试确认通过
- [ ] **Step 5**: Commit `feat(llm): BaseLlmClient supports tool calling`

#### Task 4.2: 测试先行 — 5 个 NL2SQL 工具

**Files:**
- Create: `backend/app/services/agent_tools_nl2sql.py`
- Create: `backend/tests/unit/test_agent_tools_nl2sql.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — 每个工具 1 个测试（list_tables/describe_table/sample_rows/execute_sql/list_joins）
- [ ] **Step 2**: 实现 5 个工具 handler（`execute_sql` 复用现有连接池 + `_assert_read_only`）
- [ ] **Step 3**: 跑测试确认通过
- [ ] **Step 4**: Commit `feat(agent-tools): 5 NL2SQL tools`

#### Task 4.3: 测试先行 — LangGraph Agent Loop

**Files:**
- New: `backend/app/services/agent_state.py`
- Modify: `backend/app/services/agent_runtime_service.py`
- Modify: `backend/pyproject.toml`（langgraph）
- Create: `backend/tests/integration/test_l4_agent_loop.py`

**Steps:**
- [ ] **Step 1**: `pyproject.toml` 加 `langgraph>=0.2.0` + 跑 `poetry install`
- [ ] **Step 2**: 写失败测试 — `test_agent_loop_converges_in_max_iterations`、`test_agent_loop_executes_tools`、`test_agent_loop_max_iterations_cap`
- [ ] **Step 3**: 实现 `run_agent_loop()` + StateGraph
- [ ] **Step 4**: 跑测试确认通过
- [ ] **Step 5**: Commit `feat(agent): LangGraph NL2SQL agent loop`

#### Task 4.4: 测试先行 — Chat 集成 L4 入口

**Files:**
- Modify: `backend/app/services/chat_service.py`
- Create: `backend/tests/integration/test_chat_l4_agent.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_chat_routes_to_agent_loop_on_complex_question`
- [ ] **Step 2**: 实现 `_handleNl2SqlAgent()` 入口
- [ ] **Step 3**: 跑测试确认通过
- [ ] **Step 4**: Commit `feat(chat): L4 agent loop routing entry`

### Phase 5 Tasks

#### Task 5.1: 测试先行 — MetricPromotionService 扫描冷指标

**Files:**
- Create: `backend/app/services/metric_promotion_service.py`
- Create: `backend/tests/unit/test_metric_promotion_service.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_scan_finds_repeated_l2_hits`、`test_auto_register_writes_kpi_catalog`
- [ ] **Step 2**: 实现扫描 + 提议 + 注册
- [ ] **Step 3**: 跑测试确认通过
- [ ] **Step 4**: Commit `feat(service): MetricPromotionService for cold-to-hot promotion`

#### Task 5.2: 测试先行 — RoutingMetricsService 监控

**Files:**
- Create: `backend/app/services/routing_metrics_service.py`
- Create: `backend/tests/unit/test_routing_metrics_service.py`

**Steps:**
- [ ] **Step 1**: 写失败测试 — `test_layer_distribution_aggregates_correctly`
- [ ] **Step 2**: 实现各层命中率/平均 token 统计
- [ ] **Step 3]: 跑测试确认通过
- [ ] **Step 4]: Commit `feat(service): RoutingMetricsService`

#### Task 5.3: 测试先行 — 前端监控面板

**Files:**
- Create: `frontend/src/pages/RoutingMetricsPage.tsx`
- Create: `frontend/src/pages/__tests__/RoutingMetricsPage.test.tsx`

**Steps:**
- [ ] **Step 1]: 写失败测试 — `test_routing_metrics_page_renders_4_layers`
- [ ] **Step 2]: 实现 React 组件 + ECharts 图表
- [ ] **Step 3]: 跑测试确认通过（vitest）
- [ ] **Step 4]: Commit `feat(frontend): routing metrics dashboard`

#### Task 5.4: Wiki + 文档同步

**Files:**
- Modify: `Harness/wiki/nl2sql-engine.md`
- Create: `Harness/wiki/metric-pipeline.md`
- Create: `Harness/wiki/agent-loop.md`

**Steps:**
- [ ] **Step 1**: 在 nl2sql-engine.md 加「4 层路由架构」章节
- [ ] **Step 2]: 新建 metric-pipeline.md 详解 L1/L2/L3
- [ ] **Step 3]: 新建 agent-loop.md 详解 L4
- [ ] **Step 4]: Commit `docs(wiki): 4-layer routing architecture`

---

## 附录 B：风险登记册

| ID | 风险 | 影响 | 概率 | 缓解 |
|---|---|---|---|---|
| R1 | L2 prompt 改动导致回归（LLM 不再生成窗口函数形式 formula） | 高 | 中 | 保留窗口函数为合法形式之一；A/B 测试 |
| R2 | L3 CTE 注入导致 SQL 性能下降 | 中 | 高 | SQL 执行前 EXPLAIN；超时 30s 强制 kill |
| R3 | L4 LangGraph 引入破坏现有 Agent Runtime | 高 | 中 | Phase 4 严格隔离（独立 service 路径） |
| R4 | 冷指标晋升机制误触发（写入错误 KPI） | 中 | 低 | 必须 admin 审核，不自动写入 |
| R5 | Phase 1 L1 误命中（不该命中的指标被命中） | 中 | 中 | match_threshold=0.75；可调 |
| R6 | THBI Oracle 源库不可用导致 Phase 0 集成测试失败 | 低 | 中 | 用 mock 数据；集成测试条件 skip |
| R7 | L4 Agent Loop 工具调用频次超限被风控 | 中 | 低 | max_iterations=5 + cost cap |

---

## 附录 C：上线时间表（参考）

| 日期 | Phase | 工作量 | 累计 |
|---|---|---|---|
| Day 1 | Phase 0 | 0.5d | 0.5d |
| Day 2-3 | Phase 1 | 2d | 2.5d |
| Day 4-5 | Phase 2 | 2d | 4.5d |
| Day 6-8 | Phase 3 | 3d | 7.5d |
| Day 9-13 | Phase 4 | 5d | 12.5d |
| Day 14-15 | Phase 5 | 2d | 14.5d |
| Day 16 | 集成测试 + 灰度 | 1d | 15.5d |
| Day 17 | 全量上线 | 0.5d | 16d |

**总计 ~16 天（约 3 周）**。如有并行人力可压缩至 10 天。
