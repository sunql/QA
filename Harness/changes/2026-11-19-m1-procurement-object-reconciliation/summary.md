# 变更：2026-11-19-m1-procurement-object-reconciliation（M1-1.4 采购本体对象核对与补齐）

- **日期**：2026-11-19
- **作者**：Claude
- **Phase**：M1-1.4（V0.3 落地计划 §1.4）
- **状态**：done（代码 + 测试全绿；**未合并 / 未部署**）
- **关联变更**：[[M1-1.3 采购域发布编排]]（`bf2c784f`）、[[M1-1.1 采购源绑定行为层]]（`79a2a9da`）
- **迁移版本**：`0158_procurement_object_reconciliation`（数据迁移，纯 DML）
- **分支**：`feat/procurement-m0`　**未合并 / 未部署**（用户决策：生产暂不部署，功能全完成 + 本地全绿后统一发版）

---

## 1. 需求

计划 §1.4：「**采购本体对象核对与补齐**：10 个对象逐条对现状
（CONTRACT 源缺口须给出处置：建类但无源 / 用文档目录代替 / 明确不建）」（1–1.5 人日）。
依据 V0.3 详细设计方案 §3.2「本体对象定义建议」列出的 10 个采购本体对象：
SUPPLIER / MATERIAL / PURCHASE_REQUEST / QUOTATION / PURCHASE_ORDER / RECEIPT /
CONTRACT / INVOICE / PAYMENT / SUPPLIER_PERFORMANCE。

**验收标准（本批自定，可复核）**

1. 10 个对象**逐条**有处置（穷举 6 种取值，不允许「漏项 / 语焉不详」）；
2. CONTRACT 的源缺口**有明确处置**，且写清替代承载面与「首期是否可查」；
3. 补齐后**采购域切片能看见采购 KPI**（不是「类复活了」就算完）；
4. 补齐动作**可幂等重跑、可降级**，且降级的不对称处**写清**；
5. 迁移在 Lineage A 环境（测试库 5434）**整批 no-op**（不炸、不留半截状态）；
6. 决策 SSOT（代码）与迁移**不会静默分叉**（有守卫 + 变异反证）。

---

## 2. 设计评审

### 2.1 关键发现：这不是「补数据」，是「**口径不丢**」

核对初期以为 M1-1.4 只是「对清单、缺啥补啥」。实测发现真正的病灶在**指标口径**上：

| 事实 | 证据 |
|---|---|
| 12 个 `ontology_metric` 的 `target_class_id` **全为 NULL** | 开发库 5433 直查 |
| 指标归属只能靠 `ontology_metric_source`，而它指向的 9 个采购类里 **7 个是墓碑** | `valid_to=2026-09-19 04:04–04:05` |
| 编译器 `groupMetricSources` 对**墓碑类**的源**整条丢弃** | `semantic_compiler_sections.py` |
| ⇒ 18 条指标源**只保留 3 条**（丢 15），9 个采购指标的 `sources` **全为空** | `evidence/procurement-domain-slice-before.txt` |
| ⇒ 指标归不了属 ⇒ 被域切片 **fail-closed 排除** ⇒ 采购域切片只剩 **1 指标 / 1 KPI** | 同上 |

⇒ 「复活 7 个墓碑类」从「补数据」升级为**硬需求**。
更关键的是**复活的方式**：见 §2.2。

### 2.2 关键取舍 1：墓碑类怎么复活

| 方案 | 结论 |
|---|---|
| **A. 原地复活**（清 `valid_to`，`class_id` 不变）（采纳，决策 **D-13**） | 指标源按 `class_id` 引用 ⇒ 原地复活 ⇒ 源**仍然有效** |
| B. 新建 v2 行 + 把指标源改指新 id | 换汤不换药：**得同时改所有源引用**（15 条），漏一条就是口径静默缺失；且 v2 行的 `class_id` 与生产库快照、审计、`entity_mapping` 全不一致 |
| C. 只改指标源（绑到别的活跃类） | 需**重写 measure**（A8 Q3 已判「成本高且无替代表」）；且 `_DTL` 族缺 `PROMISED_RECEIPT_DATE` / `ORDER_RQTY` / `RECEIPT_DATE` 等关键列 ⇒ 改绑即断链 |

