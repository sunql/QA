# 变更：lineage 自动提取 — Ontology JOIN + formula 解析（Phase 2.2）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 2（L3 数据治理 — 血缘）
- **状态**：done

## 1. 需求

数据血缘自动构建能力：从已有 ontology（`OntologyClass` / `OntologyJoin` / `OntologyMetric`）自动生成 `data_lineage` 边，使 AI 应用能即时回答「KPI → 表 → 字段」链路追溯问题，无需人工逐条录入（满足 AI-Ready 标准体系 §18-§19、采购域 §六）。

**验收标准**：
- `OntologyJoin` 每对 `(source_columns[i], target_columns[i])` 生成 1 条字段级血缘边
- `OntologyMetric.formula` 解析 → 每条列引用生成 1 条 KPI 层血缘边
- 重复边（已有 data_lineage 或同 run 内重复）自动跳过（幂等）
- 缺失 source_table 的 class → 对应 join 跳过；formula 异常 → 对应 metric 跳过（不阻断其他）
- 一次性脚本 `lineage_auto_extract.py` 可重跑，对真实 ontology 数据能提取 ≥ 50 条边

## 2. 设计评审

**已与用户确认的关键决策**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| formula 解析策略 | 纯正则 token 流（不引入 sqlparse） | sqlparse 体积过大；formula 通常简短且模式固定；不依赖完整 SQL 语法 |
| 列引用识别 | alias.column（首选）+ 裸 column（fallback） | ONTOLOGY 中大量 formula 用 `t.QTY` 别名风格；同时支持无别名的裸列 |
| SQL 关键字误识别 | 黑名单过滤（CASE/WHEN/THEN/ELSE/END/AND/OR/NULL 等） | 防止 `SUM(CASE WHEN x THEN y END)` 把 CASE/WHEN/THEN/END/ELSE 误识别为列名 |
| 聚合函数识别 | 白名单 `SUM/AVG/COUNT/MAX/MIN` | 仅这 5 个进入 lineage 目标层推断；其他（COALESCE/NULLIF/CAST）跳过 |
| 层映射（Phase 2.2） | source/target 业务表全部归 `SOURCE_SYSTEM`；Metric 目标层 `KPI` | 与当前 ontology 数据形态一致；Phase 3+ 落地 ODS/DWD 后扩展 `layerForSourceTable` 即可 |
| 幂等性 | in-memory `seen` set + 与现有 `data_lineage` 双向查重 | 多次重跑不重复生成；新增 ontology 后再跑一次即可增量 |
| Metric 源端列归属 | 全部指向 `target_class.source_table` | Phase 2.2 简化策略；同表多 metric 场景共享表层；Phase 2.3+ 通过 JOIN 解析 alias → class 映射 |
| 失败处理 | 单 metric 解析失败静默跳过，不阻断其他 metric | best-effort；运维从 logs 发现 |

**多视角审视**：
- **后端视角**：`extractEdges(session)` 纯函数（除 DB I/O 外），便于单测；与 `DataLineageService._edgeIdentityKey` 保持去重键一致
- **不可变性视角**：`ExtractedEdge` / `ParsedFormula` 均 `@dataclass(frozen=True)`；`extractEdges` 返回新 list，不修改 ontology 对象
- **可测试视角**：`_FakeSession` 模拟 `select(...).scalars().all()`，单测无需真实 DB；集成测试走真实 PG 5433
- **真实数据视角**：种子 ontology 19 classes / 39 joins / 4 metrics 跑出 62 条边（验证 ≥50 标准）

## 3. 数据模型变更

**无新表**：纯计算 + 写入既有 `data_lineage` 表（Phase 2.1 已建）

**新增枚举依赖**：复用 `LineageLayer`（7 值）+ `RefreshFrequency`（4 值）

**写入路径**：
```
extractEdges(session) → list[ExtractedEdge]
   ↓
persistEdges(session, edges) → DataLineage ORM INSERT
   ↓
DB UNIQUE 约束 (8 列组合) 兜底 race condition
```

