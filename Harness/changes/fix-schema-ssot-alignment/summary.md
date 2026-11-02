# 变更：Schema SSOT 对齐（ORM ↔ Alembic ↔ PostgreSQL）

- **日期**：2026-11-02
- **作者**：Claude
- **Phase**：Phase 10 工程治理（Schema 漂移治理）
- **状态**：in-review
- **关联变更**：[fix-schema-drift-two-dbs](../fix-schema-drift-two-dbs/summary.md)
- **迁移版本**：无（本批零 DDL、零新迁移）
- **MEMORY**：[../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-ssot-alignment.md](../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-ssot-alignment.md)

> MEMORY 相对路径按 `Harness/changes/<feature>/` 回退 **8** 级（仓根
> `~/Prejectcode-th/MyWiki/wiki/aicode/qa-system` → 家目录 `~/` 共 5 级，
> 再加 `Harness/changes/<feature>/` 自身 3 级 = 8 级）。
> 已用 `ls` 在仓库正式落点实测：**5/6/7/9 级都不存在，只有 8 级命中**
> （模板里写的 5 级指向 `wiki/.claude/…`，不存在）。

---

## 1. 需求

**用户视角**：容器启动时刷一屏 schema drift warning（`[db:table]` / `[db:column]` /
`[db:index]`），运维告警疲劳，最终有人直接 `SKIP_SCHEMA_CHECK=1` 把检查关掉 ——
检查一旦被关，**启动期 fail-fast 就没了**，漂移会静默积累，直到某次查询在生产上炸掉。

**验收标准（可测量）**：

1. 在**全新空库**上 `alembic upgrade head` → `python -m app.infrastructure.schema_drift --strict`
   **exit 0**，输出里零 `[db:*]`、零 `[orm:*]`、零 `[alembic]`。
2. 共享测试库（5434 `qa_metadata_test`）上 drift 输出**不再含** `[db:column]` 与 `[db:index]`
   （`[db:table]` 允许残留测试库自建的 `po_lines_sandbox`）。
3. 生产同构库（5433 `qa_metadata`）上 drift 输出**不再含** `[db:column]` 与 `[db:index]`
   （`[db:table]` 允许残留 9 张备份表 / 业务表，见 §8.8）。
4. `alembic autogenerate` **不再提议 DROP 活表**。

---

## 2. 设计评审

### 2.1 候选方案对比

| 方案 | 做法 | 代价 | 结论 |
|---|---|---|---|
| **A. 从 DB 上 DROP 多余索引** | 把 DB 里 ORM 没声明的索引全删掉 | ❌ **方向反了**。ORM 仍不声明 ⇒ 下次 `alembic autogenerate` 反而会提议把这些索引 DROP 掉；而且这些索引都有真实迁移在维护，删了等于把「ORM 漏声明」这个真 bug 转成「迁移历史与 DB 不一致」 | 否决 |
| **B. 在 ORM 侧补齐声明（本批采用）** | 把每条迁移建过、ORM 没声明的索引 / 列补进 `__table_args__` / 模型 | 工作量 = 逐条比对 DB 实测定义；ORM 文件变长（`models.py` 已 2698 行，超 800 行上限，本批不拆） | **采用** |
| **C. 建基线豁免名单（把已知差异写进 ignore 列表）** | 让 drift 工具跳过这些已知项 | ❌ 只是把「ORM 漏声明」藏起来；`autogenerate` 的 DROP 提议依旧存在（那 13 张活表的风险不解除），且以后新写的迁移漏声明会被豁免名单掩盖 | 否决 |

### 2.2 根因 + 修复方案（fix 类必填）

**根因**：**不是 DB 残留，是 ORM 侧漏声明**。每一条「多余索引」都能追溯到一条真实的
Alembic 迁移（`op.create_index` / `op.execute` 建出来的），所以正确方向是把迁移建过的东西
补进 ORM，而不是从 DB 上 DROP。

**修复方案**：4 处补齐（见 §5）—— 模块注册 / 漏列 / 33 个索引 / 1 处谓词，方向上把
「`autogenerate` 提议 DROP 13 张活表 + 一批索引」纠正为「**drift 工具可见的**表 / 列 / 索引
漂移归零（`[db:column]` / `[db:index]` 归零、`removed table` / `removed column` 归零）」。

> **初稿的两处说法都不成立，已按实测改掉**：
> ① 「27 个索引」这个数字复现不出来 —— base 分支 `alembic check` 实测 `removed index` = **64**；
> ② 「纠正为『无差异』」不成立 —— `alembic check` 仍报 **56 条**残余（全新迁移库口径）。
> 实测口径（alembic **1.19.1**，只读 `alembic check`，日志见 §8.6）：
>
> | 类别 | 本批前（base `7d28d08a`） | 本批后（HEAD；**ORM 侧自 `c618e060` 起未再变动** —— 其后的 commit 只改文档与注释） |
> |---|---|---|
> | `removed index` | **64** | **6** |
> | `removed table` | **14**（13 张未注册活表 + 测试残留 `po_lines_sandbox`） | **1**（5434 共享库口径：仅测试自建的 `po_lines_sandbox`；**全新迁移库上 = 0**） |
> | `removed column` | **1**（`knowledge_claim.confidence_level`） | **0** |
> | `removed unique constraint` | 1（`uq_metric_source_triple`） | **0** |
> | `removed check constraint` | 20 | **20（本批未收敛 —— 见 §8.8.1 的地雷条目）** |
>
> ⇒ 本批的真实成果是「**drift 工具可见的**表 / 列 / 索引漂移归零」，**不是**「无差异」：
> `alembic check` 在全新迁移库上仍报 **56 条**残余差异（§8.6），本批明确不覆盖。

### 2.3 最终决定

采用方案 B。理由：drift 工具把「DB 多余对象」判成 warning 是**保守但正确**的默认
（历史遗留不该自动删）；但当这些对象**逐条都能追溯到一条真实迁移**时，它们就不是「遗留」，
而是「ORM 欠了声明」。把声明补齐，是唯一能让 **drift、autogenerate、应用运行时**三方
同时闭嘴的做法 —— 方案 A 只让 drift 闭嘴，方案 C 只让 drift 闭嘴且把问题埋起来。

---

## 3. 数据模型变更

**本批无任何数据模型变更**（零 DDL、零新迁移、零生产写）。本批**只改 ORM 声明**、
不改 DB 结构 —— 原因：DB 侧结构已由既有迁移链建好（`alembic_version` = `0140`），
缺的是 ORM 侧对它们的**声明**；改 DB 会引入与本批目标相反的风险。

### 3.1 ORM 侧声明的实际变更清单