### 2.3 关键取舍 2：INVOICE 的头类指错了

BO `INVOICE` 现指 `DWD_AP_INVOICE_DTL` —— 实测该表**只有 46 行**，
属性是 `FILE_ID / FILE_URL / FILE_SUFFIX / EXTRACT_TIME / TENANT_ID`，
时间跨度仅 **2026-09-02 → 2026-10-09** ⇒ 它是**文件抽取旁表**，不是发票主体。
真正的采购发票在 `DWD_PURCHASE_INVOICE`（38,323 行，头）/ `_LINE`（2,486,311 行，行）。

⇒ **REPOINT**（决策 **D-15**）：不新建 BO，只把 `header_class_id` 改指。

### 2.4 关键取舍 3：`PO` 与 `POL` 只能取一

`business_object` 上有 `uq_business_object_header_class`（一个类只能挂一个 BO）
⇒ 「补 `POL`（订单行）」与「把 `PO` 改指到订单行」**互斥**。

| 方案 | 结论 |
|---|---|
| **A. 补 `POL` → `DWD_PURCHASE_ORDER_LINE`；`PO` 仍指宽表 `_DTL`**（采纳，**D-16**） | 与 `docs/data-knowledge/采购域.md` 目录一致（目录里 `PO` 与 `POL` 本就是两个对象）；宽表继续承载「订单级」问答，订单行承载 6 个 KPI 的口径锚点 |
| B. `PO` 改指订单行，不建 `POL` | 订单级问答失去宽表锚点（宽表 1,023,442 行 vs 行表 927,648 行，且宽表带供应商/交期等头字段） |

### 2.5 关键取舍 4：CONTRACT 的三种处置（计划原文点名）

| 方案 | 结论 |
|---|---|
| A. 建类但无源 | ⛔ V0.3 明写「**不虚构源表**」；建一个无源类会让「有类 = 有数据」的错觉成立 ⇒ **比不建更坏** |
| **B. 用文档目录代替**（采纳，**D-12**） | `document_catalog.document_type='CONTRACT'` + `document_entity_relation.relation_type='CONTRACT'`（枚举 `DocumentType.CONTRACT` / `DocEntityRelationType.CONTRACT` **都已存在**）⇒ 承载面在架构上就位，合同文档入库即生效 |
| C. 明确不建 | 部分采纳：**本体对象不建**，但登记「承载面在文档目录 + **实测 0 份合同文档、0 条关联 ⇒ 首期不可查**」⇒ 不假装支持 |

### 2.6 关键取舍 5：A8 Q6「补 ODS 收货 / 请购类」的建议**撤销**

A8 §7 曾建议 M1-1.4 补 `ODS_PRECEIPT*` / `ODS_PREQUIS*` 等 5 个类。
**本批不补**，理由：① 该建议写于 M0，当时**还没有绑定表**；现在 D2 两张表的
`source_table` 注释就是「**物理表名（可 ≠ class_name）**」⇒ 类可直接绑到 ODS 表，
**不需要**为每张 ODS 表造同名类；② 41 类绑定范围里**不含**这 5 个类 ⇒ 建出来是**孤儿类**；
③ 收货 / 请购**已各有 DWD 级类** ⇒ 对象覆盖度不缺。

---

## 3. 数据模型变更

### 3.1 迁移 0158（`backend/alembic/versions/0158_procurement_object_reconciliation.py`）

**纯 DML，无 DDL**。三个动作，全部防御式锚定（按**类名 + 存在性**，不按 id）：

