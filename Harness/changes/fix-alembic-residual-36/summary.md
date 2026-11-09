# 变更：alembic check 残余 36 条差异归零（16 条零 DDL + A 组 6 组 + 6 项必须 DDL）

- **日期**：2026-11-09
- **作者**：Claude
- **Phase**：Phase 10 工程治理（Schema 漂移治理 · 承 `fix-schema-check-constraints` §11 后续项 #1）
- **状态**：done（本分支已提交并推送；PR 由主会话创建）
- **关联变更**：[fix-schema-check-constraints](../fix-schema-check-constraints/summary.md)（§11 是本批权威方案）
- **迁移版本**：`0141`（手写；`0140` → `0141`）

---

## 1. 需求

上一批（CHECK 约束，PR #98）在「后续项 #1」留下的残余：**`alembic check` 报 36 行实质差异**
（全新迁移库口径）。§11 已评估：**全部对运行时无影响**，可安全排期；拆成三块：

1. **零 DDL 16 条**：E 组索引 DESC 10 条（fresh 实测 9 条）+ F 组列注释 6 条；
2. **A 组 6 组唯一索引 ↔ 唯一约束**：改成「DB 为真」（ORM `Index(unique=True)`），零 DDL；
3. **必须 DDL 的 6 项**：JSONB→JSON、两处收窄、两处放宽、NOT NULL、两条 FK。

**验收标准**：

1. 全新库 `qa_metadata_test_r36`：`alembic upgrade head` 后 `alembic check` **exit 0**；
   改前基线 **36 行**。
2. 同库 `python -m app.infrastructure.schema_drift --strict` **exit 0**。
3. 5434 共享库：`test_orm_check_constraints.py` 与 `test_orm_schema_ssot.py` 仍全绿。
4. 新守卫**不做假绿**：E 组改表达式后 drift 与 SSOT 用例都会跳过列比较 ⇒
   必须新增替代守卫，并做**变异取证**。
5. 迁移手写（禁 autogenerate），upgrade + downgrade 都有；fresh 库
   `upgrade head → downgrade -1 → upgrade head` 全程无错。

---

## 2. 事实基线（fresh 库实测，2026-11-09）

`alembic check` 36 行 = 30 项（6 组形状差异各占 2 行）+ 6 项 DDL 分解：

| 类别 | 行数 | 明细 |
|---|---|---|
| changed index（DESC） | 9 | `ix_audit_log_actor` / `ix_audit_log_entity` / `ix_feature_def_history_feature_changed` / `ix_knowledge_conflict_open` / `ix_kpi_history_kpi_revision` / `ix_learning_feedback_time` / `ix_structure_suggestion_page` / `ix_structure_suggestion_pending` / `ix_wiki_token_usage_mechanism_time` |
| 唯一索引 ↔ 唯一约束 | 12（6 项） | `uq_data_lineage_edge` / `uq_entity_mapping_entity_source` / `uq_feature_definition_name` / `uq_feature_value_dedup` / `uq_id_mapping_bo_ext` / `uq_kpi_catalog_code` |
| ORM 多出的唯一约束 | 1 | `uq_doc_entity_rel`（DB 无） |
| 列类型变更 | 7 | 见 §3.3 |
| NOT NULL | 1 | `l3_acl.granted_at` |
| 列 comment | 6 | `data_quality_rule.rule_params`、`knowledge_claim.authority_department`、`knowledge_claim.source_version`、`session_query_state.user_id`、`session_query_state.inheritance_snapshot`、`wiki_page.authority_department` |

### 2.1 与 §11「10 条 DESC」的差异

§11 的 10 条来自 5433（prod）实测；**全新库只有 9 条**。第 10 条
`ix_wiki_compile_task_created` 是 **prod 被手工改成 DESC**，而 0077 迁移建的是普通列序 ⇒
fresh 无此差异。本批按 fresh 实际（9 条）处理，不修改历史迁移；prod 的这条差异留在
5433 侧（§5.3）。

### 2.2 `uq_doc_entity_rel`：不是「漏声明」，是迁移链真的丢了

- 0030 建表时创建该唯一约束；
- 0042 `ALTER TABLE ... DROP COLUMN entity_key` 时**随列级联删除**，重建列后未重建约束；
- fresh 库与 5433 实测都不存在；全仓无 `ON CONFLICT` 依赖该约束。

⇒ 本批从 ORM 移除声明（「DB 为真」），而不是凭 ORM 记忆 ADD 一条 DB 从未有的约束。
真要恢复唯一性，必须先评估重复行 + 写专用迁移（§7 后续项）。

### 2.3 alembic 消息方向（沿用 §11.1 的核实）

- `Detected type change from X to Y` ⇒ **X = DB 侧，Y = ORM 侧**；
- `Detected NOT NULL on column` ⇒ **ORM 侧是 NOT NULL**（DB 可空）。

---

## 3. 变更清单