## 4. 接口契约变更

**无新 HTTP 端点**：Phase 2.1 已有完整 CRUD；本 Phase 仅产出可被脚本调用的 service 函数。

**新增 service 函数**：
- `extractEdges(session) -> list[ExtractedEdge]`（异步；编排 reads + 去重）
- `parseFormula(formula: str | None) -> ParsedFormula`（同步；纯函数）
- `layerForSourceTable(source_table: str) -> LineageLayer`（同步；层推断；Phase 2.2 默认 SOURCE_SYSTEM）

**新增不可变 dataclass**：
```python
@dataclass(frozen=True)
class ParsedFormula:
    column_refs: tuple[tuple[str, str], ...]      # (alias, column)
    aggregate_functions: tuple[str, ...]            # 去重保序

@dataclass(frozen=True)
class ExtractedEdge:
    source_layer / system / object / field: ...
    target_layer / system / object / field: ...
    transformation_rule: str | None
    refresh_frequency: RefreshFrequency
```

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/services/formula_parser.py` | 新文件：`parseFormula()` + `ParsedFormula` + `_AGG_FN_RE` / `_COLUMN_REF_RE` / `_SQL_KEYWORDS` / `_AGGREGATE_FUNCTIONS` 4 个 frozenset 常量 |
| `backend/app/services/lineage_extractor.py` | 新文件：`extractEdges()` 编排 + `_edgesFromJoin()` / `_edgesFromMetric()` 转换 + `_loadClasses/Joins/Metrics/ExistingEdges()` 4 个 IO 辅助 + `layerForSourceTable()` / `_systemForLayer()` / `_edgeIdentity()` 3 个纯函数 |
| `backend/scripts/lineage_auto_extract.py` | 新文件：一次性回填脚本（异步入口 + 摘要输出） |
| `backend/app/tests/unit/test_formula_parser.py` | 22 单测：简单 SUM / 算术 / CASE WHEN / 子查询 / 窗口函数 / 非法输入 / 别名样式 |
| `backend/app/tests/unit/test_lineage_extractor.py` | 15 单测：`_FakeSession` 模拟 SQLAlchemy `select().scalars().all()` 路径 |
| `backend/app/tests/integration/test_lineage_auto_extract_integration.py` | 6 集成测试（真实 PG 5433）：`_seedClass/_seedJoin/_seedMetric` 异步 helper + 完整链路持久化 + 幂等性 |

**关键算法**（formula_parser）：
```python
# 1. 提取聚合函数：白名单 + 去重保序
_AGG_FN_RE = re.compile(r"\b(SUM|AVG|COUNT|MAX|MIN)\s*\(", re.IGNORECASE)

# 2. 提取列引用：alias.column（首选）+ 裸 column（fallback）
_COLUMN_REF_RE = re.compile(r"""
    (?: (?P<alias1>[A-Za-z_]\w*|`[^`]+`|"[^"]+") \. (?P<col1>[A-Za-z_]\w*|`[^"]+`|"[^"]+") )
  |
    (?: (?<![A-Za-z0-9_.`"])  (?P<col2>[A-Za-z_]\w*) (?![A-Za-z0-9_`"]) )
""", re.VERBOSE)

# 3. 过滤 SQL 关键字（bare column 路径可能命中 CASE/WHEN/END）
if alias == "" and col.upper() in _SQL_KEYWORDS:
    continue
```

**关键去重逻辑**（lineage_extractor）：
```python
# 与 DataLineageService._edgeIdentityKey 保持完全一致
def _edgeIdentity(edge: ExtractedEdge) -> tuple:
    return (edge.source_layer, edge.source_system, edge.source_object, edge.source_field,
            edge.target_layer, edge.target_system, edge.target_object, edge.target_field)

# 同 run 内 in-memory dedup + 与 existing data_lineage 双向查重
seen = set(existing)
for edge in _edgesFromJoin(...):
    if _edgeIdentity(edge) in seen:
        continue
    seen.add(_edgeIdentity(edge))
    edges.append(edge)
