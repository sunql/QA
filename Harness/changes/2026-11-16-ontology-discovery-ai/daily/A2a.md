# A2a 每日证据（2026-11-16）—— 列长度 / 精度进快照（L-9 解冻）

> 承接 A1 遗留项 **L-9**（见 `daily/A1.md` §九）：`NUMBER(10,2)` 与 `NUMBER(10,4)`、
> `VARCHAR(10)` 与 `VARCHAR(50)` 在旧口径下**采集结果逐字相同** ⇒ 快照内容哈希相同
> ⇒ 发现引擎看不见「精度 / 长度变了」这类变更。
>
> 本增量**只**做一件事：把列的数值精度与字符长度采进快照。
> **零 LLM、零外部授权、零 DDL、零新增端点、零 CI 面变更。**
>
> ⚠️ 措辞更正（review 指出）：不是「零 API 契约变更」——`ColumnSchemaRead` 是**共享** DTO
> ⇒ 既有 `POST /datasources/{id}/introspect`、`GET /datasources/{id}/schema` 的响应列项
> **加法式**多出 3 个可选字段，`schema_cache.schema_data` 也随之下一次内省带上三键。
> 传播链与「不进任何 LLM prompt」的逐路径核实见 §三。

## 〇、需求（用户视角）

业务把一张表的金额列从 `NUMBER(10,2)` 改成 `NUMBER(10,4)`、把备注列从 `VARCHAR(50)`
改成 `VARCHAR(200)`：类型名没变 ⇒ 旧口径下这两次采集的快照**完全一样** ⇒
「本体发现」这一层**根本不知道业务库变过**。本增量让这类变更**能被看见**。

## 一、recon 事实（实读代码，不靠推测）

| 编号 | 事实 | 证据 |
|---|---|---|
| C-1 | `ColumnSchemaRead` 只有 `column_name` / `data_type` / `nullable` / `comment` ⇒ `data_type` 只带**类型名**（"NUMBER" / "VARCHAR"） | `app/domain/schemas.py:1687` |
| C-2 | 快照列项的形状 = `normalizeSnapshot` 逐字段搬运 ⇒ 想让快照带上精度，就得让 `ColumnSchemaRead` 带上精度 | `app/services/discovery_snapshot_service.py:184` |
| C-3 | 快照内容寻址用 `manifestHash`（与 Manifest 同一把尺子）⇒ 内容变 ⇒ 哈希变 ⇒ 不可擦除 | `app/services/discovery_snapshot_service.py:239` |
| C-4 | 采集链路唯一：`SchemaIntrospectionService.introspect` → `TableSchemaRead` → `normalizeSnapshot` → 落库 ⇒ 改内省即可贯通 | `app/services/discovery_snapshot_service.py:476` |
| C-5 | **内省只支持 3 种业务库类型**（`DataSourceType` = mysql / postgresql / oracle）⇒ 方言注册表里的 12 个方言**不涉及** schema 内省 | `app/domain/enums.py:29` |
| C-6 | `_mergeRows` 是列字典的**唯一**构造点 ⇒ 三方言别名统一后只需一处取键 | `app/services/schema_introspection_service.py:118` |
| C-7 | Oracle 适配器把 `cursor.description` **小写化** ⇒ 双引号小写别名即可；PG/MySQL 走 SQLAlchemy `mappings()` ⇒ 别名即键 | `app/infrastructure/business_db_pool.py:685` |
| C-8 | MySQL 的 `information_schema.columns` **只对有权限的表返回列** ⇒ 真实数据验证必须用有权限的账号/库 | 实测 `asset_demo` / `wms_demo` 权限差异 |
| C-9 | `schema_cache` 的摘要 `_schemaVersion` 覆盖整份 dump ⇒ 加字段后摘要变化 ⇒ 内省缓存**一次性刷新**（预期行为，非缺陷） | `app/services/schema_introspection_service.py:427` |
| C-10 | `SNAPSHOT_SCHEMA_VERSION` 只被 `normalizeSnapshot` 与真实数据脚本引用 ⇒ 改版本号的影响面可控 | `app/services/discovery_snapshot_service.py:55`、`scripts/ontology_discovery_realdata.py:106` |

> ⚠️ **更正 A1 的一处措辞**：A1 §九 L-9 写的是「需改**共享** `ColumnSchemaRead` +
> **12 方言**内省 SQL」。按 C-5，schema 内省**只覆盖 3 种业务库类型**（12 个方言是
> NL2SQL **SQL 生成**用的方言注册表，与内省无关）⇒ 本增量按真实覆盖面做 **3 方言**，
> 不是 12。

