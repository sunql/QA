# 变更：补 20 个 CHECK 约束进 ORM（ORM ↔ PostgreSQL 约束表达式对齐）

- **日期**：2026-11-02
- **作者**：Claude
- **Phase**：Phase 10 工程治理（Schema 漂移治理 · 承上一批）
- **状态**：done（已合并到 `main` —— PR #98 → squash `29a63b80`，2026-11-02；**本批不部署**）
- **关联变更**：[fix-schema-ssot-alignment](../fix-schema-ssot-alignment/summary.md)（上一批，PR #97 → `ef90ea10`）
- **迁移版本**：无（本批零生产 DDL、零新迁移、零生产写）
- **MEMORY**：[../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-check-constraints.md](../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-check-constraints.md)

> MEMORY 相对路径按 `Harness/changes/<feature>/` 回退 **8** 级（与上一批同一约定，已用 `ls` 实测）。

---

## 1. 需求

上一批（Schema SSOT 对齐，PR #97）在 §8.8.1 留下的**第一号遗留项**：

> **硬规矩**：**禁止**跑 `alembic revision --autogenerate`**（它会为这 20 个 CHECK 约束生成
> `op.drop_constraint('ck_*')` ⇒ 把数据校验静默删掉，而 drift 仍然 exit 0）。

本批处理该遗留项：**把缺的 20 个 CHECK 约束补进 ORM**，并让 `alembic check` 的
`removed check constraint` 归零、autogenerate 不再提议删约束。

**验收标准（可测量）**：

1. `alembic check` 在 5434 `qa_metadata_test` 上：`Detected removed check constraint` **20 → 0**。
2. 全新迁移库（`qa_metadata_test_ck`，`alembic_version` = head）上同样 **0**，且
   `python -m app.infrastructure.schema_drift --strict` **exit 0**。
3. **表达式内容**与 DB 一致（不是只补名字）—— 判据见 §2.2 与 §6。
4. 零 DDL、零生产写、不新增 Alembic 迁移。

**做完是什么样**：DB 上已有的 48 条命名 CHECK 约束，ORM 侧**逐条镜像**（名字 + 表达式）⇒
「谁建了约束、ORM 就必须声明它」这条纪律有了自动化守卫，而不是靠人记。

---

## 2. 设计评审

### 2.1 事实基线（实测，非推测）

- DB 侧：`pg_constraint.contype='c'` 且 `connamespace='public'` ⇒ **48** 个命名 CHECK 约束。
- ORM 侧：`Base.metadata` 里 `isinstance(constraint, CheckConstraint)` ⇒ **28** 个。
- ⇒ **缺 20 个**（48 − 28），且这 20 个与 `alembic check` 报出的 20 条
  `Detected removed check constraint` **集合完全一致**（上一批已实测比对，两边都无差集）。

### 2.2 两个工具都看不见「表达式写错」

- **alembic 1.19.1 不比较 CHECK 约束的表达式内容** —— 它只报「DB 有、ORM 没有」的**名字**
  ⇒ 只把 20 个名字补进 `__table_args__`，`Detected removed check constraint` 就归零了，
  但表达式少一个枚举值 / 比较方向写反，**alembic 不会报**。
- **drift 工具（`app/infrastructure/schema_drift.py`）根本不比较 CHECK 约束**
  ⇒ 补没补、补对没补对，它都是 exit 0。

⇒ **只靠 `alembic check` 验收 = 假绿**。本批必须另建表达式一致性守卫（§3）。

### 2.3 顺带发现的一处真实内容漂移

DB 的 `audit_log.ck_audit_log_action` 允许 `'RESTORE'`
（枚举顺序：`CREATE, UPDATE, DELETE, RESTORE, auth.login, …`），
ORM 的同名约束**漏了 `'RESTORE'`** ⇒ 这是一条**两边都存在、但表达式不同**的分歧，
上一批的「按名字比对」口径完全看不见它 —— 印证了 §2.2 的结论。

**这不是"表达式写错"而是"ORM 比 DB 宽松"** ⇒ 补 `'RESTORE'` 只会**收紧** ORM 侧声明，
而 DB 上本来就允许 `'RESTORE'` ⇒ 运行时校验行为不变。实证：

- `backend/app/services/ontology_service.py:615` 真实写入 `action="RESTORE"`
  （本体类软删恢复路径）⇒ `'RESTORE'` 是**在用**的取值，不是历史垃圾；
- `alembic/versions/0038_business_object.py:40` 用 `sa.CheckConstraint("code = UPPER(code)",
  name="ck_business_object_code_upper")` 建出 `ck_business_object_code_upper`
  ⇒ 该约束自 0038 起在每个库上都存在，本批补声明**不会新增任何写入拒绝**。

另 19 条新增约束全部照抄 `pg_get_constraintdef` 的实测定义（§3.1 表），
DB 侧早已存在 ⇒ 运行时行为不变。

### 2.4 候选方案对比

