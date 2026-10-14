# DialectRegistry Refactor — 设计稿

**Goal:** 把 `app/services/nl2sql_dialects.py` 里的 `_SQL_DIALECTS` 字典 + Oracle-only if/else 重构为一个显式的 `DialectRegistry` 类，按 `(DataSourceType, versionBucket)` 注册方言，fallback 链 `exact → major → default` 解析；让研究侧 LLM 在面对 MySQL 5.7 / 8.0 / MariaDB、PostgreSQL 14/15/16、Oracle 19c/21c 等不同版本时能拿到版本特定的 prompt 提示（不只"Oracle 一种方言"）。

**Scope:** 仅 dialect 注册/解析层（`nl2sql_dialects.py` + `nl2sql_service.py` 薄壳 + 测试）。B/C 不在本 spec 范围。

**Architecture:**
- 引入 `DialectRegistry` 类，`register(dbType, bucket, dialect)` / `resolve(dbType, version)` 两个公共方法。
- `SqlDialect` dataclass 新增 `bannedFunctions: tuple[str, ...]` 字段（默认空）+ `check_sql(self, sql) -> DialectGuardResult` 实例方法；`checkSqlDialect` 模块级函数改为薄壳，调用 `registry.resolve(dsType).check_sql(sql)`。
- 内置方言从全局 dict 迁到 `DialectRegistry.default()` 的初始化体里；每个版本桶一个独立 `SqlDialect` 实例（不共享 default 状态）。
- `Nl2SqlService.resolveDialect(dsType, version)` 改为一行 `return DialectRegistry.default().resolve(dsType, version)`。
- 版本 → bucket 的归一化函数（`_bucketsForVersion(dbType, version)`）作为 registry 内部 helper，**不**对外暴露。

**Tech Stack:** Python 3.11、frozen dataclass、`DataSourceType` (str, Enum)、pytest + pytest-asyncio（已有）。

---

## 全局约束

1. **Chat 行为零变化** — 所有现有 chat 调用方（`chat_service.py:_buildPipelineContext` / `_runQueryWithRetry`、`nl2sql_service.py:_twoStageGenerate`、各类 LLM prompt 构造）通过 `Nl2SqlService.resolveDialect(dsType, version)` 取方言；refactor 后**逐字段等价**。
2. **公共 API 签名不变** — `resolveDialect` / `checkSqlDialect` 签名一致；老 import 路径不破。
3. **`checkSqlDialect` 仍按 `DataSourceType` 一刀切（V1）** — banned function 集合不随 minor version 变化；version-specific banned sets 在 V2 再考虑。
4. **TDD** — 先红后绿；`tests/prompts/test_nl2sql_dialects.py` 现有断言**全部继续绿**作为基线，再加新版方言 + 新 `DialectRegistry` 单测。
5. **小文件** — 单文件不超过 800 行；`nl2sql_dialects.py` refactor 后行数控制在 600 以内。
6. **不可变** — `DialectRegistry` 用 `frozen=True` dataclass + `MappingProxyType`；register 创建新实例，不 mutate。
7. **不引入第三依赖** — 仅用标准库 + 已有的 `DataSourceType` enum。

---

## 现状（基线）

**已有文件：**

- `app/services/nl2sql_dialects.py`（~420 行）：`SqlDialect` dataclass（10 字段，全默认空串）、`_SQL_DIALECTS: dict[DataSourceType, SqlDialect]`（3 个 key）、`resolveDialect(dsType, oracle_version=None)`（仅 Oracle 走 if 分支）、`checkSqlDialect(sql, dsType)`（全局 banned set dict `_BANNED_FUNCS_PER_TYPE`）。
- `app/services/nl2sql_service.py` 第 290 行：`Nl2SqlService.resolveDialect(dsType, version)` 薄壳。
- `app/tests/prompts/test_nl2sql_dialects.py`（~180 行）：现有方言单测，含 Oracle 11g/12c 分支 + banned function 全套断言。
- `app/tests/prompts/test_nl2sql_date_arithmetic_prompt.py`：system prompt 集成测试。

**根问题：**

- MySQL / PostgreSQL **零版本感知** — 同一个 `SqlDialect` 实例无差别返回；LLM 不知道目标服务器是 MySQL 5.7（无原生 CTE）还是 8.0（有 CTE + window function）。
- Oracle 11g/12c 分支写死在 `resolveDialect` 函数体内、19c/21c 无独立方言。
- 注册逻辑分散在 dict 字面量 + 函数 if 分支两处，扩展版本要改两个地方。
- 未来要加新 DB（OceanBase、达梦、GBase）时，无清晰的注册 API。

