# 变更：feat-unlimit-row-count（取消智能问答数据库结果条数限制）

- **日期**：2026-08-19
- **作者**：QA System
- **Phase**：已落地
- **状态**：done

## 1. 需求

业务诉求：智能问答场景下，取消业务数据库结果集在服务端被截断的行数上限，便于一次性返回大结果集（如全量收货记录明细）做后处理分析。两个相关配置项默认都改为"关闭兜底"：

- `QUERY_ROW_LIMIT` 默认从 `5000` → `0`（业务 DB `execute_read_only` 实际 fetchmany 上限）
- `NL2SQL_NO_SCOPE_ROW_LIMIT` 默认从 `100` → `0`（NL2SQL 计划层"无范围明细查询"兜底）

两者都保留"环境变量可配"的回滚路径。

## 2. 设计评审

### 候选方案

| 候选 | 判定 | 依据 |
|---|---|---|
| (A) 改默认值为 `0` + fetchmany `None` | ✅ PG/MySQL 路径 | SQLAlchemy 2.x `Result.fetchmany(None)` 文档保证返回所有剩余行（内部走 `partitions()` 循环） |
| (B) 同上 + Oracle 循环 | ✅ Oracle 路径 | oracledb 4.x `cursor.fetchmany(None)` 实际按 `cursor.arraysize`（默认 100）截行，**不能**用 `None` 取全部；必须显式循环直到耗尽 |
| (C) 改成 `int \| None` 字段类型 | ❌ 拒绝 | 引入新的可选字段会破坏 env 变量解析（Pydantic Settings `int` 字段读到空字符串就报错） |

### 与前序变更的关系

- 衔接 `changes/feat-scope-aware-row-limit/summary.md`（2026-08-17 引入的"无范围 100 兜底"）：本次**关闭**该兜底（默认），但保留配置开关（设 `NL2SQL_NO_SCOPE_ROW_LIMIT=100` 即可恢复 2026-08-17 行为）。
- 衔接 `changes/feat-phase3-datasource/summary.md`（最初引入 `queryRowLimit=5000`）：本次**关闭**硬截断，保留配置开关。

### 决策表（`_applyScopeRowLimit`，新默认行为）

| 序 | 条件 | 动作 |
|---|---|---|
| 0 | `plan.isUnanswerable` | 原样返回 |
| 1 | 问题含显式条数 / 最值 | 保留模型值 |
| 2 | 问题含时间范围 **或** `plan.conditions` 非空 | `rowLimit = None` |
| 3 | `plan.aggregations` 或 `plan.groupBy` 非空 | 保留模型值 |
| 4 | 其余（无范围明细全表） | `rowLimit = None`（不再注入 100） |

### 业务 DB 适配器行为（`_OracleAdapter.execute_read_only`）

- `limit > 0`：`fetchmany(limit)`（保持原行为）
- `limit <= 0`：循环 `fetchmany(1000)` 直到返回空列表，分批 `extend` 到结果集（避免一次性巨大数组）

## 3. 数据模型变更

无。

## 4. 接口契约变更