| 方案 | 做法 | 代价 / 风险 | 结论 |
|---|---|---|---|
| **A. Python 侧正则归一化后比字符串** | 自己写正则把 `col IN ('a','b')` 与 PG 的 `((col)::text = ANY ((ARRAY[...])::text[]))` 归一到同一形态 | ❌ PG 的渲染规则（类型提升、隐式 cast、`upper()` / `length()` 的重载解析）无法靠正则穷举；写得越复杂越容易**假绿**（把真分歧也归一成一样） | 否决 |
| **B. 只比名字** | 比 (表名, 约束名) 集合 | ❌ 只能消掉 `alembic check` 的告警，**表达式漂移照样隐形**（§2.2、§2.3） | 否决 |
| **C. 把 ORM 的表达式原样交给 PG 渲染，再逐字节比（采用）** | 临时表 `CREATE TEMP TABLE <probe> (LIKE public.<表>)`（`LIKE` 默认**不**复制 CHECK 约束）→ `ALTER TABLE <probe> ADD CONSTRAINT <name> CHECK (<ORM 表达式>)` → 读回 `pg_get_constraintdef` | 多一步临时表 DDL（测试库内、可控）；换来的是**语义级**比较 —— 类型提升、cast、`ANY(ARRAY[…])` 展开全部由 PG 负责 | **采用** |

### 2.5 最终决定

采用 **方案 C**，守卫落在 `backend/app/tests/unit/test_orm_check_constraints.py`，
三条用例：**DB → ORM**（不漏声明）、**ORM → DB**（不声明 DB 上没有的）、
**表达式一致性**（PG 规范化后逐字节比）。

---

## 3. 数据模型变更

**零生产 DDL、零 Alembic 迁移。** 下列 20 条 CHECK 约束**在 DB 上本来就存在**
（由历史迁移建出）⇒ 本批只把 ORM 侧的声明补齐，**运行时校验行为完全不变**。

> 唯一在数据库上执行的 DDL 是**守卫测试在测试库内建的会话级临时表**
> （`CREATE TEMP TABLE pg_temp._ckprobe_<tbl>`）—— 只存在于该测试会话，
> 连接结束即消失，**不进任何持久 schema**，且带 `pg_temp.` 前缀 + `IF EXISTS`
> ⇒ 裸名误删真表的风险已消除（见 §7.1 的 I-1）。
> **生产库（5433）全程只读**（只跑过 `alembic check` 与 drift 巡检）。

### 3.1 新增的 ORM `CheckConstraint`（13 张表 / 20 条）

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

### 3.2 修改的既有约束（表达式对齐）

| 表 | 约束名 | 改动 |
|---|---|---|
| `audit_log` | `ck_audit_log_action` | ORM 枚举列表**补上 `'RESTORE'`**（DB 有、ORM 没有 ⇒ 见 §2.3） |

⇒ ORM 侧命名 CHECK 约束：**28 → 48**，与 DB 侧数量与内容双向对齐。

### 3.3 表 / 列 / 索引

**无变化**（本批不动任何列、索引、外键）。

---

## 4. 接口契约变更

**无。** 本批不新增 / 修改 / 删除任何 API、DTO 字段、状态机。

---

## 5. 实现要点

### 5.1 关键文件

| 文件 | 动作 |
|---|---|
| `backend/app/domain/models.py` | 13 张表补 20 条 `CheckConstraint` + 修 1 条既有约束的枚举列表（2698 → 2786 行；文件头补一条 CHECK 约束约定说明，替代原先重复 13 次的逐条注释） |
| `backend/app/tests/unit/test_orm_check_constraints.py` | 新建（3 条用例） |
| `Harness/changes/fix-schema-check-constraints/summary.md` | 新建（本文件） |
| `Harness/changes/fix-schema-ssot-alignment/summary.md` | 上一批的遗留项状态更新 |
| `Harness/wiki/architecture.md` | 「Schema 漂移治理」章节新增 §3.1 + 更新纪律条目 |

### 5.2 守卫的核心算法（PG 规范化）

```
读 DB 侧：SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
          FROM pg_constraint WHERE contype='c' AND connamespace='public'

读 ORM 侧：Base.metadata 里 isinstance(constraint, CheckConstraint) 的 (表名, 约束名) → 约束对象

把 ORM 的表达式交给 PG 渲染（每个表一张临时探针表）：
    CREATE TEMP TABLE _ckprobe_<tbl> (LIKE public.<tbl>)
    -- LIKE 默认【不】复制 CHECK 约束（只有 INCLUDING CONSTRAINTS 才复制）⇒ 探针表是干净的
    ALTER TABLE _ckprobe_<tbl> ADD CONSTRAINT "<name>" CHECK (<ORM 表达式原文>)
    SELECT pg_get_constraintdef(oid) WHERE conrelid = to_regclass('_ckprobe_<tbl>')
    DROP TABLE _ckprobe_<tbl>

比较：两边都是 PG deparse 的规范形式 ⇒ 逐字节比
```

**关键前提**：`LIKE` 复制列的类型 / 长度 / NOT NULL ⇒ 探针表与真表的列类型同构 ⇒
PG 对同一份表达式解析出的表达式树相同 ⇒ 渲染结果可比。

**为什么不能用 Python 正则归一化**：见 §2.4 方案 A。

### 5.3 依赖注入点