| 文件 | 变更 |
|---|---|
| `backend/app/domain/models.py` | 16 个模型的 `__table_args__` 新增 / 追加共 **30 条声明 = 29 个 `Index` + 1 个 `UniqueConstraint`**；`EvaluationReport.ix_evaluation_report_status_running` 的谓词从 `status IN ('PENDING','RUNNING')` 改为 `status = 'RUNNING'`；删除 `L3Acl` 上与 `mapped_column(index=True)` 重复的 `Index("ix_l3_acl_user_id")`（**DDL 完全不变**，只是去掉重复声明） |
| `backend/app/domain/wiki_models.py` | `WikiPage` 新增 `Index("ix_wiki_page_category_id", "category_id")`；`KnowledgeClaim` 新增列 `confidence_level`（`String(10)`、nullable、带 `comment`，与迁移 `0100_knowledge_claim_confidence_level.py` 逐字对齐） |
| `backend/app/domain/wiki_learning_models.py` | `WikiImportTask` 新增 `Index("ix_wiki_import_task_created", saText("created_time DESC"))`；`WikiTokenUsage` 新增 `Index("ix_wiki_token_usage_compile_task", "compile_task_id")` |

**索引总数口径**：**33 条 = 32 个 `Index` + 1 个 `UniqueConstraint`**
（`uq_metric_source_triple` 在 DB 侧是约束支撑的唯一索引 ⇒ ORM 侧必须用
`UniqueConstraint`，用 `Index` 语义会变）。

**文件体量偏差（如实记录）**：`models.py` 实际 **+115 行**（2583 → 2698 行），
**超过计划预估的 +60 行**。原因有二：① 33 条索引的声明在 ORM 里是**展开成多行**写的
（`Index("名称", "col1", "col2", unique=True, postgresql_where=sa.text(...))` 这类
一行放不下）；② 文件末尾追加了 **4 行 module-alias import**（§5.1）。
**计划已明令本批不拆该文件**（拆分 `models.py` 是独立重构项目，另立项 —— 见 §8.8），
所以这 +115 行**不视为偏差缺陷**，但**必须记录在案**：文件已达 2698 行、**超 800 行上限 3.4 倍**，
下批任何继续加声明的动作都应当先安排拆分。

### 3.2 逐条 33 条索引的「DB 实测定义 ↔ ORM 声明」对照表

DB 侧来源：5434 `qa_metadata_test` 的 `pg_index` + `pg_indexes` 实测；
ORM 侧来源：`backend/app/domain/` 三个模型文件的 `__table_args__`（与
`Harness/wiki/data-model.md` 交叉核对）。

| # | 索引名 | 所属表 | DB 侧列元组 | unique | method | predicate | ORM 侧声明形式 |
|---|---|---|---|---|---|---|---|
| 1 | `ix_agent_definition_owner` | `agent_definition` | `(owner)` | 否 | btree | — | `Index` |
| 2 | `ix_agent_definition_status` | `agent_definition` | `(status)` | 否 | btree | — | `Index` |
| 3 | `ix_agent_schedule_agent` | `agent_schedule` | `(agent_id)` | 否 | btree | — | `Index` |
| 4 | `idx_audit_log_actor` | `audit_log` | `(actor)` | 否 | btree | — | `Index` |
| 5 | `idx_audit_log_created_at` | `audit_log` | `(created_at)` | 否 | btree | — | `Index` |
| 6 | `ix_business_object_header_class` | `business_object` | `(header_class_id)` | 否 | btree | — | `Index` |
| 7 | `uq_business_object_header_class` | `business_object` | `(header_class_id)` | 是 | btree | — | `Index(unique=True)` |
| 8 | `uq_layer_rule_manual` | `data_layer_rule` | `(layer)` | 是 | btree | `pattern IS NULL` | `Index(unique=True, postgresql_where)` |
| 9 | `uq_layer_rule_pattern` | `data_layer_rule` | `(layer, pattern)` | 是 | btree | `pattern IS NOT NULL` | `Index(unique=True, postgresql_where)` |
| 10 | `idx_datasource_default` | `data_source` | `(is_default)` | 否 | btree | — | `Index` |
| 11 | `idx_datasource_name` | `data_source` | `(name)` | 否 | btree | — | `Index` |
| 12 | `ix_document_catalog_entity` | `document_catalog` | `(document_id)` | 否 | btree | — | `Index` |
| 13 | `ix_document_catalog_security` | `document_catalog` | `(security_level)` | 否 | btree | — | `Index` |
| 14 | `ix_document_catalog_type` | `document_catalog` | `(document_type)` | 否 | btree | — | `Index` |
| 15 | `uq_embedding_provider_active` | `embedding_provider` | `(is_active)` | 是 | btree | `is_active` | `Index(unique=True, postgresql_where)` |
| 16 | `idx_entity_mapping_supplier_name_trgm` | `entity_mapping` | `(name)` | 否 | **gin**（`gin_trgm_ops`） | `entity_type = 'SUPPLIER'` | `Index(postgresql_using=gin, postgresql_where)` |
| 17 | `ix_entity_mapping_enterprise_code` | `entity_mapping` | `(entity_type, enterprise_code)` | 否 | btree | — | `Index` |
| 18 | `ix_entity_mapping_source_code` | `entity_mapping` | `(entity_type, source_system, source_code)` | 否 | btree | — | `Index` |
| 19 | `ix_evaluation_report_window` | `evaluation_report` | `(time_window_end)` | 否 | btree | — | `Index` |
| 20 | `ix_feature_definition_datasource` | `feature_definition` | `(datasource_id)` | 否 | btree | — | `Index` |
| 21 | `ix_feature_definition_owner` | `feature_definition` | `(owner)` | 否 | btree | — | `Index` |
| 22 | `ix_feature_definition_status` | `feature_definition` | `(status)` | 否 | btree | — | `Index` |
| 23 | `ix_kpi_catalog_owner` | `kpi_catalog` | `(owner)` | 否 | btree | — | `Index` |
| 24 | `ix_kpi_catalog_semantic_keywords` | `kpi_catalog` | `(semantic_keywords)` | 否 | **gin** | — | `Index(postgresql_using=gin)` |
| 25 | `ix_kpi_catalog_status` | `kpi_catalog` | `(status)` | 否 | btree | — | `Index` |
| 26 | `idx_class_name` | `ontology_class` | `(class_name)` | 否 | btree | — | `Index` |
| 27 | `idx_metric_class` | `ontology_metric` | `(target_class_id)` | 否 | btree | — | `Index` |
| 28 | `ix_metric_source_class_id` | `ontology_metric_source` | `(class_id)` | 否 | btree | — | `Index` |
| 29 | `uq_metric_source_triple` | `ontology_metric_source` | `(metric_id, class_id, role)` | 是 | btree | — | **`UniqueConstraint`**（约束支撑索引，名字不在 `table.indexes` 里） |
| 30 | `idx_property_class` | `ontology_property` | `(class_id)` | 否 | btree | — | `Index` |
| 31 | `ix_wiki_import_task_created` | `wiki_import_task` | `(created_time)`（DB 侧实际 `created_time DESC`） | 否 | btree | — | `Index(saText("created_time DESC"))` |
| 32 | `ix_wiki_page_category_id` | `wiki_page` | `(category_id)` | 否 | btree | — | `Index` |
| 33 | `ix_wiki_token_usage_compile_task` | `wiki_token_usage` | `(compile_task_id)` | 否 | btree | — | `Index` |

### 3.3 已知并保留的一处形态差异（不要改）

`ix_wiki_import_task_created` 在 DB 侧是 `created_time DESC`（实测
`pg_get_indexdef` ⇒ `CREATE INDEX ix_wiki_import_task_created ON public.wiki_import_task
USING btree (created_time DESC)`），ORM 侧必须写成**表达式形式**
`saText("created_time DESC")` —— 写成普通列形式会让 `alembic autogenerate` 认为列签名
不同（反射侧是 `col.desc()`）⇒ 提议 drop+create。