| 动作 | SQL 形态 | 守卫 |
|---|---|---|
| 复活 7 个类 | `WITH old AS (SELECT id, valid_to … WHERE class_name=:n AND valid_to IS NOT NULL), upd AS (UPDATE … FROM old … RETURNING id) INSERT INTO journal …` | 已复活的行不再改（幂等）；**不按 `version` 过滤**（墓碑行可能不是 v1 ⇒ 按版本锚定会静默 0 行）；先拍快照 ⇒ 原墓碑时间戳落 journal |
| 插入 4 个 BO | `WITH inserted AS (INSERT … RETURNING code, header_class_id) INSERT INTO journal …` | 头类缺失 ⇒ 整行不插（不留半截）；BO 已存在 ⇒ 不重复插；头类已占用 ⇒ 不插（防撞唯一索引）。⚠️ `business_object` **主键是 `code`，没有 `id` 列** ⇒ `RETURNING id` 直接报 `UndefinedColumnError` |
| 改指 INVOICE | `WITH old AS (快照 code/header_class_id/graph_label), upd AS (UPDATE … RETURNING code, t.id) INSERT INTO journal …` | 已改指 ⇒ 不再改（幂等）；目标头类已被别的 BO 占用 ⇒ 不改；**原 graph_label 落 journal**（降级读回，不靠类名猜） |

⚠️ **所有参数显式 CAST**：asyncpg 会按参数在语句里的用法推断类型，
同一参数既出现在 SELECT 输出（推断 `text`）又出现在等值比较（推断 `varchar`）
⇒ `AmbiguousParameterError` ⇒ **迁移整体失败**（不是「某个分支不生效」）。

⚠️ **迁移自包含**：只 import `sqlalchemy` / `alembic`，**不 import `app.*`**
（生产回滚时不能依赖应用代码）；与决策目录的一致性由单测的**漂移守卫**钉住。

### 3.2 变更日志表（`procurement_object_reconciliation_journal`）

本批**不是纯 DML**：迁移先建一张**只服务于本迁移**的日志表，升级时逐条记录
`kind / subject / class_id / old_valid_to / old_header_class_id / old_graph_label`。
升级后实测 12 行（7 revive + 4 insert_bo + 1 repoint_bo）。

**为什么必须有它**（代码评审 + 安全评审共同提出的 HIGH）：
按「类名 + 当前状态」反推降级会把**升级前就已活跃**的同名类一并墓碑化
（误改不属于本迁移的数据），且只能把原 `graph_label` / 墓碑时间戳硬编码猜回去。

### 3.3 降级（`downgrade()`）

反向四步，**全部以 journal 为准**：

| 步骤 | 做法 | 守卫 |
|---|---|---|
| ① 改指回原头类 | 原头类 id + **原 `graph_label`** 从 journal 读回 | 只改「当前仍指向本迁移目标类」的行 |
| ② 删新增 BO | 只删 journal 记录的 4 个 **code**；`pg_constraint` **动态枚举**所有引用 `business_object` 的外键 | 任一外键有引用行 ⇒ **保留该行**（宁可留一行，也不让降级失败）；不认识的外键形态一律视为有引用（保守） |
| ③ 重新墓碑化 | 按 journal 的 class id 恢复**原墓碑时间戳** | 只改「当前活跃」的行 |
| ④ 删 journal 表 | `DROP TABLE IF EXISTS` | 降级后库形状与本迁移无关 |

实测（开发库 5433）：`alembic downgrade 0157` 后 7 个类恢复原时间戳、
4 个 BO 删除、INVOICE 改回 `DWD_AP_INVOICE_DTL`、journal 表消失；
再 `upgrade head` ⇒ 12 行 journal 与业务结果全部重现。

⚠️ **`DO` 块里放不进具名参数**：`text()` 只替换 `:name`，而 `DO $$…$$` 体是字符串
字面量 ⇒ 参数替换后留下 `$1` ⇒ plpgsql 报「there is no parameter $1」
⇒ 降级删 BO 的 DO 块**不含参数**（目标集合来自 journal，本就无需参数）。

### 3.4 迁移号顺延

head 0157 ⇒ 本批 **0158** ⇒ **A4-0 顺延 0159**。
已同步改 4 处仓内引用：`0156_semantic_manifest_v2.py`（注释）、
`0157_semantic_release_domain_version.py`（注释）、`Harness/wiki/data-model.md`、记忆文件。

---

## 4. 接口契约变更

**无**。本批不动任何 HTTP 路由、请求 / 响应 DTO、状态机。
（`OVERRIDE` 影响面只有读路径**数据内容**：采购域切片从 6 类 / 1 指标变成 10 类 / 9 指标。）

---

## 5. 实现要点

### 5.1 落点