## 二、方案取舍（D-1…D-10）

| 编号 | 取舍 | 理由 |
|---|---|---|
| **D-1** | 精度**独立成字段**，不拼进 `dataType` | 拼进去会（a）破坏下游按类型名的匹配与 prompt 文本；（b）让「类型变了」与「精度变了」无法分别判定 |
| **D-2** | 三个字段**可选 + 默认 None** | 老 `schema_cache.schema_data` 行缺新键 ⇒ Pydantic 自动取默认 ⇒ **无需数据迁移**、向后兼容 |
| **D-3** | 三方言 SQL **别名统一**为 `numeric_precision` / `numeric_scale` / `character_max_length` | 否则 `_mergeRows` 要认两套拼写（Oracle 的 `data_*` vs info schema 的 `numeric_*`）⇒ 两处拼写迟早漂移 |
| **D-4** | Oracle 取 `char_length`（**字符数**）而非 `data_length`（字节数） | 与 PG/MySQL `character_maximum_length`（字符数）**同量纲** ⇒ 三方言数值可比 |
| **D-5** | 采集不到一律 `None`，**不用 0 兜底** | `0` 与「未定义」语义不同；用 0 兜底会把「无精度限制」与「精度为 0」混为一谈 |
| **D-6** | `SNAPSHOT_SCHEMA_VERSION` **v1 → v2** | 快照 JSON 形状变了就是变了，版本号用来显式记录这件事。**代价（旧 v1 快照与新快照不可比 ⇒ 同一 schema 采集两次会新增一行快照）是预期行为** |
| **D-7** | 快照 JSON **恒含三键**（未知为 `null`） | 与 `comment` 的既有口径一致（无注释 ⇒ 键在、值为 null）⇒ 形状稳定、跨版本可比 |
| **D-8** | **不进 NL2SQL prompt** | `nl2sql_schema` 只用 `dataType` ⇒ 本体 prompt 文本**零变化** ⇒ 不影响 NL2SQL 行为与 token 成本 |
| **D-9** | **不新建 integration 测试文件** | CI 分片按「排序下标 mod 4」⇒ 新文件会重排分片 ⇒ 制造假 net-new。本增量只改**既有** unit 文件 |
| **D-10** | 不动 DDL、不动审计动作白名单 | 零迁移 ⇒ 部署面为零（仍按 A1 口径「不部署」） |
| **D-11** | **不归一各方言的「整数宽度」口径** | 各方言目录对整型列的报法本就不同（实测见 §2.1）：Oracle `NUMBER`（未按声明精度）⇒ 预期 `NULL/NULL`（⚠️ **未实测**，L-14）；PG `integer` ⇒ `32/0`；MySQL `int` ⇒ `10/0`（类型显示宽度）。归一它需要「按类型推断语义」的方言知识表 ⇒ 过度设计。**如实保留各方言口径**：副作用是「整数列改宽（`int`→`bigint`）也会被看见」——这正是我们想要的。跨方言比较同一份 schema 本就不是发现域的场景（快照按数据源分源，哈希唯一约束 `(source_id, schema_hash)`） |

### 2.1 各方言**实测**口径（在本地 PG 16 / MySQL 8.0.46 上直接查 `information_schema`）

| 列定义 | PostgreSQL 16 | MySQL 8.0.46 |
|---|---|---|
| `int` / `integer` | `32 / 0` | `10 / 0`（显示宽度） |
| `bigint` | `64 / 0` | `19 / 0`（显示宽度） |
| `smallint` | `16 / 0` | — |
| `tinyint` | — | `3 / 0` |
| `int` + `unsigned` | —（PG 无） | `10 / 0` ⇒ **与 `int` 逐字相同**（缺口 L-15） |
| `decimal`（未声明） | `NULL / NULL` | `10 / 0` |
| `decimal(12,4)` | `12 / 4` | `12 / 4` |
| `varchar(50)` | `characterMaxLength = 50` | `50` |
| `text` | `NULL` | **`65535`**（引擎上限，**不是声明长度**） |
| `timestamp(3)` / `datetime(3)` | `datetime_precision = 3`（**本增量没采**） | 同左（缺口 L-16） |

⇒ 三条必须记住的推论：

1. **同一个「bigint 主键」在 PG 记 64、在 MySQL 记 19** ⇒ 跨方言比较这三个数字**没有意义**；
   快照按源分源（哈希唯一约束 `(source_id, schema_hash)`），本就只在源内可比。
