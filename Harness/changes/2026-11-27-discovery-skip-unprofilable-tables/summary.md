---
created: 2026-11-27
updated: 2026-11-27
---

# 变更：发现运行跳过「不可剖析对象」（Oracle 回收站等）

- **日期**：2026-11-27
- **分支**：`fix/discovery-skip-unprofilable-tables`（基线 `origin/main` = `5872a81a`）
- **触发**：用户实测 —— 本体发现触发运行时选择表，反复报
  `ProfileSqlError: 表名不是合法标识符：'BIN$W81vJmasuczgY0bNqMBvog==$0'`，
  **一次就废掉整轮运行**。

## 1. 根因

Oracle 的**回收站对象**（`BIN$W81vJmasuczgY0bNqMBvog==$0`）含 `=` ⇒ 不在
`discovery_profile_sql` 的标识符白名单（`^[A-Za-z_][A-Za-z0-9_$#]*$`）内 ⇒
拼 SQL 前必被护栏拒 ⇒ `ProfileSqlError`。

而剖析编排 `_collect` 是「任一张表失败 ⇒ 整轮 FAILED」⇒ **一张回收站表就废掉
整轮运行**（用户反复触发、反复报同一个错）。

三个环节各有缺口：

1. **内省不区分**「能不能剖析」：回收站对象与业务表一起进快照 / schema 缓存；
2. **剖析层粒度太粗**：单表失败 ⇒ 整轮失败；
3. **前端没有过滤**：选择器把不可剖析的表也列出来，用户选到就踩雷。

## 2. 改动

### 2.1 单一事实源：`app/domain/sql_identifier.py`（新）

`MAX_IDENTIFIER_LENGTH` + `IDENTIFIER_PATTERN` + `isValidIdentifier()`。
`discovery_profile_sql.assertIdentifier` 改为从这里取常量 ⇒ **SQL 构造护栏与
「不可剖析对象」分类不会漂移**。

### 2.2 内省出口过滤（`schema_introspection_service._assembleTables`）

不可作标识符的表（回收站对象等）**不进** schema 缓存 / 快照 / 选择器 ⇒
新采集的快照天然干净。过滤条数写 warning（不静默）。

### 2.3 剖析层单表跳过（`discovery_profiler._collect`）

- 新增 `ProfileIdentifierError(ProfileSqlError)`：标识符类失败专用子类
  （既有 `except ProfileSqlError` 处理器**行为不变**）。
- 遇 `ProfileIdentifierError` ⇒ **跳过这一张、其余表继续**；全部被跳过 ⇒
  **明确失败**（不报「零证据成功」）。
- 其它 `ProfileSqlError`（分块非法 / 聚合无列 / 聚合无行）仍**整轮失败**
  —— 那些是整体问题，跳过等于静默产出空结果。

⇒ **老快照**（过滤前捕获的）与**直接调 API 传表名**两条路径都被兜住。

### 2.4 快照读接口给出「不可剖析表名」

`GET /ontology-discovery/snapshots/{id}` 新增 `unprofilableTables`（由
`unprofilableTableNames(snapshotJson)` 算出：表名 / 属主 / 列名任一不合法即算）。

### 2.5 前端：选择器过滤 + 搜索（`RunsPanel.tsx` + `utils/discoveryTableFilter.ts`）

- **不显示**不可剖析的表，并提示「已隐藏 N 张不可剖析的表（Oracle 回收站对象等）」；
- **搜索**（`showSearch` + 自定义 `filterOption`）：命中**表名 / 属主.表名 / 列名**
  ⇒ 既能「排查哪些表」，也能「找出包含某列的表」；按列名命中时标注是哪一列。

## 3. 测试

| 位置 | 覆盖 |
|---|---|
| `backend/app/tests/unit/test_discovery_unprofilable_tables.py`（新，10 用例） | 白名单口径（含回收站名）、内省过滤、`unprofilableTableNames`（表名/属主/列名三类）、剖析跳过 + 全部跳过时明确失败 |
| `backend/app/tests/unit/test_discovery_profile_sql.py` | 既有 29 用例仍绿（`assertIdentifier` 改抛子类，语义不变） |
| `backend/app/tests/unit/test_discovery_{profiler,snapshot_service}.py` / `test_schema_introspection_service.py` | 既有 144 用例仍绿 |
| `backend/app/tests/unit/test_ontology_discovery_api.py` | 21 用例仍绿（快照响应新增字段不破坏契约） |
| `frontend/src/tests/discoveryTableFilter.test.ts`（新，10 用例） | 搜索命中（表名/属主/列名/大小写）、隐藏不可剖析表、隐藏清单 |
| `frontend/src/tests/OntologyDiscoveryPage.test.tsx` | 新增 1 例：回收站表**不出现在选项里** + 「已隐藏 1 张」提示 |

