# 变更：补 20 个 CHECK 约束进 ORM（ORM ↔ PostgreSQL 约束表达式对齐）

- **日期**：2026-11-02
- **作者**：Claude
- **Phase**：Phase 10 工程治理（Schema 漂移治理 · 承上批）
- **状态**：draft
- **关联变更**：[fix-schema-ssot-alignment](../fix-schema-ssot-alignment/summary.md)（上一批，PR #97 → `ef90ea10`）
- **迁移版本**：无（本批零 DDL、零新迁移、零生产写）
- **MEMORY**：[../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-check-constraints.md](../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-check-constraints.md)

---

## 1. 需求

上一批（Schema SSOT 对齐）在 §8.8.1 留下的**第一号遗留项**：

> **硬规矩**：**禁止**跑 `alembic revision --autogenerate`**（它会为这 20 个 CHECK 约束生成
> `op.drop_constraint('ck_*')` ⇒ 把数据校验静默删掉，而 drift 仍然 exit 0）。

本批处理该遗留项：**把缺的 20 个 CHECK 约束补进 ORM**，
让 `alembic check` 的 `removed check constraint` 归零、autogenerate 不再提议删约束。

**验收标准（可测量）**：

1. `alembic check` 在 5434 `qa_metadata_test` 上：`Detected removed check constraint` **20 → 0**。
2. 全新迁移库（`qa_metadata_test_ck`，`alembic_version` = head）上同样 **0**，且
   `python -m app.infrastructure.schema_drift --strict` **exit 0**。
3. **表达式内容**与 DB 一致（不是只补名字）—— 见 §3 的取证口径。
4. 零 DDL、零生产写、不新增 Alembic 迁移。

---

## 2. 事实基线（实测，非推测）

### 2.1 数量

- DB 侧：`pg_constraint.contype='c'` 且 `connamespace='public'` ⇒ **48** 个命名 CHECK 约束。
- ORM 侧：`Base.metadata` 里 `isinstance(constraint, CheckConstraint)` ⇒ **28** 个。
- ⇒ **缺 20 个**（48 − 28），且这 20 个与 `alembic check` 报出的 20 条
  `Detected removed check constraint` **集合完全一致**（上一批已比对，两边都无差集）。

### 2.2 两个工具都看不见「表达式写错」

- **alembic 1.19.1 不比较 CHECK 约束的表达式内容** —— 它只报「DB 有、ORM 没有」的**名字**。
  ⇒ 只把 20 个名字补进去，`Detected removed check constraint` 就归零了，
  但表达式少一个枚举值 / 比较方向写反，**alembic 不会报**。
- **drift 工具（`app/infrastructure/schema_drift.py`）根本不比较 CHECK 约束**
  ⇒ 补没补、补对没补对，它都是 exit 0。

⇒ **只靠 `alembic check` 验收 = 假绿**。本批必须另建表达式一致性守卫（§3）。

### 2.3 顺带发现的一处真实内容漂移

DB 的 `audit_log.ck_audit_log_action` 允许 `'RESTORE'`
（枚举顺序：`CREATE, UPDATE, DELETE, RESTORE, auth.login, …`），
ORM 的同名约束**漏了 `'RESTORE'`** ⇒ 这是一条**两边都存在、但表达式不同**的分歧，
上一批的「按名字比对」口径完全看不见它。

---

## 3. 设计：为什么用 PG 做规范化对比

### 3.1 方案对比

| 方案 | 做法 | 结论 |
|---|---|---|
| **A. Python 侧归一化后比字符串** | 自己写正则把 `col IN ('a','b')` 与 PG 的 `((col)::text = ANY ((ARRAY[...])::text[]))` 归一到同一形态 | ❌ 脆弱：PG 的渲染规则（类型提升、隐式 cast、`upper()`/`length()` 的重载解析）无法靠正则穷举；写得越复杂越容易**假绿**（把真分歧也归一成一样） |
| **B. 只比名字** | 比 (表名, 约束名) 集合 | ❌ 只能消掉 `alembic check` 的告警，**表达式漂移照样隐形**（§2.2、§2.3） |
| **C. 把 ORM 的表达式原样交给 PG 渲染，再逐字节比（本批采用）** | 临时表 `CREATE TEMP TABLE <probe> (LIKE public.<表>)`（`LIKE` 默认**不**复制 CHECK 约束）→ `ALTER TABLE <probe> ADD CONSTRAINT <name> CHECK (<ORM 表达式>)` → 读回 `pg_get_constraintdef` | **采用**：两边都是 PG 自己 deparse 的规范形式 ⇒ 类型提升、cast、`ANY(ARRAY[…])` 的展开全部由 PG 负责 ⇒ 比的是**语义**，不是文本风格 |

> C 方案的关键前提：`LIKE` 会复制列的类型 / 长度 / NOT NULL，**不复制 CHECK 约束**
> （只有 `INCLUDING CONSTRAINTS` 才复制）⇒ 探针表是干净的，
> 加上去的约束由 PG 现场解析，渲染结果与 DB 上那条**同构可比**。