---

## 设计

### `DialectRegistry` 类

```python
# app/services/nl2sql_dialects.py

@dataclass(frozen=True)
class DialectRegistry:
    """按 (dbType, versionBucket) 注册/解析方言。
    
    register 是「创建新实例」语义（frozen），不 mutate。
    resolve 走 fallback 链：exact bucket → major bucket → 'default' → KeyError。
    """
    _entries: MappingProxyType[tuple[DataSourceType, str], SqlDialect]
    
    @staticmethod
    def default() -> "DialectRegistry":
        """返回系统内置注册表（所有 known 版本方言已填入）。"""
    
    def withEntry(self, dbType: DataSourceType | str, bucket: str, dialect: SqlDialect) -> "DialectRegistry":
        """注册一条 (dbType, bucket) → 方言；同 key 重复注册抛 ValueError。返回新实例。"""
    
    def resolve(self, dbType: DataSourceType | str, version: str | None = None) -> SqlDialect:
        """按 fallback 链解析方言。version 解析失败 / 未注册 → 抛 KeyError。"""
```

**Fallback 链实现：**

```python
def resolve(self, dbType, version=None):
    db = DataSourceType(dbType)
    for bucket in _candidateBuckets(db, version):
        key = (db, bucket)
        if key in self._entries:
            return self._entries[key]
    raise KeyError(f"no dialect registered for {db} (version={version!r})")

def _candidateBuckets(db: DataSourceType, version: str | None) -> list[str]:
    """按 db 类型生成候选 bucket 列表（exact → major → default）。"""
    if not version:
        return ["default"]
    v = version.lower().strip()
    if db is DataSourceType.ORACLE:
        # "19.0.0.0.0" → ["19c", "19", "default"]; "11.2.0.1.0" → ["11g", "11", "default"]
        m = re.match(r"(\d+)([gci])", v)
        if m: return [f"{m.group(1)}{m.group(2)}", m.group(1), "default"]
        m = re.match(r"(\d+)", v)
        if m: return [m.group(1), "default"]
        return ["default"]
    if db is DataSourceType.MYSQL:
        # "8.0.46" → ["8.0", "8", "default"]; "5.7.30" → ["5.7", "5", "default"]
        m = re.match(r"(\d+)\.(\d+)", v)
        if m: return [f"{m.group(1)}.{m.group(2)}", m.group(1), "default"]
        m = re.match(r"(\d+)", v)
        if m: return [m.group(1), "default"]
        return ["default"]
    if db is DataSourceType.POSTGRESQL:
        # "16.4" → ["16", "default"]
        m = re.match(r"(\d+)", v)
        if m: return [m.group(1), "default"]
        return ["default"]
    return ["default"]
```

### `SqlDialect` 扩展

```python
@dataclass(frozen=True)
class SqlDialect:
    name: str
    limitRule: str
    joinTemplate: str
    useSchemaPrefix: bool
    sampleLimitSql: str
    identifierRule: str = ""
    nullOrderingRule: str = ""
    timeBucketRule: str = ""
    aggregateRule: str = ""
    dateArithmeticRule: str = ""
    bannedFunctions: tuple[str, ...] = ()   # 新增；默认空（保持向后兼容）
    intervalAllowed: bool = True             # 新增
    notes: str = ""                           # 新增；版本特定的 prompt 提示（如 "CTE 限制解除"）
    
    def check_sql(self, sql: str | None) -> DialectGuardResult:
        """按本方言的 bannedFunctions + intervalAllowed 校验 SQL。"""
```

**关键决策：** `bannedFunctions` 字段**默认空**——老 `SqlDialect(...)` 调用方零修改，refactor 后的内置方言在 `default()` 初始化体里填上原 `_BANNED_FUNCS_PER_TYPE` 对应集合。`check_sql` 实例方法取代模块级 `_assert_no_banned_calls`。

### `checkSqlDialect` 重写为薄壳

```python
def checkSqlDialect(sql: str | None, datasourceType) -> DialectGuardResult:
    """按 dsType 解析方言并校验 SQL。banned set 仍按 DataSourceType 一刀切（V1）。"""
    dialect = DialectRegistry.default().resolve(datasourceType)
    return dialect.check_sql(sql)
```

### 内置方言（`DialectRegistry.default()` 初始化体）