无新增 DI。本批新增的测试用 `app/tests/_pg_support._newEngine()` 建引擎 ——
与 `backend/app/tests/unit/conftest.py` 的 `seedEngine` 夹具同一条路径
⇒ **自动执行 `alembic upgrade head`（幂等）**，测试文件对 schema 自足、不依赖外部先跑迁移。

**与上一批 `test_orm_schema_ssot.py` 的差别**：上一批用
`create_async_engine(resolveTestDatabaseUrl())` 直连，**不保证 schema 已到 head**
⇒ 本批改为走 `_newEngine()`，并在每条用例开头加**前置条件检查**
（`alembic_version` 必须落在代码 head 集合内，否则给一句人话而不是让下游断言报一堆
看不懂的名字差异）。

---

## 6. 测试

### 6.1 新增用例（`backend/app/tests/unit/test_orm_check_constraints.py`）

| 用例 | 断言 |
|---|---|
| `test_db_check_constraints_are_all_declared_in_orm` | DB 的每个命名 CHECK 约束，ORM 都声明了（**DB → ORM**） |
| `test_orm_declares_no_check_constraint_missing_in_db` | ORM 不得声明 DB 上不存在的约束（**ORM → DB**，否则 alembic 会提议 ADD） |
| `test_check_constraint_expressions_match_db` | 同名约束的**表达式内容**经 PG 规范化后逐字节一致 |

另有一条隐含守卫：ORM 侧任何 CHECK 约束**必须有名字**（无名约束对 alembic / drift 都不可见）。

### 6.2 TDD 证据（RED → GREEN）

- **RED**（`3b6b9b45`，只有测试文件）：**2 failed / 1 passed**
  —— `test_db_check_constraints_are_all_declared_in_orm` 失败并列出**恰好 20 个**缺失约束
  （与上一批 §8.6.2 的清单逐条一致）；
  `test_check_constraint_expressions_match_db` 失败并打印
  `audit_log.ck_audit_log_action` 的 ORM / DB 两份 `pg_get_constraintdef` 原文（差异 = 缺 `'RESTORE'`）。
- **GREEN**（`18fda93f`，补完 ORM）：**3 passed**。

### 6.2.1 「守卫不是假绿」的变异取证（review 后补做）

守卫本身如果恒绿，等于什么都没钉 ⇒ 对 ORM 做**三处故意变异**，逐条确认测试转红：

| 变异 | 预期 | 实测 |
|---|---|---|
| `ck_kpi_catalog_status` 的枚举去掉 `'DEPRECATED'` | 表达式不一致 | ✅ `test_check_constraint_expressions_match_db` 转红，并打印 ORM / DB 两份原文 |
| `ck_audit_log_action` 的枚举去掉 `'RESTORE'` | 表达式不一致 | ✅ 转红 |
| 整条 `ck_kpi_catalog_status` 从 ORM 删掉 | DB→ORM 缺声明 + 探针渲染集合不等 | ✅ `test_db_check_constraints_are_all_declared_in_orm` 与 `test_check_constraint_expressions_match_db` 双双转红 |

三次变异后均已还原 ⇒ 复核为 **3 passed**。

### 6.3 回归

| 范围 | 结果 |
|---|---|
| `test_orm_check_constraints.py` | **3 passed** |
| `test_orm_schema_ssot.py` + `test_domain_model_registration.py` + 触及模型的业务单测（business_object / data_lineage / document / entity_mapping / kpi_catalog / wiki_link / sync_entity_mapping）+ `test_check_schema_drift.py`（integration）+ `test_user_orm_matches_schema.py`（integration） | **148 passed（unit）+ 42 passed（integration）** |

> ⚠️ **后端全量 unit 套件不可作门禁**（本地裸跑 27min / 28 红基线全在 `test_refine_shortcuts.py`；
> CI 上 `backend-unit` job 实测 **42 分钟 / 17 红**，`timeout-minutes: 45` ⇒ 余量仅 ~7%，
> 详见「后续项」第 7 条与 §10.2）
> ⇒ 只跑定向（本批即如此）。新增文件放 **`unit/`** 而非 `integration/`：
> CI 的 integration 分片 = `find app/tests/integration -name 'test_*.py' | sort` 后按**下标 mod 4** 取
> ⇒ 在 `integration/` 新增文件会让其后所有文件的分片归属整体轮转一位（见 §8.3 与
> [`Harness/rules/测试规范.md`](../rules/测试规范.md) 的「⚠️ 目录分片稳定性」条）。

> ⚠️ **本文件继承了 `unit/conftest.py` 的 autouse 截断**（`warmBusinessObjectRegistry` →
> `dbSession` → `seedEngine` → `_truncateAll`，每个用例 TRUNCATE 共享测试库 5434）。
> 本文件只读 `pg_constraint`、不写业务数据 ⇒ TRUNCATE 对断言无影响；
> 但「共享库被并发套件截断」是**仓库级遗留项**（见「后续项」第 5 条），
> 本批不引入新的 fixture 变体，避免为一条只读守卫另开一条夹具路径。

### 6.4 覆盖率

新增文件为**纯守卫测试**（无生产逻辑分支）；被测的生产代码路径（ORM 元数据）由既有
`test_orm_schema_ssot.py` 覆盖。⇒ 覆盖率要求由既有套件承担，本批未引入新的未覆盖分支。