---

## 4. 接口契约变更

**无**。本批不改任何 API、DTO、状态机、请求 / 响应字段。原因是本批是**纯 ORM 声明层**
改动：不新增 / 修改任何表、列、索引的物理结构，也不触碰任何 service / router 代码路径。
应用行为上的唯一变化是「启动期 drift 检查不再报这批 warning」，不改变任何业务语义。

---

## 5. 实现要点

按 4 个根因组织（关键文件路径 + 做了什么 + 为什么）：

### 5.1 模型模块没进 `Base.metadata` → 补 4 个 module-alias import

`backend/app/domain/models.py` 文件末尾追加 4 个 module-alias import
（`mcp_models` / `multi_step_models` / `research_models` / `wiki_graph_insight`）。

**后果**：独立 CLI（`python -m app.infrastructure.schema_drift`）只 import
`app.domain.models`，看不到这 13 张活表 → 被误报 `extra_table`；`alembic autogenerate`
更危险 —— 它反过来会提议 **DROP 这 13 张活表**
（`mcp_call_log`、`multi_step_run`、`multi_step_step`、`research_session` / `task` /
`report` / `turn` / `finding` / `checkpoint` / `run_state` / `step_result` /
`event_outbox`、`wiki_graph_insight`）。这是本批拆掉的**头号地雷**。

### 5.2 `knowledge_claim.confidence_level` 列漏声明 → 补列

`backend/app/domain/wiki_models.py` 的 `KnowledgeClaim` 补列，与迁移 `0100` 的类型、
nullable、comment **逐字对齐**（comment 不对齐会让 autogenerate 提议 `alter_column`）。

### 5.3 33 个索引漏声明 → 3 个模型文件的 `__table_args__` 逐条补齐

清单见 §3.2。**ORM 侧补声明时索引的列序、unique、method（btree/gin）、partial 谓词都要
对着 `pg_indexes` 实测抄，不要凭印象写** —— 抄错的代价是 autogenerate 提议重建。

### 5.4 索引谓词分歧 → `ix_evaluation_report_status_running` 对齐迁移 0074

ORM 谓词原写成 `status IN ('PENDING','RUNNING')`，而迁移 `0074_dq_eval_report_progress.py`、
两个真实 DB（5433/5434，`pg_indexes` 实测 `WHERE ((status)::text = 'RUNNING'::text)`）
以及 `Harness/wiki/data-model.md` 第 199 行记录的都是 `status = 'RUNNING'` ⇒ **ORM 是那个异类**。

### 5.5 必须写清的一条硬事实：alembic 全包不比较索引谓词

> alembic **1.19.1**（实测）**全包不比较索引谓词** —— `postgresql_where` 在整个 alembic 包里
> 出现 **0 次**，`alembic/ddl/impl.py` 里 `"where"` 出现 **0 次**。
> **实测环境**：项目 venv
> `backend/.venv/bin/python`（`alembic.__version__ == "1.19.1"`），与 `backend/uv.lock`
> 锁定的 `alembic-1.19.1` 一致（`backend/poetry.lock` 锁 `1.19.2`）；
> `backend/pyproject.toml` 只写 `alembic>=1.13.0` ⇒ **升级 alembic 后需重新复核本结论**。
> ⇒ 谓词分歧**永远不会**被 `alembic check` 或 autogenerate 报出来，drift 工具也不比较谓词
> ⇒ 这类分歧只能靠**专门的测试**钉住。
> 本批为此新增测试 `test_running_status_index_predicate_matches_migration`
> （断言 `str(where) == "status = 'RUNNING'"`）。
> 推论：**「drift 全绿」不等于「ORM 与 DB 语义全等」** —— 谓词、列类型、列 comment、
> 索引 unique 标志、opclass、排序方向都不在 drift 的比较范围内。

---

## 6. 测试

### 6.1 新增测试

**`backend/app/tests/unit/test_orm_schema_ssot.py`**（真实 PostgreSQL，5434 `qa_metadata_test`）：

> **为什么在 `unit/` 而不是 `integration/`**：不是风格偏好，是 **CI 分片稳定性**问题，
> 详见 §8.9。`integration/` 目录的文件清单被 CI 的 round-robin 分片直接消费，
> 在该目录**新增任何一个文件**都会把其后所有文件的分片序号整体挪一位 ⇒ 既有测试的
> 分片归属全变 ⇒ 依赖「同分片内文件顺序」的既有测试会突然红。
> 本文件只读结构、不需要业务数据，放 `unit/` 后 `integration/` 的文件清单回到 base
> 的字节级一致 ⇒ 分片归属逐文件还原 ⇒ 净新增失败归零。

- `test_required_indexes_are_declared_in_orm` —— 32 个 `Index` 逐条出现在 ORM `table.indexes` 里。
- `test_required_unique_constraints_are_declared` —— `uq_metric_source_triple` 必须是
  `UniqueConstraint` 且不能同时声明成 `Index`。
- `test_no_duplicate_index_names_per_table` —— 同一张表上不得有两个同名 `Index`
  （重复声明 ⇒ PG 建表时两条同名 `CREATE INDEX` ⇒ 直接报错）。
- `test_partial_unique_indexes_keep_their_predicate` —— 3 条 partial unique
  （`uq_embedding_provider_active` / `uq_layer_rule_manual` / `uq_layer_rule_pattern`）
  必须带 `postgresql_where` 且谓词文本正确（写成普通 `UniqueConstraint` 会把
  「局部唯一」变成「全局唯一」）。
- `test_orm_index_columns_match_db_columns` —— ORM 与 DB 同名索引的**有序列元组**逐条一致
  （drift 的 blocking 类别 `index_mismatch`）。
- `test_unique_constraint_columns_match_db_index` —— `uq_metric_source_triple` 列序与
  DB 约束支撑索引一致。
- `test_running_status_index_predicate_matches_migration` —— 谓词必须是
  `status = 'RUNNING'`。

**`backend/app/tests/unit/test_domain_model_registration.py`**：

- `test_mcp_call_log_is_registered` / `test_research_tables_are_registered` /
  `test_multi_step_tables_are_registered` / `test_wiki_graph_insight_is_registered` ——
  4 组表必须进 `Base.metadata`。
- `test_registration_does_not_change_existing_table_count` —— 反向守卫。
- `test_knowledge_claim_declares_confidence_level` —— 列存在、`String(10)`、nullable、
  comment 与迁移逐字一致。

### 6.2 修改既有测试

`backend/app/tests/integration/test_check_schema_drift.py`：

- `test_healthy_db_has_no_column_or_index_drift` 的断言从「`[db:index]` 允许出现」
  **收紧为**「`[db:index]` / `[db:column]` / `[orm:table]` / `[orm:column]` /
  `[orm:index]` 一律不得出现」；
- 新增**正向断言**（防止否定断言空转）：drift 输出必须包含 `Schema drift 检查通过`
  或 `仅发现 DB 多余对象` 之一；