### 3.1 零 DDL：E 组 9 条表达式索引

ORM 改用仓库既有惯例 `sa_text("col DESC")`（样板：
`app/domain/wiki_learning_models.py` 的 `saText("created_time DESC")`、
`app/domain/mcp_models.py`）：

- `app/domain/models.py`：`ix_feature_def_history_feature_changed`、
  `ix_audit_log_entity`、`ix_audit_log_actor`、`ix_kpi_history_kpi_revision`
- `app/domain/wiki_learning_models.py`：`ix_wiki_token_usage_mechanism_time`、
  `ix_learning_feedback_time`、`ix_knowledge_conflict_open`（保留
  `postgresql_where`）、`ix_structure_suggestion_page`、`ix_structure_suggestion_pending`

### 3.2 零 DDL：F 组 6 条列注释

注释文本**逐字照抄** `col_description`：

- `app/domain/models.py`：`data_quality_rule.rule_params`、`session_query_state.user_id`、
  `session_query_state.inheritance_snapshot`
- `app/domain/wiki_models.py`：`wiki_page.authority_department`、
  `knowledge_claim.authority_department`、`knowledge_claim.source_version`

### 3.3 零 DDL：A 组 6 组 + 两处 ORM 类型声明错误

- 6 组 `UniqueConstraint`（含 2 处列级 `unique=True`）→ 同名 `Index(unique=True)`：
  `data_lineage` / `entity_mapping` / `feature_definition` / `feature_value` /
  `id_mapping` / `kpi_catalog`。
- **先改一条实测**：只改 `uq_id_mapping_bo_ext` 后重跑 `alembic check`，该条双行
  （removed index + added unique constraint）**双双消失** ⇒ 1.19.1 认为
  「DB 唯一索引」==「ORM `Index(unique=True)`」，方向成立后再批量。
- `uq_doc_entity_rel`：从 ORM 移除（见 §2.2）。
- `l3_acl.granted_at`：ORM 显式 `DateTime(timezone=True)`（DB 是 timestamptz；
  原 `Mapped[datetime]` 推导为无时区）。
- `session_query_state.inheritance_snapshot`：ORM 改为 `postgresql.JSON`
  （DB 是 `json`；原 `with_variant(JSONB)` 是声明错误）。
- `sample_pk_values` 的 ORM `server_default` 改为 `'[]'::json`。

### 3.4 必须 DDL：迁移 `0141_alembic_residual_36.py`（手写）

| # | 变更 | 方向 / 证据 |
|---|---|---|
| 1 | `data_quality_violation_sample.sample_pk_values` JSONB → JSON；**先 `DROP DEFAULT`**，改类型后 `SET DEFAULT '[]'::json` | 不先摘默认会报 *default for column cannot be cast automatically to type json* |
| 2 | `document_entity_relation.entity_type` VARCHAR(30) → VARCHAR(20) | 收窄；FK → `business_object.code VARCHAR(20)` 保证值域，表空 |
| 3 | `l3_acl.user_id` / `granted_by` INTEGER → BIGINT | 放宽；表 0 行 |
| 4 | `wiki_token_usage.compile_task_id` BIGINT → INTEGER | 收窄；非空值实测 0 条 |
| 5 | `l3_acl.granted_at` SET NOT NULL | 表 0 行 |
| 6 | `wiki_compile_task.selected_model_id` / `fallback_model_id` 补 FK → `llm_config.id` | **幂等**：fresh 的 0077 已建（PG 自动命名），按「该列是否已有 FK」跳过；prod 缺则补命名 FK |

- 所有含 `::json` / `::jsonb` 的原始 SQL 走 `exec_driver_sql`（`:` 会被 SQLAlchemy
  bindparam 改写）。
- downgrade 严格逆序；FK 只 DROP 本迁移可能新增的命名约束（fresh 的自动命名 FK 不动）。
- **不碰** partial unique（`uq_knowledge_conflict_open` / `uq_structure_suggestion_pending`）。

### 3.5 新增守卫

`app/tests/unit/test_orm_expression_indexes.py`（放 `unit/`；不新增
`integration/` 文件以免 CI 分片轮转）：

1. 9 条 DESC 索引：ORM `index.expressions` 段序 == DB `pg_get_indexdef` 段序
   （列名 + 方向逐段比对；含「至少一个 `TextClause`」断言，防止退回普通列）；
2. 6 组唯一索引：ORM 必须是 `Index(unique=True)`、不得同时是 `UniqueConstraint`；
   DB 必须是非约束支撑的唯一索引且列序一致；
3. `uq_doc_entity_rel` 不得在 ORM 声明；
4. 0141 的 6 项 DDL 终态（类型 / 可空 / 默认值片段 / 两条 FK）钉死。

---

## 4. 测试与变异取证

### 4.1 TDD

- 先写守卫 → 初始 **3 failed**（RED）；
- ORM 改完 → **5 passed**（GREEN）。