---

## 7. 安全审查

**已触发** `security-reviewer` 与 `code-reviewer`（对本分支 diff 全量）。触发理由：
新增测试会拼接 SQL 并在数据库上执行 DDL（临时表）。

### 7.1 审查结论

**security-reviewer**：**无 Critical**；1 × High、4 × Medium、7 × Low。
**code-reviewer**：**有条件通过** —— 合并前必须落 I-1（探针名 `pg_temp.` 限定）与
I-2（渲染集合硬断言）。

#### 已落地的修复

| 编号 | 级别 | 问题 | 处置 |
|---|---|---|---|
| **I-1** | High（安全 & 代码评审共同） | 探针表名是**裸名** `_ckprobe_<tbl>`：① `DROP TABLE IF EXISTS <裸名>` 按 `search_path` 解析，万一 `public` 下存在同名表会**误删那张真表**；② 结尾的 `DROP TABLE` 不带 `IF EXISTS`，中途失败会留下残留表（**实际发生过**：残留的 `public._ckprobe_business_object` 污染了 `alembic check`，报出一条假的 `remove_table`） | 探针名改为 `pg_temp._ckprobe_<tbl>`；两处 DROP 全部写成 `DROP TABLE IF EXISTS pg_temp.…`；DDL 走 `try/finally` 保证清理；**残留表已清**（仅测试库 5434，已核对 0 个 `_ckprobe%` 残留） |
| **I-2** | High（代码评审） | 「探针没渲染出来 ⇒ `continue` 静默跳过」= 假绿洞 | 比较前加**硬断言** `set(rendered) == set(ormConstraints)`；并把 `ormDefinition is None` 分支从「跳过」改为**显式记一条 mismatch** |
| **M-1 / L3** | Medium / Low | 注入护栏用 `assert`（`python -O` 下会被剥掉）；只挡 `;` / `$$` | 改为**显式 `raise ValueError`**；`_FORBIDDEN_IN_EXPRESSION` 扩到 `;` `--` `/*` `*/` `$$` |
| **M-2** | Medium | 13 处重复的逐条注释（噪声） | 收敛成**文件头一条** CHECK 约束约定说明（含"alembic 只比名字不比表达式"这条事实 + 守卫文件路径） |
| **M-3** | Medium | 测试文件对 schema不自足（不保证库已到 head） | 改用 `_pg_support._newEngine()`（内部调 `_ensureSchema` → `alembic upgrade head`） |
| **M-5** | Medium | 表达式渲染未开 `literal_binds` | `_renderExpression` 加 `compile_kwargs={"literal_binds": True}` |
| **L1** | Low | 变更记录里写了**明文 DSN** | 改为占位形式（`<PG 测试库 DSN>`），口令不落文档 |
| **L5** | Low | 库没到 head 时，断言会报一堆看不懂的名字差异 | 加**前置条件检查**（`alembic_version` 必须在代码 head 集合内，否则给一句人话） |
| **M-4** | Medium | 本文件继承了 `unit/conftest.py` 的 autouse 截断（TRUNCATE 共享库 5434） | **显式文档化**（本文件 docstring + §6.3 尾注 + 「后续项」第 5 条）⇒ 属仓库级遗留项，本批不另开夹具路径 |

#### 复审后的收尾修复（commit `06eb0591`，合并前最后一批）

scoped 复审结论 = **APPROVE**（前 8 条全部 CLOSED），仍留下 1 Medium + 3 Low，已全部修正：

| 编号 | 级别 | 问题 | 处置 |
|---|---|---|---|
| **N-1** | Medium | `ALTER TABLE` 失败后 PG 把事务标记为 aborted，`finally` 里的 `DROP` 随即报 `current transaction is aborted` ⇒ 这个**误导性错误会顶替掉真正的根因**（如 `UndefinedColumnError`） | `except` 分支先 `await conn.rollback()` 让原始异常冒泡；`finally` 的清理单独 `try/except` 吞掉（探针表只在会话 `pg_temp` 里，连接关闭即消失）⇒ 实测引用不存在列时直接报 `UndefinedColumnError: column "nonexistent_col_xyz" does not exist` |
| **N-2** | Low | 未使用的 `create_async_engine` 导入（ruff F401） | 删除 |
| **N-3** | Low | 标识符白名单用 `^…$` —— Python 的 `$` 还匹配「结尾换行符之前」，`'ok\n'` 会被放行 | 改成 `\A…\Z` |
| **N-5** | Low | 探针侧查询未过滤 `contype` ⇒ PG 17 把 NOT NULL 搬进 `pg_constraint` 后会以「缺少 []」的迷惑消息炸出来 | 补 `AND c.contype = 'c'`，与 DB 侧查询对称 |

#### 仍然有效的既有护栏（本次一并复用 / 强化）