- 新增类级 autouse fixture `_cleanDriftProbes` 清理本类自建的两个探针对象
  （`ix_drift_probe` 索引 + `menu_config.drift_probe_col` 列），避免上一次 pytest 被
  SIGINT 打断后留下残渣导致误红。

### 6.3 真实数据门禁脚本 `backend/scripts/fix_schema_ssot_realdata.py` 的两条加强断言

本脚本额外钉住了两件「drift 工具看不见」的事：

- **谓词断言收紧为等值形式** —— 检查 `ix_evaluation_report_status_running` 的真实 `indexdef`
  里是否含 `= 'RUNNING'`。**不能用裸 `RUNNING`**：PG 把 `IN (...)` 渲染成
  `= ANY (ARRAY['PENDING'::text, 'RUNNING'::text])`，里面同样含 `RUNNING` ⇒ 裸子串会放行
  ORM 原来那个错谓词（`status IN ('PENDING','RUNNING')`）。等值形式只匹配
  `status = 'RUNNING'`（实测 indexdef：`WHERE ((status)::text = 'RUNNING'::text)`），
  能把两者区分开。判定逻辑的本地验证输出见 §8.7。
- **code 集合防漏断言** —— drift 工具的 `_SEVERITY_BY_CODE` 是 code 的 SSOT，门禁启动时比对
  自己的 `ALL_CODES` 是否覆盖全部 8 个 code；日后 drift 新增一类 code 而门禁没跟上 ⇒
  直接判失败（退出码 1，走失败项列表），不会静默漏网。

### 6.4 覆盖率（实测）

**实测口径**：只跑本批相关的 **4 个定向文件**（不跑全量 unit / integration 套件 —— 全量 unit
已知有 27min/28 红基线，全量 integration >41min 且含需 SIGKILL 的用例），
`--cov` 覆盖本批改过的 3 个 ORM 模型模块。

```bash
cd backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test' \
TEST_NEO4J_URI='bolt://127.0.0.1:7688' \
TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026' \
/Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_domain_model_registration.py \
  app/tests/unit/test_orm_schema_ssot.py \
  app/tests/integration/test_check_schema_drift.py \
  app/tests/integration/test_user_orm_matches_schema.py -q \
  --cov=app.domain.models --cov=app.domain.wiki_models --cov=app.domain.wiki_learning_models \
  --cov-report=term
```

实测输出（原文）：

```
Name                                 Stmts   Miss  Cover   Missing
------------------------------------------------------------------
app/domain/models.py                   801     55    93%   131, 164, 198, 294, 356, 414, 447, 533, 573, 612, 637, 659, 721, 766, 802, 855-856, 904, 947, 988, 1081, 1113, 1184, 1211, 1245, 1320, 1377, 1447-1453, 1532, 1612, 1651, 1696, 1737, 1800, 1847, 1895, 1942, 1988, 2046, 2085, 2130, 2218, 2259, 2330, 2372, 2426, 2596, 2638, 2679
app/domain/wiki_learning_models.py     134      7    95%   260, 307, 357, 419, 480, 541, 585
app/domain/wiki_models.py              137      6    96%   214, 266, 335, 381, 435, 479
------------------------------------------------------------------
TOTAL                                 1072     68    94%
Required test coverage of 80.0% reached. Total coverage: 93.66%
55 passed, 21 warnings in 22.35s
```

**结论**：三个模块的覆盖率 **93% / 95% / 96%**，合计 **94%（93.66%）**，
**全部 ≥ 80% 门槛**（`Required test coverage of 80.0% reached`）。

**未覆盖部分的如实说明（不粉饰）**：`app/domain/models.py` 的 55 行未覆盖，
经 AST 逐行归因，**55 行全部落在各模型的 `__repr__` 方法体内**
（用 `ast` 把每个未覆盖行号映射到其最近的 `def` 作用域，计数结果为 `55 __repr__`）
⇒ **未覆盖的是展示用字符串拼接，不是业务逻辑分支**。
本批只改 ORM 声明（`__table_args__` / 列声明）与测试，**未新增任何业务逻辑分支**，
所以这 3 个模块的覆盖率不因本批而变化 —— 补齐 `__repr__` 的测试属于收益极低的
覆盖率刷分，另立项。

### 6.5 测试纪律（本批门禁口径）

- 真实 PostgreSQL，**不用 sqlite 内存库**、**不用 `Base.metadata.create_all`**；
- 每条 pytest 都导出 `TEST_DATABASE_URL`（`postgresql+asyncpg://`）+ `TEST_NEO4J_URI`
  + `TEST_NEO4J_PASSWORD`；
- 仓库 `asyncio_mode = "auto"` ⇒ async 测试**不要**加 `@pytest.mark.asyncio`；
- **后端全量 unit 套件与全量 integration 套件都不是本批的门禁** —— 全量 unit 已知有
  27min/28 红基线；全量 integration >41min 且含一个 SIGINT/SIGTERM 都停不掉、需 SIGKILL
  的用例。本批门禁只跑**定向套件**。

---

## 7. 安全审查

**触发条件判定**：本批**不涉及**认证、密钥、SQL Guard、用户输入、外部 API、文件操作、
加解密、支付 ⇒ **不触发** security-reviewer。

本批的安全相关变更（不能只写「不触发」就完事）：

- 新增了 DDL 护栏：`_ensureSchema` 在跑 `alembic upgrade head` 前调用
  `assertTestDatabase(url)`，用库名白名单 `^qa_metadata_test($|_)` 挡住「库名不对」的情况。
  **护栏只看库名、不看主机** —— 生产主机上若恰好有 `qa_metadata_test*` 同名库仍会被放行，
  主机级防护需靠容器 / 宿主机隔离，不在本护栏范围内。
- 本批**零生产写**、零 DDL、不删任何数据；5433 生产同构库全程只读。
- 备份表与业务表（`BPSUPPLIER` / `PORDER` / `PORDERQ` / `*_20260930` /
  `ontology_class_pre_pruning_20260918`）**一律不清理**，需单独确认后另立项。
- 新增的真实数据门禁脚本 `backend/scripts/fix_schema_ssot_realdata.py` **只读**：
  只 SELECT 结构视图，DSN 与密码只从环境变量读（代码里无任何硬编码凭据）。

**结果**：无 CRITICAL / HIGH / MEDIUM / LOW（本批未触发 security-reviewer）。

---

## 8. 部署验证

### 8.1 本批不部署

**本批未部署**。原因：零 DDL、零生产写、零新迁移 —— ORM 声明的改动随下次常规发版生效即可，
没有需要立即上线的功能或修复，不需要重建镜像、不需要跑 compose 冒烟。

### 8.2 全新迁移库 `--strict` 验收（Task 6 实测，命令与输出原文）

```bash
docker exec qa-pg-a1 psql -U qa_user -d postgres -c "DROP DATABASE IF EXISTS qa_metadata_test_ssot"
docker exec qa-pg-a1 psql -U qa_user -d postgres -c "CREATE DATABASE qa_metadata_test_ssot"
cd backend
DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test_ssot' python3 -m alembic upgrade head
python3 -m app.infrastructure.schema_drift \
  --database-url 'postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test_ssot' --strict
echo "EXIT=$?"
```

实测输出（逐字）：

