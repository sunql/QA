# KPI P4b 编译器 + P5 治理收口设计

> 主设计：`docs/superpowers/specs/2026-11-11-kpi-definition-consumption-design.md`（P4/P5 两行）。
> 本文件只补 P4b 与 P5 的可执行细节，不改变主设计的 Tier A/B/C 分层与对账语义。
> 状态：待实现。零生产部署。

## 0. 范围

| 阶段 | 交付 |
|---|---|
| P4b | `execution_source=COMPILED` 的指标由结构化列确定性编译成只读 SQL，走 Tier A；编译失败一律 fail-closed 降级 Tier B |
| P5 | KPI/指标状态流转矩阵、发布闸门开启路径与回滚、治理看板（后端聚合 API + 前端页面）、runbook |

**已确认决策**

1. 发布闸门默认值**保持 off**（`system_config` 行缺失即关）；P5 只保证「开启后全链路可用 + 可回滚」。
2. `PUBLISHED → DRAFT` **不允许**，必须先 `DEPRECATED`：`DRAFT→PUBLISHED`（过闸）、`PUBLISHED→DEPRECATED`（下线）、`DEPRECATED→DRAFT`（复活）。
3. 看板交付 **后端聚合 API + 前端页面**。

---

## 1. P4b：measure → SQL 编译器

### 1.1 目标与边界

- 输入只读 `ontology_metric` 的结构化列与源类/join 目录，不嗅探 `formula` 文本。
- 输出**单条 SELECT、单值、列别名 `value`**，交给现有 `KpiExecutionService.execute`（只读校验、超时、行上限、证据、降级边界全部复用）。
- 任一输入不足以确定性编译 ⇒ 返回 `CompiledSql(ok=False, reason=...)`，上层降级 Tier B。**绝不允许发半成品/猜测 SQL。**

### 1.2 输入

- `OntologyMetric`：`metric_type`、`numerator_measure`、`denominator_measure`、`aggregation_order`、`filter_expression`、`target_class_id`、`time_grain`、`time_column`、`grain_keys`。
- `OntologyMetricSource`：`(class_id, role)`；用于把 measure 的 `class_id` 与源类表对应；`role` 缺失时按 measure 内 `class_id` 直连。
- `OntologyClass.source_table`：类 → 物理表。
- `OntologyJoin`：`(source_class_id, source_columns) → (target_class_id, target_columns)`，`join_type`（默认 INNER）。
- 问句时间范围（可选）：复用 `parseTimeScope`（P4a）。
- 数据源方言 `DataSourceType`（oracle/postgresql/mysql）与 `schemaPrefix`。

### 1.3 编译语义

单一 `SELECT ... AS value FROM ... [JOIN ...] [WHERE ...]`：

| metric_type / aggregation_order | 输出表达式 |
|---|---|
| MEASURE | `AGG(expr)` |
| RATIO / WEIGHTED_RATIO | `SUM(nExpr) / NULLIF(SUM(dExpr), 0)` |
| RATIO / AVG_OF_RATIOS | `AVG(nExpr / NULLIF(dExpr, 0))` |
| DIFFERENCE | `SUM(nExpr) - SUM(dExpr)` |