2. **MySQL 的整型数字是「显示宽度」不是声明精度** ⇒ 它仍然满足我们的目标
   （`int → bigint` 会让数字从 10 变 19 ⇒ 哈希变 ⇒ 看得见），但不是「精度」这个词的字面语义。
3. **`TEXT` 在 MySQL 报 65535、在 PG 报 NULL** ⇒ 同一份「无上限文本」语义在两边数字完全不同
   ⇒ 读的人必须知道方言（见 §3.2）。

## 三、实现清单

| 文件 | 变更 | 位置 |
|---|---|---|
| `backend/app/domain/schemas.py` | `ColumnSchemaRead` 增 3 个**可选**字段 | `1703-1705` |
| `backend/app/services/schema_introspection_service.py` | `_optionalInt` 归一（异常形状 ⇒ `None` + **WARNING**，不静默）；`_mergeRows` 携带三字段 | `104-137` |
| 同上 | Oracle 列 SQL 增 `data_precision` / `data_scale` / `char_length`（别名统一） | `562-564` |
| 同上 | info_schema 列 SQL 增 `numeric_precision` / `numeric_scale` / `character_maximum_length`（别名统一） | `612-614` |
| `backend/app/services/discovery_snapshot_service.py` | `normalizeSnapshot` 列项增三键；`SNAPSHOT_SCHEMA_VERSION` → `v2` | `55`、`213-215` |
| `backend/scripts/ontology_discovery_realdata.py` | `_EXPECTED_SCHEMA_VERSION` → `v2` | `106` |
| `backend/app/tests/unit/test_discovery_snapshot_service.py` | 5 条新用例（含**仅精度变化新增一行**）+ 更新结构断言 | 规范化节 / 采集节 |
| `backend/app/tests/unit/test_schema_introspection_service.py` | 新增 `TestColumnPrecisionAndLength`（7 条，含**老缓存行兼容**与**异常取值留 WARNING**） | 文件尾 |
| `frontend/src/types/datasource.ts` | `ColumnSchema` 增 3 个**可选**字段（无 UI 变更） | `96-107` |
| `Harness/changes/.../summary.md` | §4.1 新增「A2a 对既有 schema 契约的加法式变更」+ §5/§6/§7.5 | — |

### 3.1 传播链（review M-2：必须显式登记，否则后人只看 A1 会漏判面）

```
ColumnSchemaRead（共享 DTO）
  ├─→ 快照 normalizeSnapshot → metadata_snapshot.snapshot_json（本增量目标）
  ├─→ schema_cache.schema_data（下一次内省写入 → 老行缺键 ⇒ 默认 None，无需迁移）
  ├─→ GET /api/v1/datasources/{id}/schema、POST /{id}/introspect 响应列项（登录可见）
  └─✗ 任何 LLM prompt
```

**不进 prompt 的逐路径核实**：`import_llm_enhancer` 只取 `name` + `data_type`；
数据质量规则生成只取 `column_name` / `data_type` / `nullable`；NL2SQL 走 Manifest 不走 `schema_cache`
⇒ prompt 文本与 token 成本**零变化**。

### 3.2 方言不在 JSON 里（review S-M1 的处置）

快照 JSON 顶层只有 `schemaVersion` + `tables`，**不含**方言。方言的权威来源是
`metadata_snapshot.source_id → discovery_source.datasource_id → data_source.type`
（外键锚定，单点真相）；且 `schema_hash` 的唯一约束是 `(source_id, schema_hash)`
⇒ **哈希本来就只在源内可比**。所以三字段的跨方言语义歧义**不会**污染跨源比较
（跨源比较本就是无意义操作）。代价：JSON 本身不自描述 ⇒ 读 JSON 的人必须顺着 `source_id` 查方言
（已写入 wiki）。

## 四、测试证据

### 4.1 单测：先 RED 后 GREEN（TDD 顺序可查）

```bash
cd backend
export TEST_DATABASE_URL='postgresql+asyncpg://…@127.0.0.1:5434/qa_metadata_test_semantic'
.venv/bin/python -m pytest app/tests/unit/test_discovery_snapshot_service.py \
    app/tests/unit/test_schema_introspection_service.py -q
```