```
INFO __main__: Schema drift 检查通过：ORM 与 DB 一致（表 / 列 / 索引）。
EXIT=0
```

（全新库 `alembic_version` = `0140`，`public` schema 下 95 张表。验证库已 `DROP DATABASE` 清理。）

> **本轮（评审修复轮）已原地复跑一次**：重新建 `qa_metadata_test_ssot` → `alembic upgrade head`
> exit 0 → `schema_drift --strict` exit 0 → 跑 `alembic check`（exit 255，用于 §8.6 的差异计数复核）
> → 门禁脚本在该全新库上 **8 类漂移全 0（含 `extra_table=0`）**、exit 0 → 复跑后删除该临时库。
> 5434 `qa_metadata_test` 与 5433 `qa_metadata` 两个常驻库全程只读，未建未删未改。

### 8.3 共享测试库 5434 drift 输出（实测，逐字）

```
WARNING __main__:   - [db:table] DB 存在但 ORM 未声明的表：po_lines_sandbox
WARNING __main__: 仅发现 DB 多余对象（历史残留，1 类），默认不阻断启动。如需追溯可跑 --strict 或人工核对。
```

⇒ 无 `[db:column]`、无 `[db:index]`。

### 8.4 生产同构库 5433 drift 输出（实测，逐字）

```
WARNING __main__:   - [db:table] DB 存在但 ORM 未声明的表：BPSUPPLIER, PORDER, PORDERQ, evidence_20260930, knowledge_claim_20260930, ontology_class_pre_pruning_20260918, session_message_bak_20260930, session_query_state_20260930, wiki_page_20260930
WARNING __main__: 仅发现 DB 多余对象（历史残留，1 类），默认不阻断启动。如需追溯可跑 --strict 或人工核对。
```

⇒ 无 `[db:column]`、无 `[db:index]`。

### 8.5 定向回归（**Task 6 实测，逐字**；本轮文档修复只改文档与测试注释，但已原地复跑一次）

```
55 passed, 21 warnings in 48.16s
```

**本轮（文档修复轮）原地复跑一次**（同 4 个定向文件，额外带 `--cov`，命令见 §6.4）：

```
55 passed, 21 warnings in 22.35s
Required test coverage of 80.0% reached. Total coverage: 93.66%
```

（与 Task 6 那次同样的 55 passed；覆盖率明细见 §6.4。本轮只改了文档与两处测试**注释里的日期戳**，
断言与逻辑一字未动。）

命令：

```bash
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test' \
TEST_NEO4J_URI='bolt://127.0.0.1:7688' \
TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026' \
python3 -m pytest app/tests/unit/test_domain_model_registration.py \
       app/tests/unit/test_orm_schema_ssot.py \
       app/tests/integration/test_check_schema_drift.py \
       app/tests/integration/test_user_orm_matches_schema.py -q -p no:randomly
```

（21 warnings 全部是 slowapi 的 `asyncio.iscoroutinefunction` DeprecationWarning，与本批无关。）

### 8.6 `alembic check` 残余差异（重要）

全新库上 `alembic check` **仍然 FAILED**（exit 255），报出下列**本批明确不覆盖**的残余差异 ——
它们需要改 DB 结构（DDL）或改迁移历史，超出「补 ORM 声明」的边界。

> **本节数字的三个前提**（换任何一个，数字都要重数）：
> 1. **alembic 版本 = 1.19.1**（项目 venv / `uv.lock` 锁版本）。alembic 的 autogenerate
>    比较逻辑在版本间会变 ⇒ 换版本必须重跑重数。
> 2. **DB = 5434 `qa_metadata_test`**（共享测试库），日志 `/tmp/alembic_check_1191.log`，
>    只读运行，**未做任何 DDL**。
> 3. **「全新迁移库口径」= 剔除 `removed table 'po_lines_sandbox'`**（该表是集成测试
>    自建的残留，全新迁移库上没有）⇒ **全新迁移库 = 56 条**；共享测试库 5434 上 = **57 条**
>    （56 + 1 条 `removed table`）。

**计数口径**：`alembic check` 输出里的**实质差异行数**（同一次运行、同一份日志、同一分类方式）：

```
20  Detected removed check constraint     ← 新增类别：DB 有命名 CHECK 约束、ORM 未声明
 9  Detected changed index
 7  Detected type change from
 7  Detected added unique constraint
 6  Detected removed index
 6  Detected column comment
 1  Detected NOT NULL on column 'l3_acl.granted_at'
────────────────────────────────────────────
= 56 条实质差异（全新迁移库口径）
  另 78 条 "Detected sequence named ..." 是序列提示，不是差异
```

**这 56 条是逐类实测出来的**（不是引用别处）。复现命令（只读）：

```bash
cd <worktree>/backend
DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test' \
PYTHONPATH=<worktree>/backend \
/Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m alembic check \
  > /tmp/alembic_check_1191.log 2>&1     # → exit 255

grep -o "Detected [a-z ]*" /tmp/alembic_check_1191.log | sed 's/ *$//' | sort | uniq -c | sort -rn
```

实测输出（与上表逐行一致）：

```
 78 Detected sequence named
 20 Detected removed check constraint
  9 Detected changed index
  7 Detected type change from
  7 Detected added unique constraint
  6 Detected removed index
  6 Detected column comment
  1 Detected removed table
  1 Detected
```

> ⚠️ 最后一行的裸 `Detected` 是 `NOT NULL on column 'l3_acl.granted_at'` ——
> 因为 `grep -o "Detected [a-z ]*"` 的字符类里没有大写 `NOT NULL` ⇒ 匹配到空串。
> **不要漏掉它**（漏掉会把 56 数成 55）。同理 `Detected sequence named` 78 条是序列提示、
> **不是差异**，不计入。

#### 8.6.1 「行数」↔「项数」换算（明示表）

两个口径同时存在，必须能互相推出来：

| 残余类别 | 输出**行数** | 语义**项数** | 换算说明 |
|---|---|---|---|
| changed index | 9 | 9 | 1 行 = 1 项 |
| 唯一约束 ↔ 唯一索引的**形状**差异 | **12**（6 `removed index` + 其中 6 个 `added unique constraint`） | **6** | **1 项 = 2 行** |
| 新增 unique constraint（DB 有、ORM 无） | 1（第 7 个 `added unique constraint`） | 1 | 1 行 = 1 项 |
| 列类型变更 | 7 | 7 | 1 行 = 1 项 |
| NOT NULL 变化 | 1 | 1 | 1 行 = 1 项 |
| 列 comment 差异 | 6 | 6 | 1 行 = 1 项 |
| **小计（不含 CHECK 约束）** | **36** | **30** | 36 − 6 = 30（只有形状差异那 6 项各占 2 行） |
| **removed check constraint** | **20** | **20**（**单列，不参与上面的折算**） | 1 行 = 1 项；每条对应 DB 侧 1 个**ORM 尚未声明**的命名 CHECK 约束（DB 共 48 个，ORM 已声明 28 个） |
| **合计（全新迁移库口径）** | **56** | **50** | 36 + 20 = 56 行；30 + 20 = 50 项 |