- **标识符白名单**：表名 / 约束名必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`，
  规则与 `app/services/data_quality_evaluators/_common.py:64` 的 `validate_identifier` 一致
  （同一套仓库惯例；本文件自带实现而不跨模块 import，避免把结构守卫耦合到业务评估器）。
- **DDL 走 `exec_driver_sql`（原始 SQL）** ⇒ 表达式里的 `:` 等字符不会被 SQLAlchemy
  当成 bindparam 占位符改写。
- **PG 自己渲染**（§2.4 方案 C）⇒ 不做 Python 侧正则归一化，杜绝"归一化本身把真分歧抹平"。

---

## 8. 部署验证

### 8.1 本批不部署

零 DDL、零生产写、不新增 Alembic 迁移 ⇒ ORM 侧改动**随下次常规发版生效**即可。

### 8.2 `alembic check`（alembic 1.19.1，只读）

| 口径 | 本批前 | 本批后 |
|---|---|---|
| 5434 `qa_metadata_test`：`Detected removed check constraint` | **20** | **0** |
| 5434 `qa_metadata_test`：实质差异行数（不含 78 条序列提示） | **57** | **37** |
| 全新迁移库 `qa_metadata_test_ck`：`Detected removed check constraint` | 20 | **0** |
| 全新迁移库：实质差异行数 | 56 | **36** |
| 5433 生产同构库（只读）：`Detected removed check constraint` | — | **0** |

> 两个口径的差额恒为 1 = 共享测试库上 `po_lines_sandbox` 这张测试残留表的 `removed table`。

**⚠️ 取证过程中的一次污染与清理（I-1 的实证）**：早期版本的探针表用了**裸名**
`_ckprobe_business_object`，某次会话异常结束后在**测试库 5434** 留下一张空表
⇒ `alembic check` 报出一条**假的** `remove_table`（表结构 `id integer PK` / `v text`、0 行）。
已 `DROP TABLE IF EXISTS public._ckprobe_business_object`（**仅测试库**、已核对 0 行），
并把探针改为 `pg_temp.` 限定 + `IF EXISTS`（§7.1 I-1）⇒ 上表数字是**清理后**的实测值。
**生产库（5433）全程只读，未产生任何此类残留。**

### 8.3 drift 门禁

- 全新迁移库：`python -m app.infrastructure.schema_drift --database-url … --strict` ⇒ **exit 0**
  （`Schema drift 检查通过：ORM 与 DB 一致（表 / 列 / 索引）`）。
- 5434 共享测试库：输出不变，仅剩 `[db:table] po_lines_sandbox`（测试残留，属测试卫生，另案）。

### 8.4 真实数据验证脚本（只读、幂等）

```bash
cd backend
# DSN 用占位符；实际口令走环境变量，不落文档
TEST_DATABASE_URL='postgresql+asyncpg://<user>:<password>@<host>:5434/qa_metadata_test' \
  python3 scripts/fix_schema_ssot_realdata.py --database-url "$TEST_DATABASE_URL"
```

输出（5434 共享测试库）：

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

（`extra_table = 1` = `po_lines_sandbox`，脚本白名单内。）
在**全新迁移库** `qa_metadata_test_ck` 上同一脚本 **exit 0**，`extra_table = 0`。

### 8.5 docker compose 冒烟

**本批不做** —— 无 DDL、无生产写、ORM 改动不改变运行时行为；
冒烟留到下次常规发版统一做（上一批同款处理，PR #97 合并后亦未部署）。

---

## 9. 关联

- **关联变更（predecessor）**：[fix-schema-ssot-alignment](../fix-schema-ssot-alignment/summary.md)
  —— 上一批的 §8.8.1 遗留项，本批兑现。
- **Wiki**：[`Harness/wiki/architecture.md`](../wiki/architecture.md) §3「Schema 漂移治理」
  —— 新增 §3.1「CHECK 约束：补齐了，但『只比名字』这件事仍是坑」，并更新 §4 纪律条目。
- **Rules**：[`Harness/rules/测试规范.md`](../rules/测试规范.md)
  —— 「⚠️ 目录分片稳定性」条（上一批沉淀，本批沿用：测试文件放 `unit/`）。
- **Rules**：[`Harness/rules/变更记录强制规范.md`](../rules/变更记录强制规范.md) —— 本文件按其 9 段模板填写。
- **Memory**：`~/.claude/projects/.../memory/qa-system-schema-check-constraints.md`（新）
  + `qa-system-schema-ssot-alignment.md`（上一批遗留项状态更新）+ `MEMORY.md` 索引加 1 行。

---

## 10. 合并与发布记录

**合并**：PR **#98** → **squash** → `main` commit **`29a63b80`**（2026-11-02T05:54:00Z），
父提交 `9be8e13a`。分支 `fix/schema-check-constraints`（3 个 commit）已删（远端 + 本地）。

### 10.1 CI 判定（失败集合对差，不等 CI 变绿）

main 基线 run `41376713507`（`9be8e13a`）vs 本 PR run `41387329353`（`06eb0591`）：

| job | main 基线失败数 | PR 失败数 | 净新增 | 由红转绿 |
|---|---|---|---|---|
| integration shard 0 | 17 | 17 | **0** | 0 |
| integration shard 1 | 37 | 37 | **0** | 0 |
| integration shard 2 | 22 | 22 | **0** | 0 |
| integration shard 3 | 33 | 33 | **0** | 0 |
| unit | 17 | 17 | **0** | 0 |
| frontend | — | success | — | — |
| **并集** | **126** | **126** | **0** | **0** |

unit 套件整体耗时也几乎一致（基线 `17 failed, 5418 passed, 1 skipped in 42:22`
vs 本 PR `17 failed, **5421** passed, 1 skipped in 42:17`）⇒ **+3 passed 正是本批新增的 3 条用例**，
失败集合逐条相同、无一进出一出。

### 10.2 unit job 首次撞 45 分钟超时（已复跑，非回归）

本 PR run 的 **unit job 第一次被取消**：`backend-unit` 的 `timeout-minutes: 45`，
而该套件基线就要跑 **42 分钟**（余量仅 ~7%）。首轮跑到 **82%** 被取消（`##[error]The operation was canceled.`），
job 结论 = `cancelled`（不是 `failure`）。