- `agg`：`SUM` / `AVG` / `COUNT` / `COUNT_DISTINCT`；`COUNT` 且 `expr` 为空 ⇒ `COUNT(*)`；`COUNT_DISTINCT` 必须有 `expr`。
- **measure.filter 编译为聚合内 `CASE WHEN <filter> THEN <expr> END`**，不进 `WHERE`：否则分子/分母各带 filter 时会把另一侧的行过滤掉（WEIGHTED_RATIO 分母恒为 0）。
- `metric.filter_expression` 进 `WHERE`（全局过滤）。
- 源类解析：measure 的 `class_id` 优先；为空且全指标只有唯一源类 ⇒ 回退该源类；仍无法确定 ⇒ fail。
- 跨类：必须能在源类集合内找到 JOIN 路径（BFS，复用 `nl2sql_schema._buildJoinGraph` 的同形逻辑，但按 class_id 建图并渲染真实 `ON`）。无路径 ⇒ fail。
- 时间范围：问句含显式时间 + `time_column` 非空 + 方言 oracle ⇒ 复用 `renderTimeScopePredicate`；**非 Oracle 方言 fail-closed**（`compiled_time_scope_unsupported`），因为 `time_scope_renderer` 目前只实现了 Oracle。
- `grain_keys`：v1 **忽略并记录 warning**（KPI 路径问答取首行首值；GROUP BY 会制造多行歧义）。若将来要按维度出多行，另立设计。
- 标识符引用：Oracle/PostgreSQL 用 `"X"`；MySQL 用 `` `X` ``。表名按 `schemaPrefix` 前缀（复用 `nl2sql_schema._bakeTableName` 的语义）。
- 表达式片段（`expr` / `filter` / `filter_expression`）是管理员写入的目标库 SQL 片段：**按方言原样嵌入**，但编译前先做片段守卫：
  - 拒绝 `;`、`--`、`/*`、`*/`、换行注入外的语句分隔；
  - 括号与引号必须平衡；
  - 拒绝 DML/DDL 关键字（`insert|update|delete|drop|alter|truncate|grant|create`，词边界、忽略大小写）。
- 最终 SQL 再经 `_assert_read_only`（编译器内与执行器内各一次）。

### 1.4 路由与调用点

- `planKpiExecution`：`SOURCE_COMPILED` 不再返回 `Tier B/compiled_not_supported`；与 `ESCAPE_HATCH` 同一后半段判断（datasource / tier_a_eligible / 时间参数化），最终 `TIER_A`。
  - `compiled_not_supported` 常量删除，相关测试与诊断文案同步更新。
- `ChatService._executeTierA`：读取 `metric.execution_source`；
  - `COMPILED` ⇒ `compileMetricSql(...)`；失败 `return (None, reason)`，由既有降级边界转 Tier B；
  - 其他保持 formula 路径（含 P4a 时间参数化）。
- `kpi_diagnose._tierOf`：COMPILED ⇒ A（与运行时一致）。
- `kpi_metric_reconcile` 层 3 不变（COMPILED 不比对可执行 SQL）。

### 1.5 失败原因码（全部 fail-closed）

`compiled_dialect_unsupported`、`compiled_metric_type_unsupported`、
`compiled_aggregation_order_unsupported`、`compiled_measure_missing`、
`compiled_agg_unsupported`、`compiled_expr_missing`、`compiled_invalid_fragment`、
`compiled_measure_class_unresolved`、`compiled_class_table_missing`、
`compiled_join_path_missing`、`compiled_join_direction_unsupported`、
`compiled_expr_cross_table_unsupported`、`compiled_filter_source_unsupported`、
`compiled_time_scope_unsupported`、`compiled_time_column_missing`、
`compiled_not_read_only`。

### 1.6 文件

- 新增 `backend/app/services/kpi_sql_compiler.py`：`compileMetricSql(metric, sources, classes, joins, *, datasourceType, schemaPrefix=None, timeScope=None) -> CompiledSql`（纯函数）+ `loadAndCompileMetricSql(session, metric, ...)`（查询 sources/classes/joins）。
- 修改 `backend/app/services/kpi_execution_service.py`、`backend/app/services/chat_service.py`、`backend/app/api/v1/kpi_diagnose.py`。
- 前端无需改动（`MetricTechniqueForm` 已可填结构化列）。

### 1.7 测试

- `backend/app/tests/unit/test_kpi_sql_compiler.py`（新增）：
  - MEASURE SUM/COUNT/COUNT_DISTINCT；RATIO 两阶；DIFFERENCE；
  - measure filter → `CASE WHEN`；`filter_expression` → `WHERE`；
  - 跨表 JOIN 渲染、无路径 fail、表缺失 fail、方言 fail、坏片段 fail、未支持类型 fail；
  - 同一源类 + measure.class_id 为空时回退唯一源类。