| 文件 | 性质 | 职责 |
|---|---|---|
| `app/domain/procurement_object_catalog.py` | **新增** | 决策 SSOT：10 个对象的处置（`ObjectDisposition` 6 值穷举）+ 复活名单 / 新增 BO / 改指 / 不建清单；**纯数据 + 纯函数**（不吃 DB） |
| `alembic/versions/0158_procurement_object_reconciliation.py` | **新增**（迁移） | 自包含 DML；抽「语句序列」纯函数（`reconciliationStatements()` / `revertStatements()`）供同步 alembic 与异步测试**共用同一份 SQL** |
| `app/tests/unit/test_procurement_object_catalog.py` | **新增** | 31 条：目录不变量 + **迁移↔目录漂移守卫**（`importlib` 加载迁移拿**运行期真值**）+ **变异反证**（×2）+ 迁移自包含断言（AST 走 `ast.walk`） |
| `app/tests/integration/test_ontology_governance_integration.py` | 改造 | 新增 `TestProcurementObjectReconciliationMigration`（**8 条**，真实 PG 5434） |
| `scripts/probe_procurement_object_reconciliation.py` | **新增**（探针） | 24 条只读源侧探针（`execute_read_only` + SQL Guard） |
| `scripts/probe_procurement_domain_slice.py` | **新增**（探针） | 真实治理表 → `compileManifest` → `sliceManifestByDomain`，取「切片可达性」 |
| `docs/update/1118/M1/M1-1.4-采购本体对象核对表.md` | **新增**（文档） | 10 对象逐条核对表 + 两套血统 + 前后对照 + 欠账 + 决策 D-12…D-16 |

### 5.2 四个实施坑（实际踩到的）

1. **`asyncpg AmbiguousParameterError`**：迁移在 `alembic upgrade head` 时**整体失败**
   ⇒ 所有参数改**显式 CAST**（`CAST(:code AS varchar(20))` 等）。
2. **单测变异反证的锚点削错了行**：`str.replace('    "DWD_SUPPLIER_PAYMENT",\n', "")`
   的 4 空格模式**同时命中** 8 空格缩进的 `_NEW_BUSINESS_OBJECTS` 行
   ⇒ PAY 元组被削成 3 元组 ⇒ `IndexError` ⇒ 改成带换行锚定的 `'\n    "..." ,\n'`。
3. **集成测试的异步 / 同步执行器分叉**：`applyReconciliation(bind)` 是**同步**路径，
   异步 Session 不能直接用（`coroutine never awaited`）⇒ 迁移抽出**纯函数**
   `reconciliationStatements()`，两条路径消费**同一份 SQL**（否则各写一份必然漂移）。
4. **集成断言假设 `business_object` 为空 ⇒ 4 红**：`app/tests/integration/conftest.py`
   的 autouse fixture 会补种 **5 个基线 BO**（SUPPLIER / MATERIAL / PO / GR / IQC）
   ⇒ 整表计数恒 > 0 ⇒ 断言**收窄到本批 4 个新码**（PR / QUOT / POL / PAY）。
5. **`business_object` 的主键是 `code`，没有 `id` 列**（评审后改写 journal 时踩到）：
   `RETURNING id, code, header_class_id` ⇒ `UndefinedColumnError: column "id" does not exist`
   ⇒ journal 以 **code** 为键。
6. **`DO` 块不能带具名参数**（同上）：`DO $$… :code …$$` 经 `text()` 替换后留下 `$1`
   ⇒ plpgsql 编译期报「there is no parameter $1」⇒ 删除动作改为「目标集合来自 journal」的无参 DO 块。

### 5.3 为什么集成用例并进既有文件

CI 的 `backend-integration` 按 `find app/tests/integration -name 'test_*.py' | sort -z`
**下标 mod 4** 分片 ⇒ **新建 integration 文件会重排分片 ⇒ 出现假的 net-new 失败**。
`backend-unit` 是路径 sha256 哈希分片 ⇒ **新建 unit 文件安全**。
⇒ 新集成用例并进 `test_ontology_governance_integration.py`（193 行，语义相近，未超 800 行上限）。

---

## 6. 测试

### 6.1 定向运行（`./scripts/run_local_tests.sh`）