**判据：这是 runner 变慢，不是本批引入的回归**，取证两条：

1. **失败标记逐字符对齐**：把两次的 pytest 进度标记流（`.` / `F` / `s` 拼接）拉平比较，
   base 的 17 个 `F` 位置 = `[1307, 1310, 1311, 1314, 1406, 1410, 1685, 2008, 2009, 2068, 2649, 2985, 2988, 3853, 4291, 4292, 4293]`，
   PR 的 = `[1307, 1310, 1311, 1314, 1406, 1410, 1685, 2008, 2009, 2068, 2649, 2985, 2988, 3853, **4294, 4295, 4296**]`
   ⇒ 前 15 个**完全一致**，后 3 个整体**偏移 +3**（正好 = 本批新增的 3 条用例插在 ~79% 处）
   ⇒ 取消之前**没有任何新增失败**。
2. **慢是全局的、不是某一段**：每 72 个用例的耗时从基线的 ~32s 变成 ~41s（**+28%**），
   且**从第 1% 就开始慢**（首块 53s → 64s）⇒ 与本批新增文件的位置（~79%）无关。

**处置**：用 `POST /repos/sunql/QA/actions/jobs/<job_id>/rerun` **单独复跑 unit job**
（run attempt 2，43 分钟跑完 100%，结论 `failure` 与基线同）⇒ 拿到完整失败集合做上表对差。
（`concurrency: cancel-in-progress: true` ⇒ 推送新 commit 会自动取消上一轮 run，这是首轮被取消的**另一半原因**
—— 上一个 run `41384248067` 同样被标 `cancelled`。）

⚠️ **这条超时脆弱性是仓库级遗留项**，已登记进「后续项」第 7 条：unit 套件耗时已经贴着 CI 的 45 分钟上限，
任何 runner 抖动都会把 job 打成 `cancelled`（**而不是 `failure`**）⇒ 做失败集合对差时，
**`cancelled` 的 job 必须复跑**，否则该 job 的失败集合是空的，会造成「净新增 0」的假象。

### 10.3 删分支前的内容级仲裁

`origin/main^{tree}` 与分支 tip `06eb0591^{tree}` 均为 **`04fe39eb20cfd9b941ac8271834a204964c831b5`**
（字节级一致）、`git diff --stat` 为空 ⇒ squash 未丢内容 ⇒ 删分支安全。

### 10.4 部署

**本批不部署**（零 DDL、零生产写）—— ORM 侧改动随**下次常规发版**生效。
这 20 条约束 DB 上本来就存在 ⇒ 运行时校验行为**完全不变**；唯一新增的 DDL 是测试库内的会话级临时探针表。

### 10.5 评审

最终 scoped 复审 = **APPROVE**（0 Critical / 0 Important）；复审后追加的 1 Medium + 3 Low 已全部修正
（commit `06eb0591`，见 §7.1 末表）。

---

## 11. 残余差异的运行时影响评估（2026-11-02，只读）

> 对应「后续项」第 1 条。用户要求：**先确认有没有运行时影响；没有就记录下来、提醒排期**。
> 本节是那次评估的结论，**本批未做任何实现**。

### 11.1 评估口径

- 对象：5433 生产同构库（**只读**）上 `alembic check` 实测报出的 **40 条**差异
  （全新迁移库上是 36 条；差额来自各库自身的历史状态，如备份表、外键是否真的建上）。
- 方法：逐条回到**代码**里找「今天会不会报错」，而不是只看 DDL 文本。
- alembic 消息方向已核实（读 `alembic/autogenerate/compare/constraints.py` 与 `types.py` 源码）：
  - `Detected type change from X to Y` ⇒ **X = DB 侧类型，Y = ORM 侧类型**；
  - `Detected NOT NULL on column` ⇒ **ORM 侧是 NOT NULL**（DB 侧可空），
    方向与直觉相反，别读反。

### 11.2 结论：**对运行时无影响**（今天不改一行代码也不会报错）