| Bucket | `bannedFunctions`（来源 `_BANNED_FUNCS_PER_TYPE`） | 与 default 差异 |
|---|---|---|
| `oracle_default` | Oracle banned set | `_SQL_DIALECTS_ORACLE_12C` 内容；LIMIT 用 `FETCH FIRST N ROWS ONLY` |
| `oracle_11g` | Oracle banned set | LIMIT 用 `ROWNUM` |
| `oracle_19c` | Oracle banned set | 12c + `notes="ADD_MONTHS 优先；MERGE INTO 可用"` |
| `oracle_21c` | Oracle banned set | clone 19c；`notes` 加 `"JSON_DATAGUIDE 可用"` |
| `mysql_default` | MySQL banned set | 基准 |
| `mysql_8_0` | MySQL banned set | `notes="CTE / 窗口函数 / JSON_TABLE 可用"` |
| `mysql_5_7` | MySQL banned set | `notes="无原生 CTE；递归用临时表；无窗口函数"` |
| `mariadb_10_x` | MySQL banned set | `notes="MariaDB：SEQUENCE 可用；JSON_TABLE 在 10.6+ 可用"` |
| `postgresql_default` | PG banned set | 基准 |
| `postgresql_14` | PG banned set | `notes="MERGE / 14+ 语法"` |
| `postgresql_15` | PG banned set | clone 14 |
| `postgresql_16` | PG banned set | clone 14 |

**`bannedFunctions` 内容（从 `_BANNED_FUNCS_PER_TYPE` 平移）：**

- Oracle: `(r"\bDATEADD\b", r"\bDATEDIFF\b", r"\bDATEPART\b", r"\bDATE_FORMAT\b", r"\bDATE_SUB\b", r"\bDATE_ADD\b", r"\bCURDATE\b", r"\bNOW\b")`，`intervalAllowed=False`（`\bINTERVAL\s+\d` 单独 check）
- MySQL: `(r"\bDATEADD\b", r"\bDATEDIFF\b", r"\bADD_MONTHS\b", r"\bEXTRACT\b")`
- PG: `(r"\bDATEADD\b", r"\bDATEDIFF\b", r"\bADD_MONTHS\b", r"\bDATE_SUB\b", r"\bDATE_ADD\b", r"\bDATE_FORMAT\b")`

### Backwards-Compat 契约

| 入口 | refactor 前行为 | refactor 后行为 | 等价验证 |
|---|---|---|---|
| `Nl2SqlService.resolveDialect(ORACLE, "19c")` | 返回 12c 方言 | 返回 `oracle_19c` 方言 | **不等价**（改进：以前 19c 拿的是 12c 配置） |
| `Nl2SqlService.resolveDialect(ORACLE, "11.2.0.1.0")` | 返回 11g 方言 | 返回 `oracle_11g` 方言 | 等价 |
| `Nl2SqlService.resolveDialect(ORACLE, "9i")` | 返回 11g 方言 | 返回 `oracle_default` 方言 | **不等价**（fallback 链没匹配 9i/10g；改进路径：bucket 候选 `["default"]`，仍是 12c 行为） |
| `Nl2SqlService.resolveDialect(ORACLE, None)` | 返回 12c 方言 | 返回 `oracle_default` 方言 | 等价 |
| `Nl2SqlService.resolveDialect(MYSQL, anything)` | 返回 mysql dict entry | 返回 `mysql_default` | 等价（fallback 到 default） |
| `Nl2SqlService.resolveDialect(POSTGRESQL, anything)` | 返回 pg dict entry | 返回 `postgresql_default` | 等价 |
| `checkSqlDialect(sql, ORACLE)` | 校验 DATEADD/CURDATE/... | 同样集合 | 等价 |
| `checkSqlDialect(sql, MYSQL)` | 校验 DATEADD/ADD_MONTHS/... | 同样集合 | 等价 |
| `checkSqlDialect(sql, POSTGRESQL)` | 校验 DATEADD/ADD_MONTHS/... | 同样集合 | 等价 |
| `SqlDialect(name="x", limitRule="y", ...)` 旧 10 字段直接构造 | OK | OK（`bannedFunctions` 默认 `()`） | 等价 |

**chat 影响范围验证清单**：