### 3.2 守卫放在哪

`backend/app/tests/unit/test_orm_check_constraints.py`（**放 `unit/`，不放 `integration/`**）。

原因：CI 的 `backend-integration` job 用
`find app/tests/integration -name 'test_*.py' | sort` 取清单、再按**下标 mod 4** 切 4 个分片
⇒ 在 `integration/` **新增 / 删除 / 重命名任何一个文件**，其后所有文件的分片归属整体轮转一位
⇒ 依赖「同分片内文件执行顺序」的既有测试会突然红（上一批实测踩中 5 条净新增失败）。
本文件只读结构、不需要业务数据 ⇒ 放 `unit/` 可让 `integration/` 的文件清单回到 base 的
字节级一致 ⇒ 分片归属逐文件还原。

> ⚠️ 挪到 `unit/` **没有**消除「`unit/` 与 `integration/` 的 autouse 夹具都会 TRUNCATE
> 共享测试库 5434」这条隐患 —— 该隐患属测试卫生，另案处理。

### 3.3 三条用例（双向 + 表达式）

| 用例 | 断言 |
|---|---|
| `test_db_check_constraints_are_all_declared_in_orm` | DB 的每个命名 CHECK 约束，ORM 都声明了（**DB → ORM**） |
| `test_orm_declares_no_check_constraint_missing_in_db` | ORM 不得声明 DB 上不存在的约束（**ORM → DB**，否则 alembic 会提议 ADD） |
| `test_check_constraint_expressions_match_db` | 同名约束的**表达式内容**经 PG 规范化后逐字节一致 |

另附一条隐含守卫：ORM 侧任何 CHECK 约束**必须有名字**（无名约束对 alembic / drift 都不可见）。

---

## 4. 变更清单

### 4.1 新增测试

- `backend/app/tests/unit/test_orm_check_constraints.py`（新建，3 条用例）

### 4.2 ORM 补声明（13 张表 / 20 条约束）

表达式按 `pg_get_constraintdef` 实测抄写，全部落在 `backend/app/domain/models.py`：

| 表 | 约束名 | 表达式 |
|---|---|---|
| `agent_access_policy` | `ck_agent_permission` | `permission IN ('read','masked_read','forbidden','forbidden_write')` |
| `agent_definition` | `ck_agent_status` | `status IN ('active','draft','deprecated')` |
| `agent_definition` | `ck_agent_response_latency` | `response_latency IN ('realtime','batch')` |
| `agent_definition` | `ck_agent_trigger_type` | `trigger_type IN ('user_question','scheduled','event')` |
| `agent_run_log` | `ck_agent_run_log_status` | `status IN ('success','error')` |
| `audit_outbox` | `ck_audit_outbox_attempts_nonneg` | `attempts >= 0` |
| `business_object` | `ck_business_object_code_upper` | `code = upper(code)` |
| `data_lineage` | `ck_data_lineage_source_layer` | `source_layer IN ('SOURCE_SYSTEM','ODS','DWD','DWS','ADS','KPI','AI')` |
| `data_lineage` | `ck_data_lineage_target_layer` | `target_layer IN ('SOURCE_SYSTEM','ODS','DWD','DWS','ADS','KPI','AI')` |
| `data_lineage` | `ck_data_lineage_refresh_frequency` | `refresh_frequency IN ('REALTIME','HOURLY','DAILY','WEEKLY')` |
| `document_catalog` | `ck_document_security_level` | `security_level IN ('L1','L2','L3')` |
| `document_catalog` | `ck_document_status` | `status IN ('ACTIVE','EXPIRED','ARCHIVED')` |
| `document_entity_relation` | `ck_doc_rel_entity_key_nonempty` | `length(entity_key) > 0` |
| `entity_mapping` | `ck_entity_mapping_entity_type` | `entity_type IN ('SUPPLIER','MATERIAL','PO','GR','IQC','NCR')` |
| `entity_mapping` | `ck_entity_mapping_source_system` | `source_system IN ('ERP','SRM','QMS','MDM','PLM')` |
| `entity_mapping` | `ck_entity_mapping_match_rule` | `match_rule IN ('MDM_MASTER','BUSINESS_KEY','MAPPING')` |
| `feature_definition` | `ck_feature_definition_status` | `status IN ('DRAFT','ACTIVE','DEPRECATED')` |
| `feature_value` | `ck_feature_value_not_both_null` | `value IS NOT NULL OR value_text IS NOT NULL` |
| `kpi_catalog` | `ck_kpi_catalog_status` | `status IN ('DRAFT','PUBLISHED','DEPRECATED')` |
| `wiki_ontology_link` | `chk_link_granularity` | `chunk_id IS NULL OR length(chunk_id) <= 64` |

### 4.3 顺带修的既有漂移

- `audit_log.ck_audit_log_action`：ORM 的枚举列表**补上 `'RESTORE'`**
  （DB 有、ORM 没有 ⇒ 上一批的「按名字比对」口径看不见，见 §2.3）。