⇒ **「30 项」这个数字是对的，但只覆盖前 36 行**；20 条 CHECK 约束**未**归并进 30
（若要按项数算，是 50 项，不是 30 项）。
**下文一律用「56 条实质差异行」这个口径**（= alembic 输出的实质差异行数）。

#### 8.6.2 残余类别明细

| 残余类别 | 数量 | 对象 / 举例 |
|---|---|---|
| **removed check constraint**（DB 侧 48 个命名 CHECK 约束里，**ORM 侧已声明 28 个、缺这 20 个**）—— **drift 工具根本不比较 CHECK 约束 ⇒ 补没补 CHECK 约束，drift 都看不出来**，详见 §8.8.1 的地雷条目 | **20** | `ck_feature_value_not_both_null`、`ck_agent_status`、`ck_entity_mapping_entity_type`、`ck_kpi_catalog_status`、`chk_link_granularity`、`ck_agent_permission`、`ck_agent_response_latency`、`ck_agent_run_log_status`、`ck_agent_trigger_type`、`ck_audit_outbox_attempts_nonneg`、`ck_business_object_code_upper`、`ck_data_lineage_refresh_frequency`、`ck_data_lineage_source_layer`、`ck_data_lineage_target_layer`、`ck_doc_rel_entity_key_nonempty`、`ck_document_security_level`、`ck_document_status`、`ck_entity_mapping_match_rule`、`ck_entity_mapping_source_system`、`ck_feature_definition_status` |
| changed index（排序方向，DB 是 `DESC` 而 ORM 是普通列） | 9 | `ix_audit_log_actor`、`ix_audit_log_entity`、`ix_feature_def_history_feature_changed`、`ix_knowledge_conflict_open`、`ix_kpi_history_kpi_revision`、`ix_learning_feedback_time`、`ix_structure_suggestion_page`、`ix_structure_suggestion_pending`、`ix_wiki_token_usage_mechanism_time` |
| 唯一约束 ↔ 唯一索引的**形状**差异（DB 用唯一索引、ORM 用 `UniqueConstraint`，语义相同但 alembic 认为要重建）。**口径：每条形状差异 = 2 行提示** —— 1 个 `removed index` + 1 个 `added unique constraint` | 6 条 = **12 行** | `uq_data_lineage_edge`、`uq_entity_mapping_entity_source`、`uq_feature_definition_name`、`uq_feature_value_dedup`、`uq_id_mapping_bo_ext`、`uq_kpi_catalog_code` |
| 新增 unique constraint（DB 有、ORM 无）—— 7 个 `added unique constraint` 里的**第 7 个** | 1 | `uq_doc_entity_rel`（列：`document_id, entity_type, entity_key, relation_type`） |
| 列类型变更 | 7 | `data_quality_violation_sample.sample_pk_values` JSONB→JSON；`document_entity_relation.entity_type` VARCHAR(30)→String(20)；`l3_acl.user_id` / `granted_by` INTEGER→BigInteger；`l3_acl.granted_at` TIMESTAMP(tz=True)→DateTime；`session_query_state.inheritance_snapshot` JSON→JSON；`wiki_token_usage.compile_task_id` BIGINT→Integer |
| NOT NULL 变化 | 1 | `l3_acl.granted_at`（改 NOT NULL） |
| 列 comment 差异 | 6 | `data_quality_rule.rule_params`、`knowledge_claim.authority_department`、`knowledge_claim.source_version`、`session_query_state.user_id`、`session_query_state.inheritance_snapshot`、`wiki_page.authority_department` |
| **removed table / removed column** | **0**（全新迁移库口径） | ← **这正是本批拆掉的地雷**：本批前（base `7d28d08a`）`alembic check` 实测 `removed table` = **14**（13 张未注册活表 + `po_lines_sandbox`）、`removed index` = **64**、`removed column` = **1**（`knowledge_claim.confidence_level`）、`removed unique constraint` = **1**；本批后分别是 **0 / 6 / 0 / 0** |
| **合计（实质差异行数）** | **56** | 另 78 条 `Detected sequence named ...` 是序列提示，不计入 |

> 附注：6 条形状差异里的 `added unique constraint` 有 2 条名字显示为 `None`
> （`uq_feature_definition_name` 对 `('feature_name',)`、`uq_kpi_catalog_code` 对 `('kpi_code',)`）——
> 因为 ORM 侧的 `UniqueConstraint` 无名而 DB 侧索引有名，正是 §2.1 里那条「约束支撑索引按列集合
> 匹配、不比名字」的归一化规则所覆盖的情况。

⇒ **本批的完成定义不是「ORM == DB 全等」，而是「drift 工具可见的漂移归零」。**
上表那些类型 / 约束 / comment / **CHECK 约束**差异属于**另一个项目**（需 DDL + 数据迁移评估），
本批不碰，也不为它们写迁移。

⇒ **边界说明**：drift 工具的 `[alembic]` 检查**只比版本号**（DB 的 `alembic_version`
vs 代码里的 head），**不是** autogenerate 差异检查 —— 所以 `alembic check` 的这 56 条残余差异，
drift 工具**根本看不见**。这解释了为什么 `--strict` 能 exit 0 而 `alembic check` 仍然 FAILED。

### 8.7 真实数据验证脚本输出（新增 `backend/scripts/fix_schema_ssot_realdata.py`）

```bash
cd backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test' \
  python3 scripts/fix_schema_ssot_realdata.py
```

以下三段的**准确性质**（避免把「节选」读成「逐字全文」）：

- **① 与 ② 是脚本 stdout 的完整全文**，一行未省；但块末尾的 `EXIT=0` **不是 stdout**，
  是我在 shell 里 `echo "EXIT=$?"` 补的退出码行 —— 脚本本身不打印它。
- **③ 是对应失败场景的 stderr 全文**（脚本的报错都走 `file=sys.stderr`）+ shell 补的
  `EXIT=2` 行，**不是** stdout。
- 三段全部**只读**、未改动任何对象；均为当前脚本版本（谓词断言收紧 + code 覆盖防漏断言后）
  的实测输出。

**① 5434 `qa_metadata_test`（默认路径）—— stdout 全文**：

```
target.db        = qa_metadata_test
target.allowlist = ['po_lines_sandbox']
drift.alembic         = 0
drift.missing_table   = 0
drift.missing_column  = 0
drift.extra_table     = 1
drift.missing_index   = 0
drift.index_mismatch  = 0
drift.extra_column    = 0
drift.extra_index     = 0

✅ Schema SSOT 对齐真实数据验证通过（只读，未改动任何对象）
EXIT=0
```

**② 5433 `qa_metadata`（显式 `--allow-non-test-db`，只读巡检；9 张表全部落在允许名单内）—— stdout 全文**：

```
⚠️  已显式放行非测试库：qa_metadata（只读巡检模式）
target.db        = qa_metadata
target.allowlist = ['BPSUPPLIER', 'PORDER', 'PORDERQ', 'evidence_20260930', 'knowledge_claim_20260930', 'ontology_class_pre_pruning_20260918', 'session_message_bak_20260930', 'session_query_state_20260930', 'wiki_page_20260930']
drift.alembic         = 0
drift.missing_table   = 0
drift.missing_column  = 0
drift.extra_table     = 9
drift.missing_index   = 0
drift.index_mismatch  = 0
drift.extra_column    = 0
drift.extra_index     = 0

✅ Schema SSOT 对齐真实数据验证通过（只读，未改动任何对象）
EXIT=0
```