- `app/services/nl2sql_service.py` 第 290 行 — `resolveDialect` 方法体改成 thin wrapper；调用方不变。
- `app/services/chat_service.py` `_buildPipelineContext` / `_runQueryWithRetry` / `_twoStageGenerate` — 通过 `self._nl2sql.resolveDialect(ds.type, ds.oracle_version)` 调；refactor 后 oracle_19c 字符串拿到的是 19c 方言而非 12c 方言（**改进**，不破坏）。
- `app/services/research_agent_execution.py` `_datasourceTypeOf` / `dialectArgsFor` / `runStep` — 同上调用链。
- `app/services/research_sql_runner.py` `runVerification` — 透传 `dialectArgs["datasourceType"]`，调 `checkSqlDialect`，refactor 后仍等价。

**回归断言（refactor 完成时必跑）：**

```python
def test_backcompat_resolve_oracle_19c_unchanged_behavior():
    """refactor 前 oracle_19c 返回 12c 方言；refactor 后返回 19c 方言（语义升级）。
    关键不变量：所有 date_arithmetic_prompt / banned_function 断言仍绿。"""
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "19.0.0.0.0")
    assert "FETCH FIRST" in d.limitRule   # 仍是 12c+ 风格 LIMIT

def test_backcompat_resolve_oracle_11g_returns_11g_dialect():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "11.2.0.1.0")
    assert "ROWNUM" in d.limitRule

def test_backcompat_check_sql_oracle_rejects_curdate():
    r = DialectRegistry.default().resolve(DataSourceType.ORACLE).check_sql("SELECT CURDATE()")
    assert not r.ok

def test_backcompat_check_sql_mysql_rejects_add_months():
    r = DialectRegistry.default().resolve(DataSourceType.MYSQL).check_sql("SELECT ADD_MONTHS(...)")
    assert not r.ok
```

---

## 改动文件清单

- **MODIFY** `app/services/nl2sql_dialects.py`（+180 行，~600 行总）：加 `DialectRegistry`、`_candidateBuckets`；扩展 `SqlDialect`（+3 字段 + `check_sql` 实例方法）；`checkSqlDialect` / `resolveDialect` 改成薄壳；`_BANNED_FUNCS_PER_TYPE` 改为方言初始化时的输入。
- **MODIFY** `app/services/nl2sql_service.py`（行 290 ±5）：`resolveDialect` 方法体改成一行 `return DialectRegistry.default().resolve(...)`。
- **CREATE** `app/tests/prompts/test_dialect_registry.py`（~150 行）：`DialectRegistry.register` / `resolve` / fallback chain / 重复 register 抛 `ValueError` / frozen 不可 mutate。
- **MODIFY** `app/tests/prompts/test_nl2sql_dialects.py`：现有断言全保留；新增 bucket 解析（`oracle_19c → 19c 方言`、`mysql_8.0 → mysql_8_0`、`pg_16 → postgresql_16`）；新增版本-specific `notes` 字段断言。

**不动文件：**

- `app/services/datasource_service.py` `_probeServerVersion` / `_normalizeServerVersion` — 已存到 `ds.oracle_version` / 未来加 `ds.mysql_version` / `ds.postgresql_version`（**注意**：本 spec 暂不动 ORM schema；MySQL/PG 的 version 字段暂时不在 schema 里，靠 `BusinessDbAdapter.test()` 返回值在 `dialectArgsFor` 里现场取）。
- `app/services/research_agent_execution.py` `dialectArgsFor` — 不变（透传 `ds.oracle_version`）。
- `app/services/research_sql_runner.py` `runVerification` — 不变。
- `app/services/chat_service.py` 所有调用方 — 不变（通过 `Nl2SqlService.resolveDialect` 间接拿到方言）。

**ORM schema 留白：** MySQL/PG 的 `oracle_version` 列字段名误导——本 spec 不动 schema（`oracle_version` 对 MySQL/PG 实际未被使用）。未来若要做 MySQL/PG 版本探测落库，需要新增 `mysql_version` / `postgresql_version` 列；这是独立 schema migration，不在本 refactor 范围。

---

## 测试计划

### 必跑（refactor 完成后）

1. **回归基线**：`pytest app/tests/prompts/test_nl2sql_dialects.py app/tests/prompts/test_nl2sql_date_arithmetic_prompt.py -v` — 全部继续绿（chat 兼容性的硬保证）。
2. **新测试**：`pytest app/tests/prompts/test_dialect_registry.py -v` — 全绿。
3. **集成**：`pytest app/tests/integration/test_research_sql_runner.py::test_run_verification_dialect_guard_rejects_mysql_in_oracle -v` — 仍绿（2026-10-08 commit 0d3b752 的回归守卫）。