⇒ ORM 侧命名 CHECK 约束：**28 → 48**，与 DB 侧数量与内容双向对齐。

---

## 5. 验收证据

### 5.1 `alembic check`（alembic 1.19.1，只读）

| 口径 | 本批前 | 本批后 |
|---|---|---|
| 5434 `qa_metadata_test`：`Detected removed check constraint` | **20** | **0** |
| 5434 `qa_metadata_test`：实质差异行数（不含 78 条序列提示） | **57** | **37** |
| 全新迁移库 `qa_metadata_test_ck`：`Detected removed check constraint` | 20 | **0** |
| 全新迁移库：实质差异行数 | 56 | **36** |

> 两个口径的差额恒为 1 = 共享测试库上 `po_lines_sandbox` 这张测试残留表的 `removed table`。

### 5.2 drift

- 全新迁移库：`python -m app.infrastructure.schema_drift --database-url … --strict` ⇒ **exit 0**
  （`Schema drift 检查通过：ORM 与 DB 一致（表 / 列 / 索引）`）。
- 5434 共享测试库：输出不变，仅剩 `[db:table] po_lines_sandbox`（测试残留，属测试卫生）。

### 5.3 测试

- `app/tests/unit/test_orm_check_constraints.py` ⇒ **3 passed**（RED 时 2 failed / 1 passed，
  失败原因分别为「缺 20 个」与「`ck_audit_log_action` 表达式不一致」）。
- 邻近回归（`test_orm_schema_ssot` / `test_domain_model_registration` /
  `test_check_schema_drift` / `test_user_orm_matches_schema`）⇒ **58 passed**。
- 触及模型的业务单测（business_object / data_lineage / document / entity_mapping /
  kpi_catalog / wiki_link / sync_entity_mapping）⇒ **107 passed**。

### 5.4 行为不变性

本批**只改 ORM 元数据**，DB 上这 20 条约束**本来就存在**（由历史迁移建出）
⇒ 运行时校验行为**完全不变**，不存在「新约束上线后拒绝旧写法」的风险。

---

## 6. 明确不做 / 边界

- ❌ **不写 Alembic 迁移**、不改任何 DDL、零生产写。
- ❌ **不清理**备份表 / 业务表（`BPSUPPLIER`、`PORDER`、`PORDERQ`、`*_20260930`、
  `ontology_class_pre_pruning_20260918`）。
- ❌ **不碰**其余 36 条残余差异（9 changed index（排序方向）、6 条唯一约束 ↔ 唯一索引形状差异、
  1 条新增唯一约束 `uq_doc_entity_rel`、7 条列类型变更、1 条 NOT NULL、6 条列 comment）——
  它们需要 DDL + 数据迁移评估，属另一个项目。
- ❌ **不拆** `app/domain/models.py`（本批 +95 行，2698 → 2793）。
- ❌ **不修** `po_lines_sandbox` 测试残留。
- ❌ 本批**不部署**（零 DDL、零生产写；ORM 改动随下次常规发版生效）。

### 6.1 「禁止 autogenerate」这条硬规矩**依然成立，但理由已收窄

上一批的理由是「autogenerate 会为这 20 个约束生成 `op.drop_constraint('ck_*')`」
⇒ **这一条地雷已拆**（`ck_*` 的 DROP 提议不再出现）。

但**禁止 autogenerate 仍然有效**，因为残余 36 条差异里仍有**破坏性**提议：

- 7 条列类型变更 ⇒ autogenerate 会生成 `ALTER COLUMN … TYPE`（数据可能被截断/转换失败）；
- 6 条唯一约束 ↔ 唯一索引的形状差异 ⇒ 会生成 `DROP CONSTRAINT` + `ADD CONSTRAINT`；
- 9 条 changed index ⇒ 会生成 `DROP INDEX` + `CREATE INDEX`。

⇒ **在残余差异归零之前，`alembic revision --autogenerate` 仍然禁止**；
迁移必须**手写**、逐条对照 DB 实测，不要依赖 autogenerate。

---

## 7. 后续项

1. **`alembic check` 的 36 条残余差异**（需 DDL + 数据迁移评估）⇒ 另立项目。
2. **`app/domain/models.py` 已 2793 行**（超 800 行上限 3.5 倍）⇒ 拆分另案。
3. **表达式索引的列序没有自动化覆盖**（`ix_wiki_import_task_created` 的列序在 drift 与
   `test_orm_schema_ssot.py` 里双双被跳过）⇒ 补一条针对该索引的专门断言。
4. **门禁脚本自身没有自动化测试**（`backend/scripts/fix_schema_ssot_realdata.py` 的 4 条退出码路径
   靠人工实跑取证）。
5. **`unit/` 与 `integration/` 的 autouse 夹具都 TRUNCATE 共享测试库 5434** ⇒ 测试卫生问题，
   根治方向是给只读结构类用例一个不继承截断型夹具的独立作用域。