| 阶段 | 结果 |
|---|---|
| **RED**（只加测试、未改实现） | **11 failed / 74 passed** —— 11 条全是本增量新增/更新的断言（`ColumnSchemaRead` 尚无 `numeric_precision` ⇒ `AttributeError`；`schemaVersion` 仍 `v1`；两条 SQL 尚无新列） |
| **GREEN**（实现补齐后） | **85 passed / 56.11s** |
| **复审补测**（review 处置，两个文件） | 先 RED **1 failed**（`_optionalInt` 未打日志）→ 实现后 **88 passed / 49.69s** |

> ⚠️ 如实登记：复审补的 3 条用例里，`test_仅精度变化新增一行且旧行不变` 与
> `test_build_response_tolerates_legacy_cache_rows` **第一次跑就是绿的**（它们钉的是已实现的行为，
> 属回归断言，不是新行为）；只有 `test_optional_int_logs_unexpected_value` 走完了 RED→GREEN。

新增断言的语义（逐条对应需求）：

| 用例 | 钉住的性质 |
|---|---|
| `test_仅数值精度不同哈希不同` | `NUMBER(10,2)` vs `NUMBER(10,4)` ⇒ **哈希必须不同**（L-9 的正面答复） |
| `test_仅字符长度不同哈希不同` | `VARCHAR(10)` vs `VARCHAR(50)` ⇒ 哈希必须不同 |
| `test_快照列携带精度值` | 精度值原样进快照 JSON |
| `test_快照列恒含精度三键` | 采集不到时**键仍在**、值为 `null`（D-7） |
| `test_oracle_columns_sql_reads_precision_and_length` | Oracle SQL 读三列且**别名统一**（D-3） |
| `test_info_schema_columns_sql_reads_precision_and_length[mysql/postgresql]` | PG/MySQL SQL 读三列且别名统一 |
| `test_merge_rows_carries_precision_fields` / `test_merge_rows_without_precision_is_none` | `_mergeRows` 携带三字段；缺失 ⇒ `None`（D-5） |
| `test_introspect_exposes_precision_on_columns` | 端到端：内省结果 ⇒ `ColumnSchemaRead` 带精度 |

### 4.2 回归：A1 集合 + 本增量两个文件（10 文件）

```bash
.venv/bin/python -m pytest app/tests/unit/test_discovery_models.py \
    app/tests/unit/test_discovery_snapshot_service.py \
    app/tests/unit/test_semantic_diff_service.py \
    app/tests/unit/test_ontology_discovery_api.py \
    app/tests/unit/test_ontology_discovery_realdata.py \
    app/tests/unit/test_external_text.py \
    app/tests/unit/test_production_auth_guard.py \
    app/tests/unit/test_audit_service.py \
    app/tests/unit/test_nl2sql_schema.py \
    app/tests/unit/test_schema_introspection_service.py -q
# ⇒ 247 passed / 146.51s
```

### 4.3 相邻模块（内省 / 导入 / 漂移 / 连接池）

```bash
.venv/bin/python -m pytest app/tests/unit/test_schema_drift.py \
    app/tests/unit/test_lineage_extractor.py app/tests/unit/test_join_inference.py \
    app/tests/unit/test_datasource_pool.py app/tests/unit/test_local_import_service.py \
    app/tests/unit/test_data_quality_rule_generator.py -q
# ⇒ 2 failed / 220 passed
```

> ⚠️ 2 条失败是**既有红基线**，与本增量无关：`test_datasource_pool.py` 的两条断言写的是
> `ALTER SESSION SET READ ONLY`，而实现早已（更早批次）改成 `SET TRANSACTION READ ONLY`
> （Oracle 19c 会 ORA-02248）。**本增量刻意不改**：改了会让失败集合**变小** ⇒ 合并闸门
> 判「净丢失」（exit 5）⇒ 反而需要人工确认。留作独立批次修。

### 4.4 集成测试（真实 PG + 完整 HTTP 链路）

```bash
export TEST_NEO4J_URI='bolt://…@127.0.0.1:7688'   # 独立测试实例
.venv/bin/python -m pytest app/tests/integration/test_datasource_schema_api.py -q
# ⇒ 8 passed / 14.48s
```

> 本增量**未新建** integration 测试文件（D-9）⇒ CI 分片归属**零变化**。

### 4.5 前端类型检查

```bash
cd frontend && npx tsc -b
# ⇒ exit 0（0 错误）
```

## 五、真实数据证据（`scripts/ontology_discovery_realdata.py`）

### 5.1 采集面（写隔离测试库 `qa_metadata_test_a2a`，真实 MySQL 业务库 `asset_demo`）

```
REALDATA RESULT: PASS          # 8/8 断言成立
```