```
app/tests/integration/test_ontology_governance_integration.py
app/tests/unit/test_procurement_object_catalog.py -q
⇒ 49 passed, 21 warnings in 26.31s（退出码 0）
```

（9 条新集成 + 31 条新单测 + 既有 9 条本体治理用例）

宽回归（语义发布 / 编译器 / 域切片 10 个文件）⇒ **263 passed**，净新增失败 **0**。

### 6.2 单测覆盖的不变量（31 条）

| 组 | 条数 | 关键断言 |
|---|---|---|
| 目录自身 | 11 | 10 个对象不多不少；`objectKey` 唯一；**每个处置值都有对象在用**（枚举无死分支）；**复活名单 / 新增 BO 名单被对象认领**（不被认领 = 幽灵改动）；BO 头类最终必活跃；BO 码大写且 ≤ 20（列宽 + CHECK）；CONTRACT 必须 `NOT_BUILT` 且理由含「不虚构源表」；每个「不建」必须写承载面 |
| 迁移↔目录守卫 | 4 | 逐字一致（复活名单 / 新增 BO / 改指）；revision 链 0158→0157；迁移**不 import `app.*`**（AST 走 `ast.walk`，不用正则） |
| 变异反证 | 2 | 改坏复活名单 ⇒ 守卫报「复活名单不一致」；改坏一个 BO 码 ⇒ 报「新增 BO 不一致」 |
| 正向守卫 / 纯函数 | 14 | 名单**非空**（防「空集相等」假绿）；`revivedClassNames()` 稳定序；未知 key ⇒ None；改指目标覆盖 |

### 6.3 集成覆盖的行为（9 条，真实 PG + 真实 SQL，不 mock）

| 用例 | 断言 |
|---|---|
| `test_revives_the_tombstone_class_in_place` | `valid_to` 被清空 |
| `test_metric_source_is_dropped_before_and_kept_after` | **口径不丢**：复活前 `sources == []`（复现缺陷），复活后 `[{className, role}]` |
| `test_inserts_the_four_business_objects` | 4 个 BO 落库，域 = PROCUREMENT，**图标签 = 头类名**（逐条比对） |
| `test_repoints_the_invoice_business_object` | INVOICE 头类换成 `DWD_PURCHASE_INVOICE` 的 id；**再降级** ⇒ 头类与**原 graph_label（非类名）**都读 journal 还原 |
| `test_is_idempotent` | 跑两遍结果相同 |
| `test_is_a_noop_without_lineage_b_classes` | Lineage A 环境：无 `DWD%` 类、4 个 BO 一个都不插 |
| `test_revert_restores_the_previous_state` | 降级：恢复**原墓碑时间戳** + 新增 BO 被删；**升级前就活跃的同名类不被墓碑化**（评审 HIGH 的反证） |
| `test_all_header_classes_are_active_after_apply` | `ACTIVE_HEADER_CLASSES` 不是手写声明：跑完迁移后实证 6 个头类全部活跃 |
| `test_procurement_slice_reaches_the_metric_after_reconciliation` | **业务后果**：补齐前指标不在采购域切片，补齐后在 + 类进 `classNames` |

### 6.4 证据（真实库）

| 文件 | 内容 |
|---|---|
| `docs/update/1118/M1/evidence/procurement-object-reconciliation.txt` | 24 条源侧行数实测（DWD_SUPPLIER_PRICE_LIST_HEADER=17,749；DWD_PURCHASE_REQUISITION_LINE=181,201；DWD_PURCHASE_ORDER_DTL=1,023,442；DWD_GOODS_RECEIPT_DTL=3,977,714；DWD_SUPPLIER_PAYMENT=33,576；DWD_AP_INVOICE_DTL=**46**（时间跨度 2026-09-02→2026-10-09）；DWD_PURCHASE_INVOICE=38,323；_LINE=2,486,311 …） |
| `docs/update/1118/M1/evidence/procurement-domain-slice-before.txt` | 补齐前：活跃 59 / 墓碑 18；源 3/18（丢 15）；切片 6 类 / 1 指标 / 1 KPI |
| `docs/update/1118/M1/evidence/procurement-domain-slice-after.txt` | 补齐后：活跃 **66** / 墓碑 **11**；源 **18/18**（丢 **0**）；切片 **10 类 / 9 指标 / 9 KPI** |