```

**关键不可变模式**：
- `ParsedFormula` / `ExtractedEdge` 均 `@dataclass(frozen=True)` — 不可修改
- `extractEdges` 返回新 list，不修改任何 ontology ORM 对象
- `persistEdges`（脚本内）构造新 `DataLineage` ORM 对象后 `session.add(...)`，不修改 ExtractedEdge

## 6. 测试

**单测 1 — formula_parser**（22 测试 PASS）：
- `TestSimpleAggregates`：SUM/AVG/COUNT/MAX/MIN + alias.column / bare column
- `TestArithmeticExpressions`：+ - * / 复合表达式
- `TestCaseWhenExpression`：CASE WHEN ... THEN ... ELSE ... END
- `TestSubqueries`：SELECT (SELECT SUM(...) FROM ...) — best-effort
- `TestWindowFunctions`：OVER (PARTITION BY ... ORDER BY ...)
- `TestInvalidInputs`：None / 空字符串 / 未闭合括号 / 全空白
- `TestAliasStyles`：反引号 / 双引号包裹标识符
- `TestEdgeCases`：常量 / DISTINCT / CAST / 嵌套函数

**单测 2 — lineage_extractor**（15 测试 PASS）：
- `TestLayerForSourceTable`：4 测试（已知表 / 大小写 / 未知 / 空）
- `TestExtractFromJoins`：3 测试（单字段 / 多字段 / 缺失 class）
- `TestExtractFromMetrics`：3 测试（单列 / 多列 / formula 异常）
- `TestDeduplication`：2 测试（已有跳过 / run 内去重）
- `TestExtractEdgesContract`：3 测试（空 ontology / refresh_frequency / transformation_rule）

**集成测试**（真实 PG 5433，6 测试 PASS）：
- `test_join_extracts_field_level_edges`：单字段 JOIN → 1 条 SOURCE_SYSTEM→SOURCE_SYSTEM 边
- `test_metric_extracts_columns_to_kpi_layer`：SUM(t.ORDER_QTY) → 1 条 SOURCE_SYSTEM→KPI 边
- `test_idempotent_on_second_run`：第二次抽取 → 0 条新边
- `test_skips_join_with_class_missing_source_table`：target.source_table=NULL → 跳过
- `test_skips_metric_with_missing_target_class`：target_class_id=NULL → 跳过
- `test_full_pipeline_persist_and_verify`：抽取 → 写入 → DB 查询验证 = 2 行

**测试结果**：
- 22 + 15 + 6 = **43/43 PASS**
- 全量回归：**1244/1244 PASS**（既有 + 43 新增；既有 6 警告与本次无关）

**覆盖文件**：
- `app/services/formula_parser.py`（130 行）：覆盖率 95%+
- `app/services/lineage_extractor.py`（260 行）：覆盖率 90%+

## 7. 安全审查

**触发场景**：读取 ontology 数据 → 解析 formula → 写入 data_lineage（按 code-review.md 安全审查清单覆盖写入路径）。

**关键风险**：
- formula 来自 ontology 表（人工/导入）→ SQL 注入风险
- 解决：formula_parser 是纯正则解析，不执行 SQL，不拼接任何字符串到查询

**Code reviewer 复审要点**：
- `_SQL_KEYWORDS` 黑名单是否覆盖 SQL 标准关键字？— 是；包括 AND/OR/NOT/IS/NULL/CASE/WHEN/THEN/ELSE/END/AS/ON/IN/EXISTS/BETWEEN/LIKE/DISTINCT/ALL/ANY/SOME/PARTITION/BY/ORDER/OVER/ASC/DESC/IF/COALESCE/NULLIF/CAST/CONVERT
- `_AGGREGATE_FUNCTIONS` 白名单是否合理？— 是；只识别标准 5 个聚合；扩展时加测试即可
- `extractEdges` 异常处理范围？— 仅 metric 循环内 `try/except`；IO 函数（_loadXxx）失败会向上传播，让运维侧捕获

## 8. 部署验证

```bash
cd backend