| 断言 | 结果 |
|---|---|
| 首次采集落库（内容寻址） | ✅ 落库 hash=24ad0764855b… = 独立重算 |
| 首次采集的行数变化符合内容寻址语义 | ✅ 0 行 → 1 行（reused=False） |
| 落库行的表/列计数与快照内容一致 | ✅ tableCount=2 columnCount=7 |
| 二次采集复用既有快照（幂等） | ✅ reused=True snapshotId=1 |
| 二次采集不新增快照行（只增不改） | ✅ 1 行 → 1 行 |
| 二次采集未改写既有快照行（**原生读**比对） | ✅ captured_at 未变 |
| 快照 JSON 形状合规（版本 / 排序） | ✅ schemaVersion=v2 |
| 哈希可由落库 JSON 独立重算 | ✅ 口径可复现 |

**落库快照实测（本增量的正面证据）**：

```
schemaVersion= v2
DIM_ASSET_CATEGORY  CATEGORY_CODE      varchar  None None 32
DIM_ASSET_CATEGORY  CATEGORY_NAME      varchar  None None 100
DIM_ASSET_CATEGORY  DEPRECIATION_YEARS decimal  6    2    None
DWD_ASSET_LEDGER    ASSET_CODE         varchar  None None 32
DWD_ASSET_LEDGER    ASSET_NAME         varchar  None None 100
DWD_ASSET_LEDGER    CATEGORY_CODE      varchar  None None 32
DWD_ASSET_LEDGER    PURCHASE_DATE      date     None None None
```

⇒ 旧口径下 `varchar(32)` 与 `varchar(100)` 采集结果**完全一样**；现在**可区分**
⇒ 改长度 ⇒ 哈希变 ⇒ 发现引擎看得见。

> ⚠️ **各方言的整型口径不同（刻意不归一，D-11）**：Oracle `NUMBER`（未声明精度）报 `NULL/NULL`；
> PG `integer` 报 `32/0`；MySQL `int` 报 `10/0`（类型显示宽度）⇒ 副作用是
> **整数列改宽（`int`→`bigint`）也会被看见**（这正是我们要的）。快照按数据源分源 ⇒
> 跨方言比较同一份 schema 不是发现域的场景。

### 5.2 差异面（**只读**治理库 `qa_metadata`，真实 ACTIVE 发布）

```
REALDATA RESULT: PASS          # 3/3 断言成立（变更数 23 vs 23，风险分级一致）
```

## 六、范围外 / 未做（如实登记）

| 项 | 状态 | 原因 |
|---|---|---|
| 行级样本 / 列剖析指标（PII、采样授权） | **未做** | 阻塞于 **Q3**（业务决策：授权连接范围 / 敏感字段 / 数据质量基线） |
| 精度变更的**类型化差异**（"这列从 6,2 变成 8,4"） | **未做** | A1 的 `/diff` 比较的是**本体 Manifest**，不是快照 ⇒ 快照侧目前只能靠「哈希变了」提示；类型化差异属 A2 范畴 |
| 前端 UI 展示精度 | **未做** | 发现域**尚无前端页面**（只有 API）⇒ 本增量只补类型 |
| 本体导入链路（`ontology_property`）落精度 | **未做** | L-9 的诉求是**快照**；导入链路未使用精度，且改它会牵动本体发布语义（另一批次） |
| Oracle 分支真实库取证 | **未做**（L-14） | 本机无 Oracle 实例。⚠️ **必须在首次生产采集前补**——快照不可擦除，采错了改不了 |
| MySQL `INT` vs `INT UNSIGNED` 区分 | **未做**（L-15） | 实测两者 `NUMERIC_PRECISION` 都是 `10`、`DATA_TYPE` 都是 `int` ⇒ 同哈希。要区分得读 `COLUMN_TYPE`（第 4 个字段），本增量不做 |
| 时间列小数秒精度（`datetime(3)` → `datetime(6)`） | **未做**（L-16） | PG/MySQL 把小数秒精度放在**独立的** `datetime_precision` 列（实测 3 / 6），本增量没采 ⇒ 只改小数秒精度**看不见** |

## 七、偏离与遗留登记