**③ 失败路径（退出码语义 0/1/2；每条都是该场景的 stderr 全文 + shell 补的退出码行）**：

```
$ # 指向非测试库、不加开关 → 拒绝（exit 2）
❌ 目标库 qa_metadata 不在测试库白名单（^qa_metadata_test($|_)）。确需只读巡检生产同构库时加 --allow-non-test-db。
EXIT=2

$ # DSN 协议不对 → 拒绝（exit 2）
❌ 参数错误：DSN 必须是 postgresql+asyncpg://（项目仅 asyncpg）
EXIT=2

$ # 未设 TEST_DATABASE_URL 且不传参 → 拒绝（exit 2）
❌ 参数错误：未指定数据库。请传 --database-url 或设置 TEST_DATABASE_URL（默认 5434 qa_metadata_test）
EXIT=2
```

**门禁自身的两条新断言的判定逻辑（纯本地验证，无 DB 写）**：

```
=== 1) 谓词断言 ===
  等值形式 = 'RUNNING'             → 断言通过? True   (期望 True)
  IN 形式 = ANY(ARRAY[...])      → 断言通过? False   (期望 False)

=== 2) 防漏断言 ===
  drift code 集合 (8) = ['alembic', 'extra_column', 'extra_index', 'extra_table', 'index_mismatch', 'missing_column', 'missing_index', 'missing_table']
  门禁 ALL_CODES (8) = ['alembic', 'extra_column', 'extra_index', 'extra_table', 'index_mismatch', 'missing_column', 'missing_index', 'missing_table']
  未覆盖 = []；多余 = []
  模拟新增 'type_mismatch' 后未覆盖 = ['type_mismatch']

✅ 两处新断言的判定逻辑验证通过
```

### 8.8 明确未做的事（本批边界）

- **不部署**（零 DDL、零生产写）。
- **不清理**备份表与业务表：`BPSUPPLIER`、`PORDER`、`PORDERQ`、`*_20260930`、
  `ontology_class_pre_pruning_20260918`（5433 上残留的 9 张表就是它们）。
- **不写迁移**去收敛 §8.6 的 **56 条** `alembic check` 残余差异（需 DDL + 数据迁移评估，另立项）。
- **不拆** `app/domain/models.py`（已 2698 行，超 800 行上限）—— 拆分是独立重构，另立项。
- **不修** `research_models` 的循环 FK `SAWarning`（需 `use_alter=True` + 迁移，另立项）。

#### 8.8.1 ⚠️ 地雷：20 条 `removed check constraint`（下一批必须先处理）

**这是本批留在系统里最危险的一颗雷**，比 §8.6 里其它残余差异都严重：

- **事实**：DB 侧（5434 `qa_metadata_test`，`pg_constraint.contype='c'`）有 **48 个命名 CHECK 约束**，
  **ORM 侧已声明 28 个**（分布在 **13 张表**：`agent_orchestration_binding` / `_profile` / `_run` /
  `_step` / `_usage` 共 17 个，`agent_tool_config`、`audit_log`、`data_quality_score`、
  `evaluation_report`、`evaluation_report_schedule`、`mcp_call_log`（3 个）、`permission_grant`、
  `wiki_ontology_link`（`chk_link_type` / `chk_link_weight`）），
  **缺 20 个** ⇒ **48 − 28 = 20**。这 20 个与 `alembic check`（1.19.1）报出的
  **20 条 `Detected removed check constraint ...`** 集合**完全一致**（已实测比对，两边都无差集）。
  ⚠️ 补的时候**不要重复添加已声明的那 28 个** —— 那会把工作量与风险量级都记错。
- **为什么 drift 看不见**：**drift 工具根本不比较 CHECK 约束**（它只比表集合、列名、
  索引名 + 有序列元组）⇒ **补没补 CHECK 约束，drift 都看不出来**
  （drift 检测不到 CHECK 约束的缺失）⇒ **drift `--strict` 依然 exit 0**，
  门禁脚本也照样 exit 0，**没有任何告警**。
- **为什么危险**：一旦有人跑 `alembic revision --autogenerate`，autogenerate 会为这 20 个
  约束生成 `op.drop_constraint('ck_*')` ⇒ **把数据校验静默删掉**，而 drift 仍然 exit 0
  ⇒ 校验消失这件事在两个工具里都看不出来。
- **点名**（20 条全名见 §8.6.2 表）：
  `ck_feature_value_not_both_null`（feature_value 的「金额与数量不能同时为空」）、
  `ck_agent_status`、`ck_entity_mapping_entity_type`、`ck_kpi_catalog_status`、
  `chk_link_granularity`，以及 `ck_agent_permission`、`ck_agent_response_latency`、
  `ck_agent_run_log_status`、`ck_agent_trigger_type`、`ck_audit_outbox_attempts_nonneg`、
  `ck_business_object_code_upper`、`ck_data_lineage_refresh_frequency`、
  `ck_data_lineage_source_layer`、`ck_data_lineage_target_layer`、
  `ck_doc_rel_entity_key_nonempty`、`ck_document_security_level`、`ck_document_status`、
  `ck_entity_mapping_match_rule`、`ck_entity_mapping_source_system`、
  `ck_feature_definition_status`。
- **下一批必须做的**：**先把缺的 20 个 CHECK 约束补进 ORM**（`__table_args__` 的
  `CheckConstraint`，表达式对着 `pg_get_constraintdef` 实测抄；**已声明的那 28 个不要重复添加**），
  **或**明确决定废弃它们并写迁移显式删除 ——
  两者选一，但**必须显式决策**。
- **在此之前的硬规矩**：**禁止**跑 `alembic revision --autogenerate`**。

---

### 8.9 ⚠️ CI 分片重排陷阱：本批新增 1 个 integration 文件 ⇒ 5 条净新增失败（已定位并规避）

**背景**：PR #97 首轮 CI 相对 main 基线出现 **5 条净新增失败**。按仓库合并判据
（「失败集合对差，净新增为空才合并」，不等 CI 变绿）必须先归零才可合并。

**CI 分片机制**（`.github/workflows/ci.yml`，`backend-integration` job）：

```bash
mapfile -t all_tests < <(find app/tests/integration -name 'test_*.py' -print | sort)
shard_tests=()
for ((i=${{ matrix.shard }}; i<${#all_tests[@]}; i+=4)); do
  shard_tests+=("${all_tests[$i]}")
done
```

⇒ 分片归属 = **排序后下标 mod 4**。**在 `integration/` 目录新增任何一个文件**，
其后所有文件的下标整体 +1 ⇒ 分片归属整体轮转一位 ⇒ 既有测试的**同分片邻居全变**。
本批新增 `test_orm_schema_ssot.py` 后，integration 文件数 **314 → 315**，
于是 base 的 shard 0 尾部整体挪到 shard 1。

**两条净新增失败的根因（均已本地复现，非推测）**：