| 组 | 条数 | 判定 | 依据 |
|---|---|---|---|
| **A** 唯一索引 ↔ 唯一约束 | 6 组 | **无影响** | 全仓唯一的 `ON CONFLICT ON CONSTRAINT` 是 `scripts/seed_rbac.py:113` 的 `constraint="user_roles_pkey"`（PK，无关）；其余 upsert 一律用 `index_elements=[...]`（列推断，索引/约束都认）；unique index 与 unique constraint 都报 `unique_violation`；NULL 语义相同；drift 工具按列集合认领（`app/infrastructure/schema_drift.py:268-277`） |
| **B** 列类型变更 | 7 | **6 条无影响**；`entity_type`、`compile_task_id` 属**收窄方向**，但生产同构库上 `document_entity_relation` 与 `wiki_token_usage` 均为**空表**（实测 `entity_type` 最大长度 0）⇒ 现在收窄也不炸 | `entity_type` 有 FK `fk_document_entity_relation_entity_type` → `business_object.code VARCHAR(20)` + Pydantic `len(code) > 20` 校验 ⇒ 存不进 >20 的值；`compile_task_id` 恒为 `wiki_compile_task.id`（BIGSERIAL 从 1 起） |
| **C** `l3_acl.granted_at` NOT NULL | 1 | **无影响** | 方向是 **ORM=NOT NULL / DB=可空**；`l3_acl` 在生产同构库上 **0 行**；且**全仓没有任何代码往 `l3_acl` 插入**（只有 `app/services/document_clearance.py:36` 读一行） | 
| **D** 外键 | 3 | **无影响** | ORM 多 2 条（`wiki_compile_task.selected_model_id` / `fallback_model_id` → **`llm_config.id`**，非 `wiki_compile_task.id`）；DB 多 1 条（`wiki_token_usage.compile_task_id` → `wiki_compile_task.id`，**`ON DELETE SET NULL`**）。`llm_config` 只有**软删除**（`app/api/v1/model_config.py:69-76`）；`wiki_compile_task` **没有任何删除路径**（无 DELETE 路由，只有 `scripts/reset_business_data.sql` 的 `TRUNCATE … CASCADE`）⇒ 缺 FK / 有 FK 都不会炸 | 
| **E** 索引排序方向（DB 有 DESC、ORM 无） | 10 | **无影响**（只影响 planner） | drift 工具读 `pg_index.indkey`（**不含方向**）⇒ 启动门禁根本看不见这 10 条；单列索引 ASC/DESC 等价（PG 可反向扫描），多列索引丢了 DESC 最多是计划变差 | 
| **F** 列注释 | 6 | **无影响** | 纯元数据；唯一读注释的代码 `app/services/schema_introspection_service.py:629-648` 打的是**外部业务库**，不是本库元数据表 |

### 11.3 真正要 DDL 的只有 6 条；**另有 16 条零 DDL 可清**

- **零 DDL 可清（性价比最高，但本批不做）**：
  - **E 的 10 条** ⇒ ORM 侧改成仓库**既有惯例** `Index("ix_x", sa_text("col DESC"))`
    （样板见 `app/domain/wiki_learning_models.py:200-204`、`app/domain/mcp_models.py:86-98`）。
    ⚠️ 该写法会被 drift 工具与 `test_orm_schema_ssot.py` 跳过（表达式索引不可比）⇒ 改完 `alembic check` 归零，
    但**换来的是「表达式索引失去列序守卫」**——这是取舍，不是纯收益。
  - **F 的 6 条** ⇒ ORM 侧补 `comment="..."`（纯元数据注解，无 DDL）。
- **必须 DDL 的 6 条**：`sample_pk_values` JSONB→JSON（⚠️ 迁移必须同时把
  `server_default` 从 `'[]'::jsonb` 改成 `'[]'::json`，否则 PG 报
  *"default for column cannot be cast automatically to type json"*）、
  `entity_type` 收窄、`l3_acl.user_id`/`granted_by` 放宽、`compile_task_id` 收窄、
  `l3_acl.granted_at` 加 NOT NULL、`wiki_compile_task` 两条 FK。

### 11.4 后续 DDL 的两个已知陷阱（写迁移时必须处理）

1. **A 组若改成「ORM 为真」** ⇒ 需要 `op.drop_index` + `op.create_unique_constraint`，
   而 **6 个既有迁移的 downgrade 里已有 `op.drop_index(<同名>)`**（`0020`、`0021`、`0023`、`0027`、`0097`）
   ⇒ 回滚会报 "index does not exist"。**改成「DB 为真」（ORM 声明 `Index(unique=True)`）则零 DDL。**
2. **两个 partial unique 索引绝不能改成约束**：`uq_knowledge_conflict_open`
   （`app/domain/wiki_learning_models.py:379-391`）、`uq_structure_suggestion_pending`
   （同文件 `:446-452`）—— 它们的 `ON CONFLICT` 推断依赖 partial 谓词，
   改成约束会直接报 *"no unique or exclusion constraint matching"*。

---

## 明确不做 / 边界

- ❌ **不写 Alembic 迁移**、**不改任何生产 DDL**、零生产写
  （测试库内仅会话级临时探针表，见 §3 尾注）。
- ❌ **不清理**备份表 / 业务表（`BPSUPPLIER`、`PORDER`、`PORDERQ`、`*_20260930`、
  `ontology_class_pre_pruning_20260918`）。