### 6.5 迁移在开发库 5433 的实际应用

```
uv run alembic upgrade head
⇒ Running upgrade 0157 -> 0158, 采购本体对象核对与补齐（0158，M1-1.4）。
```

---

## 7. 审查

两轮审查（`code-reviewer` / `security-reviewer`）**首轮因 API 额度上限（HTTP 429）中断**，
额度恢复后**重跑取得结论**；下列问题**已全部修复并回归**。

### 7.1 评审发现与处置

| # | 级别 | 来源 | 问题 | 处置 |
|---|---|---|---|---|
| 1 | **HIGH** | 双评审 | 降级按「类名 + `valid_to IS NULL`」全局命中 ⇒ 会把**升级前就活跃**的同名类一并墓碑化（误改不属于本迁移的数据），且文档宣称「完整还原」不成立 | 引入 **journal 表**：升级只记录**实际命中**的 class id，降级只回改这些 id。新增集成反证（预置一个「本就活跃」的同名类，降级后必须仍活跃） |
| 2 | **HIGH** | 双评审 | 复活 / 降级墓碑化用 `version = 1` 锚定而插入用「最高版本」，版本解析不对称 ⇒ 墓碑行不是 v1 时复活**静默 0 行**，指标源照旧被丢弃（且测试恰好种 v1 ⇒ 分支永不暴露） | 复活**去掉 version 过滤**（按 `class_name + valid_to IS NOT NULL` 锚定），一致性由 journal 负责 |
| 3 | **HIGH** | security | 清 `valid_to` 即让类重入 `compileManifest` 与 NL2SQL 召回，无留痕 ⇒ 未确认对象静默进入口径 | **接受风险并留痕**：journal 表逐条记录改动（可审计、可逆）；复活的是**已发布口径的锚点类**而非新增业务数据；**候选绑定仍不进快照**（D-3 不变）；生产部署前须走发布闸门（本批不部署） |
| 4 | MEDIUM | 双评审 | 降级删 BO 的外键白名单只枚举 3 张表，漏 `business_object_domain`（以及未来新增的引用面） | 改为 **`pg_constraint` 动态枚举**所有引用 `business_object` 的外键；不认识的外键形态一律保守视为「有引用」⇒ 留行 |
| 5 | MEDIUM | code | 降级把 `graph_label` 硬编码还原成 `DWD_AP_INVOICE_DTL`，生产原值不同即**永久丢失**且不可逆清单未披露 | 升级时把原 `graph_label` 落 journal，降级读回原值；集成用例用**非类名**原值（`发票（文件抽取旁表）`）反证 |
| 6 | MEDIUM | code | 改指回滚**零覆盖**（测试不种 INVOICE BO ⇒ `_REPOINT_BACK_SQL` 命中 0 行） | 同一用例内 apply → revert，断言头类与 graph_label 均还原 |
| 7 | MEDIUM | security | 探针把 `ds.database_name` 与**异常原文**写进证据文件（驱动异常常含 host/user/SQL），违反「不落密」承诺 | 去掉库名；新增 `maskSecrets()`（`scheme://user:pass@` → `scheme://user:***@`，`password=…` → `password=***`）并套在两处 `except` 上 |
| 8 | MEDIUM | security | `primary_domain_id` 传的是域 code 字符串，若列为域 id 外键则语义错 | **核实**：`business_object.primary_domain_id` 是 `varchar(20)`（不是 FK）⇒ 传 `'PROCUREMENT'` 正确；写入 §3.1 备注 |
| 9 | LOW | code | `ACTIVE_HEADER_CLASSES` 只是手写声明，无实证 | 新增集成用例 `test_all_header_classes_are_active_after_apply`（真库验证 6 个头类活跃） |
| 10 | LOW | security | 探针 `sys.path.insert(0, os.getcwd())` 依赖启动目录 | 接受（与仓内其他探针同款；默认输出 stdout 不含 DSN） |

### 7.2 主 agent 自审（补充）