| 失败 | 报错 | 根因 |
|---|---|---|
| `test_rag_api.py` × 4 | `MilvusException: (code=800, message=database not found[database=qa_ci])` | `qa_ci` 逻辑库由 `ensureIsolatedDatabase()` **惰性**创建，而只有 3 个 integration 文件会调它（`test_insert_embeddings_dual.py` / `test_reconcile_milvus_id_mapping.py` / `test_search_type_routed.py`，经 `milvusCleanClient` 夹具 → `dropOntologyCollectionsForTest()`）。base 里 `test_insert_embeddings_dual.py`（下标 168）与 `test_rag_api.py`（下标 236）**同在 shard 0 且前者先跑** ⇒ 库被建好；HEAD 里 `test_rag_api.py`（下标 237）挪到 shard 1，而 **shard 1 里一个创建者都没有**（已用 awk 逐文件核对）⇒ `qa_ci` 从未被创建 |
| `test_reconcile_neo4j_id_mapping.py::test_reconcile_reports_clean_when_both_sides_align` | `AssertionError: expected 0 diff, got 3` | 该用例要求图谱在**开始时**干净，但 `neo4jCleanDriver` 只在**teardown** 清图、不在 setup 清。shard 1 里更早跑过的文件留下的 Class 节点（带 `unified_id`）被 reconcile 读成 3 条差异 |

**本地复现记录（Neo4j 那条）**：往测试实例 `bolt://127.0.0.1:7688` 手工建 3 个带
`unified_id` 的 Class 节点（`obj:supplier:RESIDUE1` / `obj:material:RESIDUE2` /
`obj:customer:RESIDUE3`），重跑该用例 ⇒ **逐字复现** `expected 0 diff, got 3`；
清空后重跑 ⇒ 通过。（测试自身的 teardown 会清干净，复现后已确认图谱无残留。）

**与本批代码的关系**：本批改动只有 **ORM 元数据 + 文档 + 只读脚本**，**不碰 Milvus / Neo4j**
⇒ 这 5 条**不是本批引入的回归**，而是**分片重排暴露出的既有顺序依赖**。

**处置**：把 `test_orm_schema_ssot.py` 从 `integration/` 挪到 `unit/`
（该文件只读结构、不需要业务数据；`unit/` 的 conftest 同样强制真实 PostgreSQL）。
挪完之后 `integration/` 的文件清单**回到与 base 字节级一致**（已用 `git ls-tree` 双侧核对：
HEAD 与 base 的 integration 测试文件清单**各 314 个、逐名相同、`diff` 为空**）
⇒ **每个文件的分片归属逐个还原** ⇒ 那 5 条净新增失败随之消失，且**不需要改动任何共享夹具**。

**遗留（未修，属另一批 —— 下一个人往 `integration/` 加文件会再踩一次）**：

- `test_rag_api.py` 隐式依赖「同分片里更早的文件建过 `qa_ci` 库」；
  根治方向：把 `ensureIsolatedDatabase()` 提到 `integration/conftest.py` 的
  **session 级 autouse 夹具**，让任何分片都自带该库。
- `neo4jCleanDriver` 只在 teardown 清图、不在 setup 清 ⇒ 首个用到它的用例会吃到上一个
  文件的残留；根治方向：让它在 `yield` **之前**也清一次。
- CI 的 round-robin 分片本身对「新增文件」不稳定（改一个清单就全量重排）；
  根治方向：改成**按文件名哈希**分片或维护一份**显式分片清单**。
  本批**不做**这三项 —— 改共享夹具与 CI 分片策略影响面大，且与本批
  「零 DDL、零生产写、只补 ORM 声明」的边界无关。

**另注**：挪到 `unit/` 后，本文件仍被 `unit/conftest.py` 的 autouse 夹具
（`warmBusinessObjectRegistry` → `dbSession` → `seedEngine` → `_truncateAll`）接管
⇒ **共享测试库仍会被 TRUNCATE**（与 `integration/` 的 `warmAgentCaches` 同构）。
即「挪目录」解决的是**分片稳定性**，**不是**「TRUNCATE 共享库」这条隐患 ——
后者要靠给该文件一个不继承截断型 autouse 的独立夹具作用域来根治，仍未做。

---

## 9. 关联

- 设计稿：本次**无独立设计稿**；实现计划在仓库外
  `/Users/sunql/.claude/plans/quizzical-snuggling-adleman.md`（如实注明：不在本仓内）。
- Wiki：[`Harness/wiki/architecture.md`](../../wiki/architecture.md)（本次已更新 ——
  新增「Schema 漂移治理：ORM ↔ Alembic ↔ PostgreSQL 三方对齐」章节）。
- Rules：[`Harness/rules/工程结构.md`](../../rules/工程结构.md)（本次已更新 ——
  聚合机制说明与 CI 门禁建议两处）、[`Harness/rules/变更记录强制规范.md`](../../rules/变更记录强制规范.md)。
- Memory：`~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-ssot-alignment.md`
  （本仓相对路径见 frontmatter，回退 **8** 级，已 `ls` 实测存在）。
- 关联变更（predecessor）：[`Harness/changes/fix-schema-drift-two-dbs/summary.md`](../fix-schema-drift-two-dbs/summary.md)。
- 数据模型：[`Harness/wiki/data-model.md`](../../wiki/data-model.md)（§3.2 对照表的交叉核对来源；
  本次**未修改**该文件 —— `knowledge_claim.confidence_level` 未收录是既有缺口，另立项）。

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A / B / C 三案 + 取舍表 + 根因与修复方案）
- [x] 第 3 段迁移文件名 ≤ 32 字符 —— **N/A（不适用）**：已核对，本批零 DDL、零新迁移，
      `迁移版本` 字段写「无」，不存在迁移文件名
- [x] 第 7 段触发了 security-reviewer 则必填 CRITICAL/HIGH/MEDIUM/LOW 至少 1 项 ——
      **N/A（不适用）**：已核对触发条件，本批不涉及认证 / 密钥 / SQL Guard / 用户输入 /
      外部 API / 文件操作 / 加解密 / 支付，未触发 security-reviewer；§7 已写清本批的安全相关
      变更与「不触发」判定
- [x] 第 8 段 docker compose 冒烟命令 + 输出贴出 —— **N/A（不适用）**：已核对，**本批不部署**
      （零 DDL、零生产写），故不跑 compose 冒烟；替代验收为 §8.2 全新迁移库 `--strict` exit 0
      + §8.7 真实数据门禁脚本 exit 0（两者都是真实 PostgreSQL 上的实测输出）
- [x] 第 9 段 ≥ 3 个跨文件链接（关联变更 / Wiki / Rules / Memory 中至少 3 个）
- [x] 相关 wiki 文档已更新（架构 → `Harness/wiki/architecture.md` 新增漂移治理章节）
- [x] 至少 1 条 MEMORY 索引已在 `~/.claude/projects/.../memory/MEMORY.md` 添加
- [x] 涉及真实 SQL/DB 改动时 `scripts/<feature>_realdata.py` 已跑通
      （`backend/scripts/fix_schema_ssot_realdata.py`，exit 0，输出见 §8.7）

> 上述任一项未打勾 → code-review 打回 → 不允许合。
> 本清单 **10 项全部已勾选**；其中 3 项标 **N/A（不适用）** —— 语义是「已逐条核对、确认本变更
> 不适用」，不是「做过了」也不是「未满足」。