| 编号 | 项 | 状态 |
|---|---|---|
| **L-9** | 列长度 / 精度不入快照 | ✅ **已解冻**（本增量）；A1 §九 该行同步更新 |
| **L-12** | 快照侧变更只有「哈希变了」，没有类型化差异 | 登记为 A2 待办（与 Q3 解冻一起做） |
| **L-13** | 前端 `ColumnSchema` 仍**没有** `comment` 字段声明（后端早已返回） | 既有缺口，本增量不扩大范围 ⇒ 登记 |
| **L-14** | Oracle 分支零真实库证据（`DATA_PRECISION` / `DATA_SCALE` / `CHAR_LENGTH`，含 CLOB 的 `CHAR_LENGTH` 是 NULL 还是 0、`TIMESTAMP` 的小数秒精度是否落进 `DATA_PRECISION`） | 登记；**首次生产采集前必须补**（脚本已支持 `--datasource-id`） |
| **L-15** | MySQL `INT` vs `INT UNSIGNED` 同哈希 | 登记；需读 `COLUMN_TYPE` |
| **L-16** | 时间列小数秒精度不在快照里（`datetime(3)` vs `datetime(6)` 同哈希） | 登记；需采 `DATETIME_PRECISION` |

## 八、审查结论（security-reviewer + code-reviewer，两轮并行）

| 审查 | 结论 |
|---|---|
| security-reviewer | **CRITICAL 0 / HIGH 0 / MEDIUM 2 / LOW 3** |
| code-reviewer | **CRITICAL 0 / HIGH 1 / MEDIUM 4 / LOW 4** |

逐条处置（编号：S = security，C = code）：

| 项 | 处置 |
|---|---|
| S-M1 跨方言语义未标注且被固化进不可擦除记录 | **文档钉死 + 不落 JSON**：方言权威来源是 `source_id → discovery_source.datasource_id → data_source.type`；哈希唯一约束本就是 `(source_id, schema_hash)` ⇒ 跨源比较无意义。**拒绝**「加顶层方言键」：会再造一个可能与外键不一致的真相源。方言差异实测表见 §2.1 |
| S-M2 变更溢出到共享 DTO / `schema_cache` / 内省 API | 已在开头告示 + §3.1 传播链 + summary §4.1 显式登记 |
| S-L1 / C-M4 Oracle 无真实库证据 | 登记 L-14 |
| S-L2 三列成为内省硬依赖 | 登记最低版本：Oracle 8i+ / PG 9.0+ / MySQL 5.7+ / MariaDB 10.0+（`information_schema` 这三列自这些版本稳定存在） |
| S-L3 列宽构成信息泄露新维度 | **显式披露决策**：列宽 / 精度属**结构元数据**，判定为可公开；已知边际推断 = 定长敏感列的格式长度（被既有「列名 + 类型 + 注释 + 主外键图」暴露严格支配）。要收紧就收**读门禁**，不是采集过滤（采集过滤对已入库行无效——不可变） |
| C-H1 文档漂移（summary 仍称「既有契约零变更」） | **已修**：summary §4.1 新增加法式变更登记；本文件措辞同步更正 |
| C-M1 MySQL `unsigned` 盲区 | 登记 L-15（实测证实：`int` 与 `int unsigned` 都是 `10/0`） |
| C-M2 DB 层精度变更只钉在纯函数层 | **补用例** `test_仅精度变化新增一行且旧行不变`（原生 SQL 读 JSON 文本） |
| C-M3 老 `schema_cache` 行兼容零覆盖 | **补用例** `test_build_response_tolerates_legacy_cache_rows` |
| C-L1 `_optionalInt` 静默吞异常 | **已改**：异常形状 ⇒ `None` + WARNING（取值截断 40 字符打印，不静默）；用例 `test_optional_int_logs_unexpected_value` |
| C-L2 TEXT/BLOB 的 `characterMaxLength` 语义不同 | 写入 §2.1 与 wiki（MySQL 报引擎上限） |
| C-L3 v1 / v2 快照不可直接比较 | 写入 wiki（读侧按 `schemaVersion` 分流） |

## 九、验收自检

- [x] 先写测试并**看到它失败**（RED 11 failed）才写实现
- [x] 实现只做到让测试通过（GREEN 85 passed）
- [x] 零 LLM、零外部授权、零 DDL、**零新增端点**；既有 schema 响应**加法式**增 3 个可选字段（§3.1 已登记传播链）
- [x] 不新建 integration 测试文件 ⇒ CI 分片归属零变化
- [x] 三方言别名统一（`_mergeRows` 只有一套取键口径）
- [x] 未知一律 `None`（不用 0 兜底）
- [x] 真实数据两个面都跑通（8/8 + 3/3）
- [x] 既有红基线如实登记且**刻意不修**（避免制造「净丢失」）
- [x] 前端类型检查 0 错误
- [x] 文档同步（wiki + 本文件 + MEMORY）