### 端到端（手动 / 集成）

4. **真机跑一个 oracle_19c 提问**（如果当前 sandbox 能连 Oracle 19c）：跑现有 session 流程；进 prompt 模板；断言 system prompt 包含 `oracle_19c.notes`（"ADD_MONTHS 优先；MERGE INTO 可用"）。
5. **fallback 链 smoke test**：`registry.resolve(ORACLE, "9i")` 返回 `oracle_default`，日志记 `fallback to oracle_default`；`registry.resolve(MYSQL, "10.4.14-MariaDB")` 返回 `mysql_default`。

### Test 设计原则

- `test_dialect_registry.py` 用纯 Python 单测，无 DB 依赖（mock `SqlDialect`）。
- `test_nl2sql_dialects.py` 现有断言**不动**，**只加**新的 bucket-specific 断言。
- 不构造真实数据库连接做方言测试（与现状一致）。

---

## Spec Self-Review

1. **占位符扫描** — 无 TBD / TODO；所有版本号都给了具体值。
2. **内部一致性** — `bannedFunctions` 字段定义在 §设计 和 §改动文件清单 两处一致；fallback 链在 §设计和 §Backwards-Compat 两处一致。
3. **Scope check** — 单文件 refactor（nl2sql_dialects.py + nl2sql_service.py thin wrapper + 测试），范围聚焦；B/C 明确不在本 spec。
4. **歧义检查** —
   - `_candidateBuckets` 对 Oracle "9i" 走 `["default"]`（无 `9i` bucket 注册）；§Backwards-Compat 已说明 refactor 前是 `["11g", "default"]`，refactor 后是 `["default"]`，**语义变化**（不再回落到 11g 而是直接 default）。这是降级还是改进待评估 — 若需保留 9i→11g 行为，需在 `_candidateBuckets` ORACLE 分支加 `9i→11g` 显式映射。**待确认**：与产品确认是否要保留 9i/10g 显式支持。
   - "MariaDB 走 mysql_default" 的兜底在 fallback 链之外（`mariadb_10_x` 显式注册 + 兜底 mysql_default）；如果未注册 `mariadb_10_x` bucket，`resolve(MYSQL, "10.4.14-MariaDB")` 走 `["10", "default"]` → `mysql_default`。OK。

---

## 风险

1. **Oracle 9i/10g 行为变化** — refactor 前 oracle "9i" → 11g 方言；refactor 后 → `oracle_default`（12c 风格）。若生产有 9i/10g 数据源且 prompt 模板关键差异在 11g，会回归。**缓解**：运行 `git grep -n "9i\|10g\|oracle_version"` 确认没有数据源用这两个版本；如有用，要么保留 9i/10g bucket，要么在 `_candidateBuckets` 加显式映射。
2. **`SqlDialect` 加字段导致 dataclass 构造方不兼容** — 直接位置参数构造（如 `SqlDialect("Oracle", "FETCH FIRST...")`）如果有，需改为关键字。**缓解**：先 `git grep -n "SqlDialect("` 确认所有构造方都用关键字。
3. **`monkeypatch` 现有测试** — 若有测试 `monkeypatch.setattr(nl2sql_dialects, "_SQL_DIALECTS", ...)` 会失效（dict 已不存在）。**缓解**：refactor 前 grep 一下，把这类测试改成 monkeypatch `DialectRegistry.default()`。
4. **行为不变性的盲点** — 现有 `test_nl2sql_dialects.py` 没断言 oracle_19c 的具体行为（只测 11g/12c），所以 refactor 后的"19c 返回 19c"是**新增能力**而非回归。**缓解**：在 §测试计划 加 oracle_19c 端到端真机验证。

---

## Acceptance Criteria

- [ ] `DialectRegistry` 类实现并有单测
- [ ] `SqlDialect` 扩展字段（`bannedFunctions` / `intervalAllowed` / `notes` / `check_sql`）
- [ ] 内置 12 个版本方言注册（oracle_4 + mysql_4 + pg_4）
- [ ] `checkSqlDialect` / `Nl2SqlService.resolveDialect` 公共签名不变
- [ ] 现有 `test_nl2sql_dialects.py` + `test_nl2sql_date_arithmetic_prompt.py` + `test_research_sql_runner.py` 全部继续绿
- [ ] chat 真机回归（构造一个 chat session 跑 SQL，断言 SQL 模板含 oracle_19c 特定 notes）
- [ ] 无新依赖