- `backend/app/tests/unit/test_kpi_execution_plan.py`：COMPILED → Tier A 矩阵。
- `backend/app/tests/unit/test_kpi_tier_parity.py`：诊断 Tier 与运行时一致。
- **并入既有** `backend/app/tests/integration/test_kpi_tier_a_chat_api.py`（不新建 integration 文件）：COMPILED 端到端 Tier A（业务库=测试 PG 自身，源类表 `kpi_catalog`）；一个编译失败降级 Tier B 用例。
- 变异取证：临时改坏 CASE filter / JOIN 渲染，测试必须变红（写进实施记录）。

---

## 2. P5：治理收口

### 2.1 状态流转矩阵

`KpiStatus`：

| from → to | 允许 | 条件 |
|---|---|---|
| DRAFT → PUBLISHED | ✅ | 过 `enforceKpiPublishGate` |
| PUBLISHED → DEPRECATED | ✅ | 无需额外闸门 |
| DEPRECATED → DRAFT | ✅ | admin（路由已限） |
| 同状态 → 同状态 | ✅ | 视为无操作 |
| 其他（含 PUBLISHED→DRAFT、DRAFT→DEPRECATED、DEPRECATED→PUBLISHED） | ❌ | `ConflictError`，`code=INVALID_STATUS_TRANSITION`，消息含 from/to 与允许集合 |

`MetricStatus`（DRAFT/PUBLISHED）：

| from → to | 允许 | 条件 |
|---|---|---|
| DRAFT → PUBLISHED | ✅ | 过 `enforceMetricPublishGate` |
| PUBLISHED → DRAFT | ✅ | 但若有 `PUBLISHED` 的 KPI 挂靠该 metric ⇒ 409（防口径断链） |
| 同状态 | ✅ | 无操作 |

实现：新模块 `backend/app/services/kpi_status_machine.py`（纯函数 `assertKpiTransition(from, to)` / `assertMetricTransition(from, to, *, publishedKpiCount)`），service 在写库前调用；非法流转不落库、不写 outbox。合法流转沿用既有 outbox → `audit_log` + `kpi_catalog_history`，**零 DDL**。

### 2.2 发布闸门

- 默认值**不改**（`kpi_metric_publish_gate.py` 默认 False）。
- P5 交付：本地/测试把 `KPI_METRIC_GATE_ENABLED=true` 后验证「不合规发布 409、合规发布成功、看板显示开启」；
- 看板暴露当前开关状态；
- runbook 写明开启/回滚步骤。

### 2.3 治理看板

后端 `GET /api/v1/admin/kpi/governance-summary`（`requireAdmin`，只读）：

```json
{
  "kpis": {"total": 12, "byStatus": {"DRAFT": 3, "PUBLISHED": 8, "DEPRECATED": 1}},
  "metrics": {"total": 12, "byExecutionSource": {"COMPILED": 6, "ESCAPE_HATCH": 5, "NONE": 1}, "byStatus": {"DRAFT": 4, "PUBLISHED": 8}},
  "reconcile": {"windowDays": 7, "mismatchCount": 2, "passCount": 40, "unknownCount": 1, "lastMismatchAt": "..."},
  "syncHealth": {"publishedKpiCount": 8, "milvusKpiCount": 8, "kpiTotal": 12, "neo4jKpiCount": 12},
  "gates": {"publishGateEnabled": false, "tierAEnabled": true, "syncEnabled": true},
  "generatedAt": "2026-11-14T00:00:00Z"
}
```