### 4.2 变异取证（故意改坏 → 红 → 还原）

| 变异 | 结果 |
|---|---|
| `ix_learning_feedback_time` 的 `saText("feedback_at DESC")` 改为 `saText("feedback_at")` | 红：`表达式索引的列序/方向与 DB 不一致: ["ix_learning_feedback_time: ORM=['feedback_at'] 期望=['feedback_at DESC']"]` |
| `uq_kpi_catalog_code` 去掉 `unique=True` | 红：`kpi_catalog.uq_kpi_catalog_code 必须是 unique=True 的 Index` |

两处还原后重跑：**5 passed**。

### 4.3 定向测试

```
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test \
python3 -m pytest app/tests/unit/test_orm_check_constraints.py \
  app/tests/unit/test_orm_schema_ssot.py \
  app/tests/unit/test_orm_expression_indexes.py -q
```

结果：**15 passed**（5434 共享库，head 已升到 0141）。

---

## 5. 验收证据

### 5.1 全新库 `qa_metadata_test_r36`（5434）

- 改前：`alembic check` **36 行实质差异**，exit 255；
- 只改 ORM 后：**6 行**（只剩必须 DDL 的 6 项）；
- 应用 0141 后：

```
alembic check                      → No new upgrade operations detected.  EXIT=0
schema_drift --strict              → Schema drift 检查通过                  EXIT=0
alembic downgrade -1               → 0141 -> 0140                          EXIT=0
alembic upgrade head               → 0140 -> 0141                          EXIT=0
alembic check（round-trip 之后）   → No new upgrade operations detected.  EXIT=0
```

### 5.2 5434 共享库

- 迁移前确认：`document_entity_relation` / `l3_acl` / `wiki_compile_task` 全 0 行，
  `wiki_token_usage` 非空 `compile_task_id` 0 条 ⇒ 收窄/加 NOT NULL 安全。
- `alembic upgrade head` → 0141，exit 0。
- 三个结构守卫文件 **15 passed**（含 `test_orm_check_constraints` 的
  `_assertDbAtHead` —— 它要求 DB 版本在代码 head 内，故必须升级共享库）。
- 注：5434 上 `alembic check` / drift `--strict` 仍非零，**唯一原因**是历史测试残留表
  `po_lines_sandbox`（本批明确不修）。fresh 库无此残留，故验收用 fresh 库。

### 5.3 5433（生产同构，只读）

- 改动前基线：`alembic check` **127 Detected 行**（含 9 张备份表、78 条
  `Detected sequence named` 提示、prod 独有的 `ix_wiki_compile_task_created` DESC、
  2 条缺失 FK、1 条多余 FK）。
- 代码 head 变为 0141 后，`alembic check` 对 5433 直接报
  **`Target database is not up to date`**（prod 未升级 0141）⇒ 在不写 prod 的前提下
  无法重新测量迁移后的差异。按硬约束**未对 5433 做任何写**；prod 由发版窗口执行 0141。

---

## 6. 边界 / 明确不做

- ❌ 不修 `po_lines_sandbox`（5434 历史测试残留）。
- ❌ 不改历史迁移（`ix_wiki_compile_task_created` 在 prod 被手工改成 DESC；0077 仍是普通列序）。
- ❌ 不处理 5433 的 9 张备份表 / 业务表 / prod 独有 FK / `ix_wiki_compile_task_created`。
- ❌ 不恢复 `uq_doc_entity_rel`（需先评估重复行；见后续项）。
- ❌ 不拆 `app/domain/models.py`（继续追加，行债另行登记）。
- 本批新增的 `0141` **尚未在任何生产库执行**；部署动作属主会话发版流程。

---

## 7. 后续项

1. **`uq_doc_entity_rel` 的真实唯一性**：0042 丢失约束后再未重建。若业务需要该唯一性，
   需先对 `document_entity_relation` 查重复（`document_id, entity_type, entity_key,
   relation_type`），再写专用迁移建唯一索引；本批只做到「ORM 与 DB 现状一致」。
2. **prod 手工改过的 `ix_wiki_compile_task_created`**：prod 是 DESC、迁移/ORM 是普通列序。
   二选一（把迁移改 DESC 需新迁移 + 回滚策略；把 prod 改回普通列序需一次 DDL），另案。
3. **`wiki_token_usage.compile_task_id` 在 prod 多一条 `ON DELETE SET NULL` FK**：
   fresh 与 ORM 都没有。若确认该语义是想要的，需单独迁移 + ORM 同步；否则 prod 侧删除。
4. **5433 备份表清理**：9 张历史表仍在 prod 侧（drift 的 `--strict` 会一直红），另案。
5. **表达式索引的守卫**：本批已新增 `test_orm_expression_indexes.py`；
   `test_orm_schema_ssot.py` 与 drift 仍按原规则跳过表达式索引（本批不改它们，
   避免改变既有比较口径）。