| 关注面 | 结论 |
|---|---|
| 迁移注入面 | 全部参数化（`text(sql)` + 参数字典），**无字符串拼接**；DO 块里的动态 SQL 只用 `format(%s/%I)` 渲染 `pg_catalog` 取到的标识符，不拼用户输入 |
| 探针只读 | `probe_procurement_object_reconciliation` 走 `execute_read_only`（SQL Guard）；`probe_procurement_domain_slice` 只做 `select` + 内存编译 |
| 复活是否扩大读面 | 复活让**已发布口径**重新可用（不是新增数据）；**候选绑定仍不进快照**（D-3 不受影响）；本批**不部署** ⇒ 生产读路径零变化 |
| 降级误删面 | journal 精确 id + 动态外键枚举 + 「当前就指向本迁移目标」三重守卫 ⇒ 不会误删 / 不会撞 RESTRICT |

### 7.3 遗留

- 本批**未部署**；部署时 journal 表会随迁移创建（首期 12 行），降级末尾自动清除。
- ⚠️ **`alembic check` 在已应用 0158 的环境里必然报一条 drift（`remove_table
  procurement_object_reconciliation_journal`）** —— 这是**设计使然**，不是缺陷：
  journal 表**只服务于本迁移**（升级写、降级删、**没有 ORM 模型**）⇒ 应用元数据里
  本就不该有它 ⇒ autogenerate 看到「库里有、元数据里没有」就报 `remove_table`。
  ⇒ 判据口径：**该 drift 之外无其它差异**才算绿；CI 不跑 `alembic check`
  （见 `.github/workflows/ci.yml`）⇒ 不构成合并闸门。

---

## 8. 部署验证

- **开发库 5433**：已应用 0158（`alembic upgrade head` 成功）。
- **测试库 5434**：集成用例直接在自己的事务里执行迁移语句（不动 `alembic_version`）⇒ 无需升级测试库。
- **生产**：**未部署**（用户决策）。
- **冒烟**：无 HTTP 面变化 ⇒ 不做端点冒烟；业务后果由 §6.3 的切片可达性用例 + §6.4 的 before/after 证据承担。

---

## 9. 关联

- 计划：`docs/update/1118/智能采购平台_V0.3落地实施计划_M0-M7_1118.md` §1.4
- 设计稿：`docs/update/智能采购平台详细设计方案_V0.3.docx` §3.2
- 核对表：`docs/update/1118/M1/M1-1.4-采购本体对象核对表.md`
- Wiki：`Harness/wiki/data-model.md`「采购本体对象：墓碑类必须原地复活（迁移 0158）」
- 前序：`Harness/changes/2026-11-19-m1-procurement-domain-release/summary.md`（M1-1.3）、
  `Harness/changes/2026-11-18-m1-procurement-binding-service/summary.md`（M1-1.1）
- 后续：M1-1.5 采购术语补齐；M1-1.6 范围裁决落地；M1-b 绑定确认（仍卡 C 问卷）

---

## SSOT 校验清单（合并前必查）

- [x] 10 个对象逐条有处置（6 种取值穷举，无漏项）
- [x] CONTRACT 处置明确：**本体不建 + 文档目录承载 + 实测 0 份 ⇒ 首期不可查**
- [x] 迁移**自包含**（AST 断言不 import `app.*`）
- [x] 迁移**幂等**（三重存在性守卫）+ **可降级**（journal 精确还原：原墓碑时间戳 / 原 graph_label / 只删本次新增的 BO）
- [x] Lineage A 环境**整批 no-op**（集成用例实证）
- [x] 决策 SSOT ↔ 迁移**逐字一致**（漂移守卫 + 2 条变异反证 + 非空正向守卫）
- [x] 所有 SQL 参数**显式 CAST**（asyncpg 歧义已实测踩到并修）
- [x] 业务后果取证：切片 **6 类/1 指标/1 KPI → 10 类/9 指标/9 KPI**；丢弃源 **15 → 0**
- [x] 集成用例**并进既有文件**（不打乱 CI 分片）
- [x] 集成断言**收窄到本批 4 个 BO 码**（不假设 `business_object` 为空）
- [x] 迁移号顺延 **0158 ⇒ A4-0 取 0159**（4 处仓内引用已同步）
- [x] **两轮审查重跑**（§7.1：2 HIGH / 5 MEDIUM / 2 LOW，**全部处置**）