- ❌ **不碰**其余 36 条残余差异（9 changed index（排序方向）、6 条唯一约束 ↔ 唯一索引形状差异、
  1 条新增唯一约束 `uq_doc_entity_rel`、7 条列类型变更、1 条 NOT NULL、6 条列 comment）——
  它们需要 DDL + 数据迁移评估，属另一个项目。
- ❌ **不拆** `app/domain/models.py`（本批 2698 → 2786 行）。
- ❌ **不修** `po_lines_sandbox` 测试残留。

### 「禁止 autogenerate」这条硬规矩依然成立，但理由已收窄

上一批的理由是「autogenerate 会为这 20 个约束生成 `op.drop_constraint('ck_*')`」
⇒ **这一条地雷已拆**（`ck_*` 的 DROP 提议不再出现）。

但**禁止 autogenerate 仍然有效**，因为残余 36 条差异里仍有**破坏性**提议：

- 7 条列类型变更 ⇒ autogenerate 会生成 `ALTER COLUMN … TYPE`（数据可能被截断 / 转换失败）；
- 6 条唯一约束 ↔ 唯一索引的形状差异 ⇒ 会生成 `DROP CONSTRAINT` + `ADD CONSTRAINT`；
- 9 条 changed index ⇒ 会生成 `DROP INDEX` + `CREATE INDEX`。

⇒ **在残余差异归零之前，`alembic revision --autogenerate` 仍然禁止**；
迁移必须**手写**、逐条对照 DB 实测，不要依赖 autogenerate。

---

## 后续项

1. **`alembic check` 的 36 条残余差异**（需 DDL + 数据迁移评估）⇒ 另立项目。
   ✅ **运行时影响已评估完毕（2026-11-02，见 §11）：对运行时无影响**（今天不改一行代码也不会报错；
   收窄方向的那几条在生产同构库上对应表**均为空表**）。
   ⇒ **可安全排期**。拆成两件事：
   ① **零 DDL 的 16 条**（10 条索引 DESC + 6 条列注释）⇒ 只改 ORM；
   ② **必须 DDL 的 6 条**（见 §11.3）⇒ 手写迁移，**禁止 autogenerate**，
      且必须处理 §11.4 的两个陷阱。
   ⚠️ ①不是纯收益：把 DESC 索引改成 `sa_text("… DESC")` 后，drift 工具与
   `test_orm_schema_ssot.py` 会**跳过**这些表达式索引 ⇒ 失去列序守卫。
   ✅ **已兑现（2026-11-09）**：[fix-alembic-residual-36](../fix-alembic-residual-36/summary.md)
   —— fresh 库 36 → **0**，`schema_drift --strict` exit 0；新增
   `test_orm_expression_indexes.py` 补回表达式索引守卫（含变异取证）。
2. **`app/domain/models.py` 已 2786 行**（超 800 行上限 3.5 倍）⇒ 拆分另案。
3. **表达式索引的列序没有自动化覆盖**（`ix_wiki_import_task_created` 的列序在 drift 与
   `test_orm_schema_ssot.py` 里双双被跳过）⇒ 补一条针对该索引的专门断言。
4. **门禁脚本自身没有自动化测试**（`backend/scripts/fix_schema_ssot_realdata.py` 的 4 条退出码路径
   靠人工实跑取证）。
5. **`unit/` 与 `integration/` 的 autouse 夹具都 TRUNCATE 共享测试库 5434** ⇒ 测试卫生问题，
   根治方向是给只读结构类用例一个不继承截断型夹具的独立作用域。
   本批的守卫文件已**显式文档化**这条继承关系（docstring + §6.3 尾注），
   但**未**为它单开一条夹具路径（避免为一条只读守卫引入与 `unit/conftest.py` 分叉的夹具）。
6. **守卫的覆盖口径已核对完整**：5434 上 `public` schema 的 CHECK 约束**共 48 条**，
   且**全部**是显式命名的 `ck_*` / `chk_*`（无 PG 自动命名、无未覆盖项）⇒
   守卫覆盖 = DB 侧 CHECK 约束的 **100%**。
   口径：`pg_constraint.contype='c'` 且表在 `public` schema。
7. **CI 的 `backend-unit` job 已贴着超时上限**：`timeout-minutes: 45`，而该套件基线实测 **42 分钟**
   （余量 ~7%）⇒ runner 抖动 10% 就会把 job 打成 **`cancelled`**（不是 `failure`），
   失败集合为空 ⇒ 失败集合对差会误判成「净新增 0」。
   ⇒ ① **对差时凡 `cancelled` 的 job 必须复跑**再取集合；
   ② 根治方向：把 unit 套件按目录拆成多个 job（与 integration 的分片同理），
   或把 `backend-unit` 的 `timeout-minutes` 放宽 —— 属独立立项，本批只登记不实施。
   ✅ **② 已实施（2026-11-02）**：`backend-unit` 改为 **4 个分片 job**
   （路径 sha256 稳定哈希分片，见
   [`fix-ci-unit-sharding`](../fix-ci-unit-sharding/summary.md)），`timeout-minutes` 30、
   `fail-fast: false`、每个分片跑完打印 `UNIT_SHARD_DONE shard=N`
   ⇒ 缺该标记即说明该 job 被取消，**其失败集合不可参与对差，必须复跑**。