- `reconcile` 统计来自 `audit_log`（`entity_type="kpi_metric_reconcile"`）近 7 天，按 `payload`/`action` 计数；无记录时全 0（不 500）。
- `syncHealth` 复用 `kpi_diagnose._kpiSyncHealth` 的查询口径（PG 计数 + Milvus/Neo4j 计数，失败降级为 `null` 并附 `syncHealthError`，不让看板整体 500）。
- 新路由文件 `backend/app/api/v1/kpi_governance.py` 或并入 `kpi_diagnose.py`（选并入，保持单文件职责：KPI 管理面）。

前端：

- 新页 `frontend/src/pages/KpiGovernancePage.tsx`：统计卡片（KPI 总数/各状态、metric 执行来源分布、对账 mismatch、同步健康、闸门开关），带刷新；仅 admin 可见入口。
- 路由 `/kpi-governance`（`App.tsx`），菜单 seed `backend/scripts/seed_menu_config.py` 增一项（幂等 upsert）。
- i18n `zh-CN` / `en-US` 文案。

### 2.4 Runbook

`Harness/wiki/operations-runbook.md` 新增「KPI 治理」章节：

1. 开关清单与默认值（`KPI_METRIC_TIER_A_ENABLED`、`KPI_METRIC_GATE_ENABLED`、`KPI_METRIC_MATCH_CONTAINMENT_ENABLED`、`KPI_SYNC_ENABLED`、`KPI_MATCH_VECTOR_ENABLED`）；
2. 发布/下线/复活操作步骤与非法流转报错处置；
3. 闸门开启前检查（对账 pass 率、datasource 绑定）与回滚（删除 system_config 行即回落默认）；
4. 同步补偿（`--cleanup` 脚本 / sync-missing 入口）与 admin-only 前提；
5. 数据修复与审计查询（`kpi_catalog_history` / `audit_log`）；
6. 生产发版硬闸门：0143 迁移 → 回填（changes=11 / noop=1）→ 再上代码，never autonomous。

变更记录：`Harness/changes/feat-kpi-p4b-p5/summary.md`。

### 2.5 测试

- `backend/app/tests/unit/test_kpi_status_machine.py`（矩阵全组合，含反向守卫）；
- `backend/app/tests/integration/test_kpi_catalog_governance.py`（并入既有文件）：非法流转 409 + 合法流转成功 + metric 被 PUBLISHED KPI 引用时 PUBLISHED→DRAFT 409；
- `backend/app/tests/integration/test_kpi_diagnose_api.py`（并入）：governance-summary 形状与非 admin 403；
- 前端 `frontend/src/tests/KpiGovernancePage.test.tsx`：渲染统计、非 admin 不可见入口、加载失败提示。

## 3. 明确不做

- 不把发布闸门默认改成 on（决策 1）。
- 不做 PG/MySQL 的时间范围渲染（编译路径 fail-closed）。
- 不做 `grain_keys` 分组编译。
- 不改三层对账语义、不改 Tier A 执行器超时/行数/证据边界。
- 不部署生产；零 DDL（状态机与看板都不需要迁移）。

## 4. 风险

| 风险 | 处置 |
|---|---|
| SQL 注入/越权 | 片段守卫 + `_assert_read_only` + 只读 adapter 三层；fail-closed |
| 编译语义错误（辛普森） | 严格按 aggregation_order 出式；measure.filter 用 CASE；单测钉住 |
| 状态矩阵误伤存量 | 只拦非法流转；同状态放行；集成用例覆盖 |
| 看板拖垮诊断 | 所有外部计数 best-effort，失败降级为 null/0，不让整体 500 |
| CI 分片轮转 | 后端新用例一律并入既有 integration 文件；前端测试不受影响 |

## 5. 验收

- 编译器单测矩阵全绿；COMPILED 指标在 chat 端到端走 Tier A（0 LLM），编译失败降级 Tier B。
- 非法状态流转 409（KPI 与 metric 各自矩阵）；合法流转仍有 audit/history。
- governance-summary 返回约定形状，非 admin 403；前端页可渲染、非 admin 无入口。
- runbook 章节落地。
- CI 失败集合对差净新增 0。