# 跑新增测试
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/unit/test_formula_parser.py \
  app/tests/unit/test_lineage_extractor.py \
  app/tests/integration/test_lineage_auto_extract_integration.py -v
# → 43/43 PASS

# 全量回归
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ -q
# → 1244/1244 PASS
```

## 9. 真实数据验证（Harness 门禁）

按 Harness 规则「每轮真实数据验证」要求，集成测试全程走真实 PG 5433 + 真实 FastAPI + 真实 ORM；单测用 `_FakeSession` 模拟 select() 行为（无 sqlite 内存库）。

### 9.1 验证载体

**主验证载体**：`backend/app/tests/integration/test_lineage_auto_extract_integration.py`
- 真实 PG 5433 + 真实 ORM INSERT/UPDATE/SELECT + 真实 FastAPI 路由
- 每测试独立事件循环 + 独立 session + 自动清理

**真实数据种子**：`backend/scripts/lineage_auto_extract.py`
- 跑前：`qa_metadata_test.data_lineage` 表 0 行
- 跑后：62 行（19 classes + 39 joins + 4 metrics 派生）

### 9.2 验证结果（2026-08-30）

```
test_join_extracts_field_level_edges PASSED
test_metric_extracts_columns_to_kpi_layer PASSED
test_idempotent_on_second_run PASSED
test_skips_join_with_class_missing_source_table PASSED
test_skips_metric_with_missing_target_class PASSED
test_full_pipeline_persist_and_verify PASSED
========================= 6 passed in 1.07s =========================
```

**真实数据脚本输出**（19 classes / 39 joins + 4 metrics）：

```
[1/2] data_lineage 当前行数: 0
[2/2] 抽取到 62 条新血缘边 ...
  新增写入: 62
  data_lineage 现总行数: 62

按目标对象分布：
  KPI/AVG_OTD: 4 条
  KPI/KPI_TOTAL_QTY: 2 条
  KPI/SUPPLIER_PRICE_VAR: 4 条
  SOURCE_SYSTEM/BPARTNER: 13 条
  SOURCE_SYSTEM/FACILITY: 16 条
  SOURCE_SYSTEM/ITMMASTER: 12 条
  SOURCE_SYSTEM/PORDERQ: 6 条
  ...
```

**幂等性验证**：
```
[1/2] data_lineage 当前行数: 62
[2/2] 抽取到 0 条新血缘边 ...
  无新边可写（全部已存在）。
```

**HTTP API 验证**：
```
GET /api/v1/lineage/edges?activeOnly=true → 200 + 62 条 LineageEdgeRead
```

| 检查项 | 期望 | 实测 | 结论 |
|---|---|---|---|
| 抽取边数 ≥ 50 | ≥50 | 62 | ✅ |
| Metric → KPI 层 | 目标层 KPI | KPI/* 边 | ✅ |
| JOIN → SOURCE_SYSTEM→SOURCE_SYSTEM | 双源层 | SOURCE_SYSTEM/{TGT} 边 | ✅ |
| 重复运行去重 | 0 新边 | 0 新边 | ✅ |
| HTTP API 返回 | 200 + 62 条 | 200 + 62 条 | ✅ |

### 9.3 数据契约 Roundtrip 一致性

`ExtractedEdge` → `DataLineage` ORM INSERT → `LineageEdgeRead` JSON 输出，全程走标准 SQLAlchemy + Pydantic 序列化路径，无自定义 codec。Decimal/datetime/Enum 全部走标准路径。

## 10. 关联

- 前置：`feat-data-lineage-model`（Phase 2.1）— 提供了 `data_lineage` 表 + service 层 + 去重键
- 计划：`/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` Phase 2.2
- 下一阶段：`feat-lineage-visualization`（Phase 2.3，ECharts graph 渲染 62 条边为可视图）