全量：前端 **272 文件 / 2673 用例全绿**；后端定向 144 + 21 + 10 全绿。

## 4. 影响面

- **不变**：标识符白名单本身（护栏不放松 ⇒ 没有注入面变化）。
- **变**：内省结果里不再有回收站对象（对业务是正向：它们本来就不能被剖析）。
- **兼容**：`ProfileIdentifierError` 是 `ProfileSqlError` 子类 ⇒ 既有处理器不变；
  快照响应新增字段是**增量**（老前端忽略即可）。
- **老快照**：过滤前捕获的快照仍含回收站表 ⇒ 剖析层兜底跳过（不再整轮失败）。

## 4.1 两处**刻意的取舍**（评审关注点）

1. **跳过粒度 = 整张表**（不是单列）：表里只要有一列名字不合法，`_columnRows` 会抛
   `ProfileIdentifierError`，本表**整张跳过**（表级行数与列级统计都不写）。
   理由：坏列名比坏表名更罕见（回收站对象是坏**表名**），整表跳过行为可预测；
   损失有 warning 日志（表名 + 原因）。若要更精细（保留表级行数、只跳过坏列），
   需改 `_columnRows`/`chunkColumns` 的粒度 —— 本批未做。
2. **`unprofilableTableNames` 在每次 `GET /snapshots/{id}` 都现算**：40000 表级别的快照
   会做数十万次 `isValidIdentifier`（正则 + 短路），相对该响应本身的数 MB JSON 序列化
   可忽略；换成落库列会让快照表多一列「派生数据」，与「快照是证据、只增不改」冲突 ⇒
   保持现算。

## 4.2 追加修复：ORA-00932（CLOB 连 `COUNT(col)` 都不允许）

**用户实测第二炸**：修完回收站表之后触发运行，又报
`DatabaseError: ORA-00932: 数据类型不一致: 应为 -, 但却获得 CLOB`。

**根因**：旧裁剪只去掉 `COUNT(DISTINCT)/MIN/MAX`（`_UNSUPPORTED_DISTINCT`），
留下了空值计数 `COUNT(*) - COUNT(col)` —— 而 **Oracle 连 `COUNT(clob_col)` 都不允许**
⇒ 含 CLOB 列的表全量剖析必炸。直连 THBI 复现：`ODS_SRM_ORDER_PURCHASE_ORDER_INFO`
53 列（含 `REMARK CLOB`），chunk 1 必炸；THBI 全 schema 实测 **38 个 CLOB 列**。

**修法**：裁剪粒度从「逐聚合」提到「**逐列**」：

- 新增 `_UNAGGREGATABLE_COLUMNS = _UNSUPPORTED_DISTINCT | {"UNDEFINED"}` ——
  这些类型**整列不进剖析 SQL**（连空值数都不做；对 LOB 列空值数本来意义就有限，
  表级 `rowCount` 来自 `COUNT(*)` 不受影响）；`UNDEFINED` 是 Oracle 19c
  不可识别类型的占位，任何聚合都可能炸 ⇒ 一并跳过。
- `ProfileColumn` 加 `supportsNullCount`；`profileColumnsFromSnapshot` 按类型标记；
  `buildColumnStatsSql` 对 `supportsNullCount=False` 的列**抛错**（构造层防御，
  不静默产出会炸的 SQL）；`discovery_profiler._columnRows` 先剔除这类列并写
  warning（跳过的列**不写** profile 行）。
- 直连复现验证：修后 53 列 → 52 列进剖析，三个 chunk 全 OK。

**测试**：新增 5 例（CLOB/UNDEFINED 整列标记、普通类型保留、构造层拒绝、
BOOLEAN 只裁 MIN/MAX 仍保留 COUNT）；既有
`test_unsupported_aggregates_are_omitted_from_sql` **语义升级**（CLOB 从
「只裁 DISTINCT/MIN/MAX」变为「整列剔除」⇒ 用例改为断言构造层拒绝，并在 docstring
写明行为升级的原因）。相关套件 53 + 118 全绿。

**与 §4.1 取舍 1 的关系**：列级跳过 = **不写**该列的 profile 行（不是炸掉）；
表级跳过（回收站）= 整张表没有剖析证据。两者口径一致：**证据缺失可解释、有日志**。

## 5. 遗留 / 后续

- **快照里已有的回收站表不会被清掉**（快照是内容寻址、只增不改）⇒ 老快照继续带着
  它们，只是剖析时跳过。要彻底干净需重新采集快照（内容寻址 ⇒ 内容变了会新增一行）。
- **回收站对象何时进快照**取决于采集时刻库里的状态：库侧 purge 回收站后重新采集，
  新快照就不含它们了。
- 未做：把「不可剖析」做成**数据源级**的可见状态（当前只在发现域体现）。