无 API 契约变更（`ChatRequest` / `QueryPlan` 字段名/类型不变）。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/config.py` | `queryRowLimit` 默认 `5000 → 0`，`nl2sqlNoScopeRowLimit` 默认 `100 → 0`；附详细注释 |
| `backend/app/infrastructure/business_db_pool.py` | `_SqlaAdapter.execute_read_only`：`fetchSize = limit if limit > 0 else None`，SQLAlchemy 走 `fetchmany(None)`；`_OracleAdapter.execute_read_only`：`limit > 0` 单次 `fetchmany(limit)`，否则循环 `fetchmany(_UNLIMITED_BATCH_SIZE=1000)` 直到耗尽；新增模块常量 `_UNLIMITED_BATCH_SIZE` |
| `backend/app/tests/unit/test_scope_row_limit.py` | 默认行为测试从"注入 100"改为"保持 None"；新增 `test_default_limit_positive_applies_fallback` 验证配置开启路径；重命名 `test_returns_new_plan_when_modified` → `test_returns_same_plan_when_disabled` + 新增 `test_returns_new_plan_when_fallback_active` |
| `backend/app/tests/unit/test_query_plan_generation.py` | `test_unscoped_question_applies_default_row_limit` → `test_unscoped_question_keeps_null_when_default_disabled` |
| `backend/app/tests/unit/test_datasource_pool.py` | `_FakeOracleCursor` / `_FakeOracleConnection` 默认改为 `streaming=True`（模拟真实 oracledb 的耗尽行为，**防死循环**）；新增 `test_execute_read_only_no_limit_drains_full_streaming_result`（2500 行验证全取回）、`test_execute_read_only_positive_limit_uses_single_fetchmany`（验证单次路径不被循环） |
| `backend/app/tests/integration/test_datasource_api.py` | `TestHostAllowlist` fake settings `queryRowLimit=5000 → 0`（贴合新默认） |

## 6. 测试

### 新增 / 修改

- `tests/unit/test_scope_row_limit.py`：`TestApplyScopeRowLimitDetailBranch` 5 条（含 1 条新加的 `test_default_limit_positive_applies_fallback`）、`TestApplyScopeRowLimitImmutability` 2 条（拆分原 1 条为 2 条）、`TestApplyScopeRowLimitDirtyInput` 3 条（期望改 None）
- `tests/unit/test_query_plan_generation.py`：`TestGenerateValidatedPlan::test_unscoped_question_*` 1 条重命名 + 期望改 None
- `tests/unit/test_datasource_pool.py`：`TestOracleAdapterExecute` +2 条 Oracle 取消限制回归（其中 1 条 2500 行验证 fetchmany 循环路径 + 1 条 spy fetchmany 验证单次路径不被循环）
- `tests/integration/test_datasource_api.py`：`TestHostAllowlist` fake settings 改 0

### 回归

`uv run pytest app/tests/unit/ --ignore=app/tests/unit/test_seed_ontology_sync.py`：**800 passed**（其中本变更相关测试全过）。
`tests/unit/test_seed_ontology_sync.py::testSeedJoinsMaterializesEdgesAndIsIdempotent` 仍是与本次无关的失败（Neo4j 集成测试，本环境未跑）。
集成测试需要真实 PostgreSQL（按项目规范禁止 sqlite 内存库），本环境未跑。

## 7. 安全审查

- **SQL Guard 未受影响**：`_assert_read_only` 仅校验语句动词，与行数限制无关。
- **超时仍生效**：`queryTimeoutSeconds=30` 兜底，防止意外大查询拖垮后端。
- **不可变性保留**：`_applyScopeRowLimit` 仍用 `dataclasses.replace` 返回新 frozen dataclass。
- **Python 列表拼接而非原地变更**：`fetched.extend(batch)` 创建新列表再覆盖局部变量（语义上的"扩展"，非对入参的 mutation）。

## 8. 部署验证

### 回滚开关

```bash
# 恢复业务 DB 行数硬上限（2026-08-13 之前的行为）
export QUERY_ROW_LIMIT=5000

# 恢复 NL2SQL 无范围明细查询的 100 行兜底（2026-08-17 之前的行为）
export NL2SQL_NO_SCOPE_ROW_LIMIT=100
```

完全回滚：revert 本次变更涉及的文件。

### 手动冒烟

| 问题 | 新默认 `plan.rowLimit` | 配置开启（=100） |
|---|---|---|
| 列出所有收货记录 | null | 100 |
| 2025 年的收货记录 | null | null（规则 2 不受 defaultLimit 影响） |
| 前 10 条收货记录 | 10（保持） | 10（保持） |
| 采购额最高的供应商 | 模型值（保持） | 模型值（保持） |
| 按月统计采购趋势 | 模型值（保持） | 模型值（保持） |

| 数据源类型 | queryRowLimit=0 行为 |
|---|---|
| PostgreSQL / MySQL | `fetchmany(None)` → SQLAlchemy 文档保证返回所有剩余行 |
| Oracle | 循环 `fetchmany(1000)` 直到返回 `[]`；`cursor.arraysize` 默认 100 **不再**截断结果集 |

## 9. 关联

- 前序：`Harness/changes/feat-scope-aware-row-limit/summary.md`（2026-08-17 引入的"无范围 100 兜底"）
- 前序：`Harness/changes/feat-phase3-datasource/summary.md`（最初引入 `queryRowLimit=5000`）
- 设计稿：`docs/20260813-NL2SQL.md` §2（历史回归）
- Wiki：`Harness/wiki/nl2sql-engine.md`（"范围感知行数限制"小节默认值描述已过时，需注明本次变更）
- Wiki：`Harness/wiki/config-reference.md`（`QUERY_ROW_LIMIT` / `NL2SQL_NO_SCOPE_ROW_LIMIT` 默认值描述已过时，需注明本次变更）
- 规则：`Harness/rules/开发流程规范.md`（TDD 10 阶段）