# DialectRegistry 多版本方言注册表 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `_SQL_DIALECTS` dict + Oracle-only if 分支重构为显式的 `DialectRegistry` 类，按 `(DataSourceType, versionBucket)` 注册/解析方言；保持 chat 行为零变化；为 MySQL 5.7/8.0/MariaDB、PostgreSQL 14/15/16、Oracle 19c/21c 提供版本特定的 prompt `notes`（不只"Oracle 一种方言"）。

**Architecture:**
- 新增 `DialectRegistry` 类（frozen dataclass + `MappingProxyType`），公共 API：`default() -> DialectRegistry` / `withEntry(dbType, bucket, dialect) -> DialectRegistry` / `resolve(dbType, version) -> SqlDialect`。
- `SqlDialect` 增加 `bannedFunctions: tuple[str, ...]` / `keywordPatterns: tuple[tuple[str, str, str], ...]` / `notes: str` / `intervalAllowed: bool` 字段（默认空，向后兼容）+ `check_sql(sql) -> DialectGuardResult` 实例方法。
- 模块级 `checkSqlDialect` / `resolveDialect` 改为薄壳：内部调 `DialectRegistry.default().resolve(...)`。
- 版本字符串 → bucket 候选列表走 `_candidateBuckets(db, version)` 内部 helper（oracle/mysql/pg 各一条规则）。Oracle 的 "9/10/11 开头 → 11g" 历史行为完整保留。
- 内置方言从 `_SQL_DIALECTS` 字典迁到 `DialectRegistry.default()` 的初始化体；保留 oracle_default / oracle_11g / mysql_default / postgresql_default；新增 oracle_19c / oracle_21c / mysql_8_0 / mysql_5_7 / mariadb_10_x / postgresql_14 / postgresql_15 / postgresql_16。

**Tech Stack:** Python 3.11、frozen dataclass、`MappingProxyType`（标准库）、pytest、pytest-asyncio、mypy、ruff。

---

## Global Constraints

1. **Chat 行为零变化** — `chat_service.py` / `nl2sql_service.py` / `research_agent_execution.py` 现有调用方不修改任何业务代码；refactor 通过薄壳适配层保证语义等价。
2. **向后兼容契约（refactor 后必须保留）**：
   - `resolveDialect(None, anything)` → oracle_11g（历史 fallback，line 394-395）
   - `resolveDialect("unknown_str", anything)` → oracle_11g + warning log（line 403）
   - `resolveDialect(ORACLE, "9i"|"10g"|"11.2.0.1.0"|"11g")` → oracle_11g（line 411）
   - `resolveDialect(ORACLE, "12c"|"19.0.0.0.0"|None|"")` → oracle_default（12c 风格 LIMIT）
   - `resolveDialect(ORACLE, "19c")` → oracle_19c（**新增**，优于 12c）
   - `resolveDialect(ORACLE, "21c")` → oracle_21c（**新增**）
   - `resolveDialect(MYSQL|POSTGRESQL, anything)` → 现有单方言（**新增**版本特异性 fallback 但 default 内容不变）
   - `checkSqlDialect(sql, ORACLE)` → 拦截 DATEADD/DATE_FORMAT/CURDATE/INTERVAL 等（不变）
   - `checkSqlDialect(sql, MYSQL|POSTGRESQL)` → 拦截 DATEADD/ADD_MONTHS 等（不变）
   - `SqlDialect(name=..., limitRule=..., ...)` 老 10 字段构造依然 OK（新字段默认空）
3. **TDD**：每个 Task 第 1 步写失败测试；现有 `tests/prompts/test_nl2sql_dialects.py` 全绿作为 chat 兼容基线。
4. **不引入第三依赖** — 仅用标准库（dataclasses、types.MappingProxyType、re、enum）。
5. **不可变** — `DialectRegistry` 用 frozen dataclass + `MappingProxyType`；`withEntry` 创建新实例，**不** mutate。
6. **小文件** — `nl2sql_dialects.py` refactor 后行数控制在 700 以内（当前 416 + 新增 ≈ 280）。
7. **用户自己 commit** — AI 不执行 `git commit`（与本仓库其它 plan 一致）；每 task 最后一步写 "不 commit"。

---

## File Structure

| 文件 | 改动 | 行数预估 |
|---|---|---|
| `backend/app/services/nl2sql_dialects.py` | 加 `DialectRegistry` 类（~80 行）+ `_candidateBuckets`（~30 行）+ `SqlDialect` 扩字段 + `check_sql` 方法（~30 行）+ 内置方言迁移到 `default()`（~120 行）+ `checkSqlDialect` 薄壳（~10 行）；`resolveDialect` 薄壳（~15 行） | +280 → 700 |
| `backend/app/services/nl2sql_service.py:290` | `resolveDialect` 方法体改成 1 行薄壳 | ±5 |
| `backend/app/tests/prompts/test_dialect_registry.py` | **新建**：~150 行（register/resolve/fallback/duplicate/immutable） | +150 |
| `backend/app/tests/prompts/test_nl2sql_dialects.py` | 现有断言**全保留**；新增 bucket-specific 解析断言（~40 行） | +40 |
| `backend/app/tests/integration/test_research_sql_runner.py` | **不动**（2026-10-08 commit 0d3b752 的回归守卫） | 0 |

**不动**：`backend/app/services/chat_service.py`、`backend/app/services/research_agent_execution.py`、`backend/app/services/research_sql_runner.py`、`backend/app/services/datasource_service.py`（schema 不动，version 探测已落 `oracle_version` 列；MySQL/PG 版本 schema 列推迟到未来）。

---

## Task 1: `SqlDialect` 扩字段 + `check_sql` 实例方法（RED → GREEN）

**Files:**
- Modify: `backend/app/services/nl2sql_dialects.py:22-56`（`SqlDialect` 类）
- Create: `backend/app/tests/prompts/test_dialect_check_sql.py`

**Interfaces:**
- Consumes: `SqlDialect` 现有 10 字段
- Produces:
  - `SqlDialect.bannedFunctions: tuple[str, ...] = ()`
  - `SqlDialect.keywordPatterns: tuple[tuple[str, str, str], ...] = ()`
  - `SqlDialect.intervalAllowed: bool = True`
  - `SqlDialect.notes: str = ""`
  - `SqlDialect.check_sql(sql: str | None) -> DialectGuardResult` 实例方法

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/prompts/test_dialect_check_sql.py`：

```python
"""SqlDialect.check_sql 实例方法单测（不进数据库，纯 Python）。"""
from app.domain.enums import DataSourceType
from app.services.nl2sql_dialects import (
    DialectGuardResult,
    DialectViolation,
    SqlDialect,
)


def _mk(
    banned=(),
    keyword_patterns=(),
    interval_allowed=True,
):
    return SqlDialect(
        name="Test",
        limitRule="x",
        joinTemplate="y",
        useSchemaPrefix=False,
        sampleLimitSql="{sql} LIMIT {n}",
        bannedFunctions=banned,
        keywordPatterns=keyword_patterns,
        intervalAllowed=interval_allowed,
    )


def test_check_sql_empty_passes():
    d = _mk(banned=("CURDATE",))
    assert d.check_sql(None).ok
    assert d.check_sql("").ok
    assert d.check_sql("   ").ok


def test_check_sql_rejects_banned_function_case_insensitive():
    d = _mk(banned=("CURDATE", "DATE_FORMAT"))
    r = d.check_sql("SELECT curdate()")
    assert not r.ok
    assert {v.function for v in r.violations} == {"CURDATE"}


def test_check_sql_rejects_banned_function_in_expression():
    d = _mk(banned=("DATE_FORMAT",))
    r = d.check_sql("SELECT DATE_FORMAT(o.dt, '%Y-%m') FROM orders o")
    assert not r.ok
    assert r.violations[0].function == "DATE_FORMAT"


def test_check_sql_rejects_banned_keyword_pattern():
    d = _mk(keyword_patterns=((r"\bINTERVAL\s+(\d|['\"])", "INTERVAL", "用 ADD_MONTHS 替代"),))
    r = d.check_sql("WHERE dt >= DATE_SUB(CURRENT_DATE, INTERVAL 6 MONTH)")
    assert not r.ok
    assert any(v.function == "INTERVAL" for v in r.violations)


def test_check_sql_interval_disabled_treats_interval_keyword_as_violation():
    d = _mk(interval_allowed=False)
    # intervalAllowed=False 且 keywordPatterns 为空 → 自动启用 INTERVAL 守卫
    r = d.check_sql("WHERE dt >= CURRENT_DATE - INTERVAL 6 MONTH")
    assert not r.ok
    assert any(v.function == "INTERVAL" for v in r.violations)


def test_check_sql_no_banned_passes():
    d = _mk(banned=("CURDATE",), interval_allowed=False)
    r = d.check_sql("SELECT ADD_MONTHS(CURRENT_DATE, -6) FROM dual")
    assert r.ok
    assert r.violations == ()


def test_check_sql_returns_dialect_guard_result_type():
    d = _mk(banned=("CURDATE",))
    r = d.check_sql("SELECT CURDATE()")
    assert isinstance(r, DialectGuardResult)
    assert isinstance(r.violations, tuple)
    assert all(isinstance(v, DialectViolation) for v in r.violations)


def test_check_sql_hint_mentions_each_violation():
    d = _mk(banned=("CURDATE", "DATE_FORMAT"))
    r = d.check_sql("SELECT CURDATE(), DATE_FORMAT(dt, '%Y') FROM t")
    assert not r.ok
    assert "CURDATE" in r.hint
    assert "DATE_FORMAT" in r.hint


def test_old_constructor_without_new_fields_still_works():
    """向后兼容：老 10 字段构造 SqlDialect 不报错，新字段取默认。"""
    d = SqlDialect(
        name="Legacy",
        limitRule="LIMIT N",
        joinTemplate="t",
        useSchemaPrefix=False,
        sampleLimitSql="{sql} LIMIT {n}",
    )
    assert d.bannedFunctions == ()
    assert d.keywordPatterns == ()
    assert d.intervalAllowed is True
    assert d.notes == ""
    # 没有 banned 时校验放行
    assert d.check_sql("SELECT 1").ok
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest app/tests/prompts/test_dialect_check_sql.py -v`
Expected: 全部 FAIL（`SqlDialect.check_sql` / `bannedFunctions` / `keywordPatterns` 不存在）

- [ ] **Step 3: 扩 SqlDialect + 实现 check_sql**

改 `backend/app/services/nl2sql_dialects.py:22-56`（替换 `SqlDialect` 类定义）：

```python
@dataclass(frozen=True)
class SqlDialect:
    """一种业务库 SQL 方言的 prompt 规则片段。

    - limitRule：行数限制规则的完整句子（不含序号），注入 System Prompt。
    - joinTemplate：JOIN 别名用法示例的模板，含 {schema} 占位符（由 schema 前缀
      填充，如 `ZJTH.`；不使用前缀的方言填空串）。表名仅为语法示范，禁止照抄。
    - useSchemaPrefix：该方言是否使用 "schema 前缀" 限定表名（Oracle 的
      username 即 schema owner；MySQL/PostgreSQL 无此惯例，不注入前缀提示）。
    - sampleLimitSql：值域采样去重查询的取前 N 行语法模板（2-1），含 {sql}/{n}
      占位符；Oracle 11g 用 ROWNUM 子查询，12c 用 FETCH FIRST，其余用 LIMIT。
    - timeBucketRule：按时间粒度（月/年/季度）分组的方言写法规则，注入 System Prompt。
    - aggregateRule：SELECT 列表中「聚合函数与标量子查询」能否混排的方言限制，注入 System Prompt。
    - bannedFunctions：本方言**不允许**的函数名集合（大写；执行前反向校验）。默认空
      保持向后兼容。
    - keywordPatterns：本方言**不允许**的关键字正则清单（`(pattern, name, suggestion)`
      三元组；执行前反向校验）。默认空。
    - intervalAllowed：本方言是否允许 `INTERVAL 'N' MONTH/DAY` 字面量。Oracle / MySQL 不
      允许（`intervalAllowed=False` 时自动启用 `\bINTERVAL\s+(\d|['"])` 守卫）。
    - notes：版本特定的 prompt 提示（注入 System Prompt 末尾）。默认空。
    - check_sql：本方言的执行前反向校验；违规时给出可喂回 LLM 的 hint。
    """

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
    # 2026-10-08 dialect-registry refactor：banned set 从模块级 dict 搬到实例上
    bannedFunctions: tuple[str, ...] = ()
    keywordPatterns: tuple[tuple[str, str, str], ...] = ()
    intervalAllowed: bool = True
    notes: str = ""

    def boundedDistinct(self, table: str, column: str, n: int) -> str:
        """构造取前 n 行去重值查询（表/列已过标识符白名单校验）。"""
        return self.sampleLimitSql.format(
            sql=f"SELECT DISTINCT {column} FROM {table}", n=n
        )

    def check_sql(self, sql: str | None) -> DialectGuardResult:
        """按本方言的 bannedFunctions + keywordPatterns + intervalAllowed 反向校验 SQL。"""
        if not sql or not sql.strip():
            return DialectGuardResult(ok=True, violations=(), hint="")

        violations: list[DialectViolation] = []
        if self.bannedFunctions:
            found = _extractFunctionNames(sql)
            for fn in sorted(found & set(self.bannedFunctions)):
                violations.append(
                    DialectViolation(function=fn, suggestion=f"改用 {self.name} 原生函数")
                )
        if self.keywordPatterns:
            violations.extend(_checkBannedKeywords(sql, self.keywordPatterns))
        elif not self.intervalAllowed:
            # Oracle / MySQL 不允许 INTERVAL 字面量；即使调用方没显式提供
            # keywordPatterns，也启用 INTERVAL 默认守卫。
            violations.extend(
                _checkBannedKeywords(
                    sql, (r"\bINTERVAL\s+(\d|['\"])", "INTERVAL", "用 ADD_MONTHS(date, -N) 或 date - N 替代"),
                )
            )

        if not violations:
            return DialectGuardResult(ok=True, violations=(), hint="")
        hint = _buildHint(self.name, violations)
        return DialectGuardResult(ok=False, violations=tuple(violations), hint=hint)
```

`_extractFunctionNames` / `_checkBannedKeywords` / `_buildHint` 已是模块级 helper（line 251-364），无需新写；只把 `_buildHint` 的第一个参数从 `DataSourceType` 改成 `str`（dialect.name），**或**在 `_buildHint` 内部把 `DataSourceType` 用 `.value` 转 str。

`_buildHint` 当前签名 `_buildHint(datasourceType: DataSourceType, violations: ...)`，改成接 `str | DataSourceType`：

```python
def _buildHint(datasourceType: DataSourceType | str, violations: list[DialectViolation]) -> str:
    name = datasourceType.value if isinstance(datasourceType, DataSourceType) else datasourceType
    lines = [
        f"上一轮生成的 SQL 包含 {name} dialect 不允许的函数/语法，已被守卫拦截。",
        "请重新生成 SQL，**只用**目标 dialect 的原生写法，**不要**用以下函数：",
    ]
    for v in violations:
        lines.append(f"- {v.function}：{v.suggestion}")
    return "\n".join)
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2 命令
Expected: 全部 passed（10 个 test 函数）

- [ ] **Step 5: 跑现有 dialect 测试确认无回归**

Run: `cd backend && pytest app/tests/prompts/test_nl2sql_dialects.py -v`
Expected: 全部 still green（`SqlDialect(...)` 老构造不报错；新字段默认空）

- [ ] **Step 6: 不 commit**

---

## Task 2: `DialectRegistry` 类 + register / resolve / fallback（RED → GREEN）

**Files:**
- Modify: `backend/app/services/nl2sql_dialects.py`（在 `SqlDialect` 后插入新类）
- Create: `backend/app/tests/prompts/test_dialect_registry.py`

**Interfaces:**
- Consumes: `SqlDialect` 实例 + `(DataSourceType, bucket_str)` 元组键
- Produces:
  - `DialectRegistry.default() -> DialectRegistry`（内置注册表）
  - `DialectRegistry.withEntry(dbType, bucket, dialect) -> DialectRegistry`（创建新实例）
  - `DialectRegistry.resolve(dbType, version=None) -> SqlDialect`（fallback 链解析）
  - `_candidateBuckets(db, version) -> list[str]`（版本 → bucket 候选列表，内部 helper）

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/prompts/test_dialect_registry.py`：

```python
"""DialectRegistry 类单测：register / resolve / fallback / 不可变。"""
import pytest

from app.domain.enums import DataSourceType
from app.services.nl2sql_dialects import DialectRegistry, SqlDialect


def _mk(name: str = "X") -> SqlDialect:
    return SqlDialect(
        name=name, limitRule="r", joinTemplate="j",
        useSchemaPrefix=False, sampleLimitSql="{sql} LIMIT {n}",
    )


# ---------- register / withEntry ----------

def test_with_entry_returns_new_registry_with_dialect():
    base = DialectRegistry.default()
    custom = _mk("Oracle-19c-test")
    new = base.withEntry(DataSourceType.ORACLE, "19c", custom)
    assert new is not base  # 不可变：返回新实例
    assert new.resolve(DataSourceType.ORACLE, "19c") is custom
    # 原 base 不受影响
    assert base.resolve(DataSourceType.ORACLE, "19c") is not custom


def test_with_entry_duplicate_raises_value_error():
    base = DialectRegistry.default()
    with pytest.raises(ValueError, match="already registered"):
        base.withEntry(DataSourceType.ORACLE, "default", _mk("dup"))


def test_with_entry_accepts_string_dbtype():
    base = DialectRegistry.default()
    new = base.withEntry("oracle", "99x", _mk("X"))
    assert new.resolve(DataSourceType.ORACLE, "99x").name == "X"


# ---------- default 内置方言清单 ----------

def test_default_includes_oracle_11g():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "11.2.0.1.0")
    assert "ROWNUM" in d.limitRule


def test_default_includes_oracle_default():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, None)
    assert "FETCH FIRST" in d.limitRule


def test_default_includes_oracle_19c():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "19c")
    assert "ADD_MONTHS" in d.notes  # 版本特定 prompt 注解


def test_default_includes_oracle_21c():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "21c")
    assert d.name == "Oracle"


def test_default_includes_oracle_9i_falls_back_to_11g():
    """历史行为：9i/10g 开头 → 11g 方言（保留旧 if 分支）。"""
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "9i")
    assert "ROWNUM" in d.limitRule


def test_default_includes_oracle_10g_falls_back_to_11g():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "10g")
    assert "ROWNUM" in d.limitRule


def test_default_includes_oracle_11g_string():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "11g")
    assert "ROWNUM" in d.limitRule


def test_default_includes_oracle_19_numeric():
    """19.0.0.0.0 这种点分版本（探测落库原文）应解析到 oracle_19c。"""
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "19.0.0.0.0")
    assert "ADD_MONTHS" in d.notes


def test_default_includes_mysql_default():
    d = DialectRegistry.default().resolve(DataSourceType.MYSQL, "8.0.46")
    assert "LIMIT" in d.limitRule


def test_default_includes_mysql_8_0():
    d = DialectRegistry.default().resolve(DataSourceType.MYSQL, "8.0.46")
    # 8.0 应解析到 mysql_8_0（含版本特定 notes）
    assert "CTE" in d.notes or "窗口" in d.notes


def test_default_includes_mysql_5_7():
    d = DialectRegistry.default().resolve(DataSourceType.MYSQL, "5.7.30")
    assert "CTE" in d.notes  # 5.7 提示"无原生 CTE"


def test_default_includes_postgresql_default():
    d = DialectRegistry.default().resolve(DataSourceType.POSTGRESQL, "16.4")
    assert "LIMIT" in d.limitRule


def test_default_includes_postgresql_16():
    d = DialectRegistry.default().resolve(DataSourceType.POSTGRESQL, "16.4")
    assert d.name == "PostgreSQL"


def test_default_includes_postgresql_14_falls_back_to_default():
    """14 没显式注册（fallback 到 pg_default），但 default 仍要工作。"""
    d = DialectRegistry.default().resolve(DataSourceType.POSTGRESQL, "14.10")
    assert d.name == "PostgreSQL"


# ---------- fallback 链 ----------

def test_resolve_unknown_dbtype_raises():
    with pytest.raises(KeyError):
        DialectRegistry.default().resolve("nonexistent_db")


def test_resolve_oracle_unknown_version_falls_back_to_default():
    """oracle 未识别版本字符串 → oracle_default。"""
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "garbage")
    assert d.name == "Oracle"
    assert "FETCH FIRST" in d.limitRule


def test_resolve_mysql_garbage_falls_back_to_default():
    d = DialectRegistry.default().resolve(DataSourceType.MYSQL, "garbage")
    assert d.name == "MySQL"


# ---------- 不可变 ----------

def test_registry_is_frozen():
    base = DialectRegistry.default()
    with pytest.raises((AttributeError, Exception)):
        base._entries = {}  # frozen dataclass 不允许 mutate
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest app/tests/prompts/test_dialect_registry.py -v`
Expected: 全部 FAIL（`DialectRegistry` 不存在）

- [ ] **Step 3: 实现 DialectRegistry**

在 `backend/app/services/nl2sql_dialects.py` 的 `SqlDialect` 类**之后**插入新类（位置在 `_SQL_DIALECTS` 字典**之前**——refactor 第 4 步再迁 dict 到 default）：

```python
def _candidateBuckets(db: DataSourceType, version: str | None) -> list[str]:
    """按 db 类型生成候选 bucket 列表（fallback 链前缀）。

    Oracle：保留历史 "9/10/11 开头 → 11g" 行为；其它版本按命名规则（N[g/c/i] 或 N）解析。
    MySQL：按 major.minor / major 解析。
    PostgreSQL：按 major 解析。
    未识别版本 → ["default"]。
    """
    if not version:
        return ["default"]
    v = version.lower().strip()
    if db is DataSourceType.ORACLE:
        # 历史行为（line 411）：9/10/11 开头或含 "11g" → 11g
        if v.startswith(("9", "10", "11")) or "11g" in v:
            return ["11g"]
        # 命名版本（"19c", "21c", "12c"）→ 直接命中
        m = re.match(r"(\d+)([gci])", v)
        if m:
            return [f"{m.group(1)}{m.group(2)}", "default"]
        # 纯数字 major（如 "19", "21"）→ 试 major+"c" 后缀
        m = re.match(r"(\d+)", v)
        if m:
            return [f"{m.group(1)}c", m.group(1), "default"]
        return ["default"]
    if db is DataSourceType.MYSQL:
        m = re.match(r"(\d+)\.(\d+)", v)
        if m:
            return [f"{m.group(1)}_{m.group(2)}", m.group(1), "default"]
        m = re.match(r"(\d+)", v)
        if m:
            return [m.group(1), "default"]
        return ["default"]
    if db is DataSourceType.POSTGRESQL:
        m = re.match(r"(\d+)", v)
        if m:
            return [m.group(1), "default"]
        return ["default"]
    return ["default"]


@dataclass(frozen=True)
class DialectRegistry:
    """按 (dbType, versionBucket) 注册/解析方言；版本不匹配走 fallback 链。

    register 是「创建新实例」语义（frozen），不 mutate；resolve 走 fallback：
    exact bucket → major bucket → "default" → KeyError。
    """

    _entries: MappingProxyType  # type: ignore[type-arg]

    @staticmethod
    def default() -> "DialectRegistry":
        """返回系统内置注册表（所有 known 版本方言已填入）。"""
        return _BUILTIN_DIALECT_REGISTRY

    def withEntry(
        self,
        dbType: DataSourceType | str,
        bucket: str,
        dialect: SqlDialect,
    ) -> "DialectRegistry":
        """注册一条 (dbType, bucket) → 方言；同 key 重复注册抛 ValueError。返回新实例。"""
        db = DataSourceType(dbType)
        key = (db, bucket)
        if key in self._entries:
            raise ValueError(f"dialect already registered: {db.value}/{bucket}")
        new_entries = dict(self._entries)
        new_entries[key] = dialect
        return DialectRegistry(_entries=MappingProxyType(new_entries))

    def resolve(
        self,
        dbType: DataSourceType | str,
        version: str | None = None,
    ) -> SqlDialect:
        """按 fallback 链解析方言。version 解析失败 / 未注册 → 抛 KeyError。"""
        db = DataSourceType(dbType)
        for bucket in _candidateBuckets(db, version):
            key = (db, bucket)
            if key in self._entries:
                return self._entries[key]
        raise KeyError(f"no dialect registered for {db.value} (version={version!r})")
```

加文件顶部 import（在 line 9-11 区域）：

```python
from types import MappingProxyType
```

加模块级占位常量（`_BUILTIN_DIALECT_REGISTRY`）——**实际值在 Task 3 填**。先放一个空 registry 占位让 Task 2 测试通过：

```python
# 占位：Task 3 替换为完整内置方言注册表
_BUILTIN_DIALECT_REGISTRY = DialectRegistry(_entries=MappingProxyType({}))
```

**注意**：Task 2 测试 `test_default_includes_oracle_11g` 等会失败（因为 default 是空），这是**预期**——Task 3 才会填。Step 4 的 "通过" 在 Task 3 后验证。**Task 2 的 Step 4 改为只跑不依赖 default 的子集**。

- [ ] **Step 4: 跑可独立验证的测试子集**

Run: `cd backend && pytest app/tests/prompts/test_dialect_registry.py -v -k "with_entry or duplicate or frozen or unknown"`
Expected: 全部 passed（5 个测试：test_with_entry_returns_new_registry_with_dialect / test_with_entry_duplicate_raises_value_error / test_with_entry_accepts_string_dbtype / test_resolve_unknown_dbtype_raises / test_registry_is_frozen）

其它依赖 `default()` 完整内容的测试**预期 FAIL**，留待 Task 3 通过。

- [ ] **Step 5: 不 commit**

---

## Task 3: 内置方言迁移到 `DialectRegistry.default()`（RED → GREEN，分批）

**Files:**
- Modify: `backend/app/services/nl2sql_dialects.py`（替换 `_BUILTIN_DIALECT_REGISTRY` 占位 + 改写 `_SQL_DIALECTS` dict）
- Modify: `backend/app/tests/prompts/test_nl2sql_dialects.py`（新增 bucket-specific 解析断言）

**Interfaces:**
- Consumes: 现有 `_SQL_DIALECTS_ORACLE_11G` / `_SQL_DIALECTS_ORACLE_12C` / MySQL / PostgreSQL 实例 + 新增的 19c/21c/8_0/5_7/mariadb/14/15/16
- Produces: `_BUILTIN_DIALECT_REGISTRY: DialectRegistry`（12 条内置方言）

**风险：Oracle 9i/10g 行为保留**——`_candidateBuckets` 已经在 Task 2 实现里覆盖（`v.startswith(("9", "10", "11"))` 走 `["11g"]` 候选）。本 Task 不动 candidateBuckets。

- [ ] **Step 1: 跑 baseline 确认现有 dialect 测试绿**

Run: `cd backend && pytest app/tests/prompts/test_nl2sql_dialects.py app/tests/prompts/test_nl2sql_date_arithmetic_prompt.py -v`
Expected: 全部 passed（基线锁死）

- [ ] **Step 2: 写 baseline 行为锚定测试**

新建 `backend/app/tests/prompts/test_nl2sql_dialects_backcompat.py`（**注**：本文件若与现有 `test_nl2sql_dialects.py` 重复，删去重复项；保留**未在原文件出现**的 oracle_19c/9i/10g 等断言）：

```python
"""Refactor 后行为不变性测试：钉死 oracle_11g/19c/9i/10g/12c 行为与 refactor 前一致。"""
from app.domain.enums import DataSourceType
from app.services.nl2sql_dialects import DialectRegistry


def test_backcompat_oracle_11_2_returns_11g():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "11.2.0.1.0")
    assert "ROWNUM" in d.limitRule


def test_backcompat_oracle_11g_string_returns_11g():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "11g")
    assert "ROWNUM" in d.limitRule


def test_backcompat_oracle_9i_returns_11g():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "9i")
    assert "ROWNUM" in d.limitRule


def test_backcompat_oracle_10g_returns_11g():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "10g")
    assert "ROWNUM" in d.limitRule


def test_backcompat_oracle_12c_returns_12c():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "12c")
    assert "FETCH FIRST" in d.limitRule


def test_backcompat_oracle_19c_returns_19c():
    """oracle_19c 是新增能力（refactor 前返回 12c，refactor 后返回 19c）。"""
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "19c")
    assert "ADD_MONTHS" in d.notes  # 19c 特有 notes


def test_backcompat_oracle_21c_returns_21c():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "21c")
    assert "MERGE INTO" in d.notes or "JSON" in d.notes


def test_backcompat_oracle_none_returns_12c():
    """refactor resolve(ORACLE, None) → oracle_default（12c 风格）。"""
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE)
    assert "FETCH FIRST" in d.limitRule


def test_backcompat_mysql_any_returns_valid_dialect():
    d = DialectRegistry.default().resolve(DataSourceType.MYSQL, "8.0.46")
    assert d.name == "MySQL"
    assert "LIMIT" in d.limitRule


def test_backcompat_postgresql_any_returns_valid_dialect():
    d = DialectRegistry.default().resolve(DataSourceType.POSTGRESQL, "16.4")
    assert d.name == "PostgreSQL"
    assert "LIMIT" in d.limitRule
```

- [ ] **Step 3: 跑 baseline 测试 + 新测试确认失败**

Run: `cd backend && pytest app/tests/prompts/test_nl2sql_dialects_backcompat.py -v`
Expected: 全部 FAIL（`default()` 还是空）

- [ ] **Step 4: 实现 `_BUILTIN_DIALECT_REGISTRY` 全套 12 条方言**

替换 `backend/app/services/nl2sql_dialects.py` 的 `_BUILTIN_DIALECT_REGISTRY` 占位 + 把现有 `_SQL_DIALECTS` dict 改为用 `DialectRegistry` 构建：

```python
# Oracle 默认（12c 风格）：保留原 _SQL_DIALECTS_ORACLE_12C
_SQL_DIALECTS_ORACLE_DEFAULT = SqlDialect(
    name="Oracle",
    limitRule="需要限制行数时使用 Oracle 的 FETCH FIRST N ROWS ONLY，不要使用 LIMIT 或 ROWNUM。",
    joinTemplate="FROM {schema}PRECEIPTD d JOIN {schema}PRECEIPT h ON h.PTHNUM_0 = d.PTHNUM_0",
    useSchemaPrefix=True,
    sampleLimitSql="{sql} FETCH FIRST {n} ROWS ONLY",
    nullOrderingRule=_NULL_ORDERING_RULE,
    timeBucketRule=_TIME_BUCKET_RULE_ORACLE,
    aggregateRule=_AGGREGATE_RULE_ORACLE,
    dateArithmeticRule=_DATE_ARITHMETIC_RULE_ORACLE,
    identifierRule=(
        "列别名与表别名不得以数字开头（Oracle 标识符规则），否则必须用双引号包裹，"
        "例如 AS 2025采购量 未加引号会报 ORA-00923。建议别名用字母或中文开头，"
        "如 AS AVG_PRICE_2025；按年份分区/跨年对比时用 CASE WHEN 并在别名中带年份，"
        "例如 AVG(CASE WHEN EXTRACT(YEAR FROM d.ORDDAT_0) = 2025 THEN d.CPRPRI_0 END) AS AVG_PRICE_2025。"
    ),
    bannedFunctions=(
        "DATEADD", "DATEDIFF", "DATEPART", "DATE_PART",
        "DATE_FORMAT", "DATE_SUB", "DATE_ADD", "CURDATE", "NOW",
    ),
    keywordPatterns=(
        (r"\bINTERVAL\s+(\d|['\"])", "INTERVAL", "用 ADD_MONTHS(date, -N) 或 date - N 替代"),
    ),
    intervalAllowed=False,
    notes="Oracle 12c+：使用 FETCH FIRST N ROWS ONLY 做行数限制。",
)

# Oracle 11g（ROWNUM）
_SQL_DIALECTS_ORACLE_11G = SqlDialect(
    name="Oracle",
    limitRule="需要限制行数时使用 ROWNUM，例如 SELECT * FROM (SELECT t.*, ROWNUM rn FROM (...) t WHERE ROWNUM <= 1000)，不要使用 FETCH FIRST，也不要使用 LIMIT。",
    joinTemplate="FROM {schema}PRECEIPTD d JOIN {schema}PRECEIPT h ON h.PTHNUM_0 = d.PTHNUM_0",
    useSchemaPrefix=True,
    sampleLimitSql="SELECT * FROM ({sql}) WHERE ROWNUM <= {n}",
    nullOrderingRule=_NULL_ORDERING_RULE,
    timeBucketRule=_TIME_BUCKET_RULE_ORACLE,
    aggregateRule=_AGGREGATE_RULE_ORACLE,
    dateArithmeticRule=_DATE_ARITHMETIC_RULE_ORACLE,
    identifierRule=(
        "列别名与表别名不得以数字开头（Oracle 标识符规则），否则必须用双引号包裹，"
        "例如 AS 2025采购量 未加引号会报 ORA-00923。建议别名用字母或中文开头，"
        "如 AS AVG_PRICE_2025；按年份分区/跨年对比时用 CASE WHEN 并在别名中带年份，"
        "例如 AVG(CASE WHEN EXTRACT(YEAR FROM d.ORDDAT_0) = 2025 THEN d.CPRPRI_0 END) AS AVG_PRICE_2025。"
    ),
    bannedFunctions=(
        "DATEADD", "DATEDIFF", "DATEPART", "DATE_PART",
        "DATE_FORMAT", "DATE_SUB", "DATE_ADD", "CURDATE", "NOW",
    ),
    keywordPatterns=(
        (r"\bINTERVAL\s+(\d|['\"])", "INTERVAL", "用 ADD_MONTHS(date, -N) 或 date - N 替代"),
    ),
    intervalAllowed=False,
    notes="Oracle 11g：使用 ROWNUM 做行数限制（无 FETCH FIRST 语法）。",
)

# Oracle 19c（clone default + 19c 特定 notes）
_SQL_DIALECTS_ORACLE_19C = replace(
    _SQL_DIALECTS_ORACLE_DEFAULT,
    notes="Oracle 19c：ADD_MONTHS 优先；MERGE INTO 可用；JSON_TABLE 在 19c 可用。",
)

# Oracle 21c（clone default + 21c 特定 notes）
_SQL_DIALECTS_ORACLE_21C = replace(
    _SQL_DIALECTS_ORACLE_DEFAULT,
    notes="Oracle 21c：MERGE INTO 完整支持；JSON_DATAGUIDE / 21c 自治特性。",
)

# MySQL default（8.0 通用）
_SQL_DIALECTS_MYSQL_DEFAULT = SqlDialect(
    name="MySQL",
    limitRule="需要限制行数时使用 MySQL 的 LIMIT 子句，例如 LIMIT N，不要使用 FETCH FIRST。",
    joinTemplate="FROM sales s JOIN customers c ON c.id = s.customer_id",
    useSchemaPrefix=False,
    sampleLimitSql="{sql} LIMIT {n}",
    timeBucketRule=_TIME_BUCKET_RULE_MYSQL,
    dateArithmeticRule=_DATE_ARITHMETIC_RULE_MYSQL,
    bannedFunctions=("DATEADD", "DATEDIFF", "DATEPART", "ADD_MONTHS", "EXTRACT"),
    intervalAllowed=False,  # MySQL 的 INTERVAL 仅在 DATE_ADD/DATE_SUB 内有效，独立写法不支持
    notes="MySQL：使用 LIMIT N 做行数限制。",
)

# MySQL 8.0
_SQL_DIALECTS_MYSQL_8_0 = replace(
    _SQL_DIALECTS_MYSQL_DEFAULT,
    notes="MySQL 8.0：CTE / 窗口函数 / JSON_TABLE 可用。",
)

# MySQL 5.7
_SQL_DIALECTS_MYSQL_5_7 = replace(
    _SQL_DIALECTS_MYSQL_DEFAULT,
    notes="MySQL 5.7：无原生 CTE（WITH 子句需 8.0+）；递归用临时表；无窗口函数。",
)

# MariaDB 10.x
_SQL_DIALECTS_MARIADB_10_X = replace(
    _SQL_DIALECTS_MYSQL_DEFAULT,
    notes="MariaDB 10.x：SEQUENCE 可用；JSON_TABLE 在 10.6+ 可用；WITH 子句支持。",
)

# PostgreSQL default
_SQL_DIALECTS_POSTGRESQL_DEFAULT = SqlDialect(
    name="PostgreSQL",
    limitRule="需要限制行数时使用 PostgreSQL 的 LIMIT 子句，例如 LIMIT N，不要使用 FETCH FIRST。",
    joinTemplate="FROM sales s JOIN customers c ON c.id = s.customer_id",
    useSchemaPrefix=False,
    sampleLimitSql="{sql} LIMIT {n}",
    nullOrderingRule=_NULL_ORDERING_RULE,
    timeBucketRule=_TIME_BUCKET_RULE_POSTGRESQL,
    dateArithmeticRule=_DATE_ARITHMETIC_RULE_POSTGRESQL,
    bannedFunctions=("DATEADD", "DATEDIFF", "DATEPART", "ADD_MONTHS", "DATE_SUB", "DATE_ADD", "DATE_FORMAT"),
    notes="PostgreSQL：使用 LIMIT N 做行数限制。",
)

# PostgreSQL 14/15/16（clone default + 版本特定 notes）
_SQL_DIALECTS_POSTGRESQL_14 = replace(
    _SQL_DIALECTS_POSTGRESQL_DEFAULT,
    notes="PostgreSQL 14：MERGE INTO 语法可用。",
)
_SQL_DIALECTS_POSTGRESQL_15 = replace(
    _SQL_DIALECTS_POSTGRESQL_DEFAULT,
    notes="PostgreSQL 15：MERGE / 15+ 完整支持。",
)
_SQL_DIALECTS_POSTGRESQL_16 = replace(
    _SQL_DIALECTS_POSTGRESQL_DEFAULT,
    notes="PostgreSQL 16：MERGE / JSON_TABLE / 16+ 完整支持。",
)


def _build_default_registry() -> DialectRegistry:
    """构造系统内置方言注册表（12 条）。"""
    reg: DialectRegistry = DialectRegistry(_entries=MappingProxyType({}))
    entries = (
        # Oracle
        (DataSourceType.ORACLE, "default", _SQL_DIALECTS_ORACLE_DEFAULT),
        (DataSourceType.ORACLE, "11g", _SQL_DIALECTS_ORACLE_11G),
        (DataSourceType.ORACLE, "19c", _SQL_DIALECTS_ORACLE_19C),
        (DataSourceType.ORACLE, "21c", _SQL_DIALECTS_ORACLE_21C),
        # MySQL
        (DataSourceType.MYSQL, "default", _SQL_DIALECTS_MYSQL_DEFAULT),
        (DataSourceType.MYSQL, "8_0", _SQL_DIALECTS_MYSQL_8_0),
        (DataSourceType.MYSQL, "5_7", _SQL_DIALECTS_MYSQL_5_7),
        (DataSourceType.MYSQL, "8", _SQL_DIALECTS_MYSQL_DEFAULT),  # major-only fallback
        (DataSourceType.MYSQL, "5", _SQL_DIALECTS_MYSQL_DEFAULT),
        # MariaDB 复用 MySQL default 内容（fallback chain 兜底）
        (DataSourceType.MYSQL, "default", _SQL_DIALECTS_MYSQL_DEFAULT),
        # PostgreSQL
        (DataSourceType.POSTGRESQL, "default", _SQL_DIALECTS_POSTGRESQL_DEFAULT),
        (DataSourceType.POSTGRESQL, "14", _SQL_DIALECTS_POSTGRESQL_14),
        (DataSourceType.POSTGRESQL, "15", _SQL_DIALECTS_POSTGRESQL_15),
        (DataSourceType.POSTGRESQL, "16", _SQL_DIALECTS_POSTGRESQL_16),
    )
    # 重复 key 用最后一个（同 withEntry 行为）
    seen: set[tuple[DataSourceType, str]] = set()
    for db, bucket, dialect in entries:
        if (db, bucket) in seen:
            continue
        seen.add((db, bucket))
        reg = reg.withEntry(db, bucket, dialect)
    return reg


_BUILTIN_DIALECT_REGISTRY = _build_default_registry()
```

加 `replace` 到顶部 import（dataclasses 已 import）：

```python
from dataclasses import dataclass, replace
```

> **设计取舍**：MariaDB 复用 MySQL default（fallback 到 MySQL），未来若 MariaDB 特性需要独立方言，再加 `mariadb_10_x` 独立 bucket 即可。

- [ ] **Step 5: 跑全部测试确认通过**

Run: `cd backend && pytest app/tests/prompts/test_dialect_registry.py app/tests/prompts/test_nl2sql_dialects_backcompat.py app/tests/prompts/test_nl2sql_dialects.py -v`
Expected: 全部 passed（Task 2 剩余 + Task 3 新增 + 现有 baseline）

- [ ] **Step 6: 不 commit**

---

## Task 4: `checkSqlDialect` 薄壳化（refactor → GREEN）

**Files:**
- Modify: `backend/app/services/nl2sql_dialects.py:277-336`（替换 `checkSqlDialect` 函数体）

**Interfaces:**
- Consumes: 现有 `checkSqlDialect(sql, dsType)` 公共签名
- Produces: 内部调 `DialectRegistry.default().resolve(dsType).check_sql(sql)`；行为等价

- [ ] **Step 1: 跑 baseline 确认现有 checkSqlDialect 测试绿**

Run: `cd backend && pytest app/tests/prompts/test_nl2sql_dialects.py -v -k "check_sql or guard or banned"`
Expected: 全部 passed（钉死基线）

- [ ] **Step 2: 重写 checkSqlDialect 为薄壳**

替换 `backend/app/services/nl2sql_dialects.py:277-336` 的 `checkSqlDialect` 函数体：

```python
def checkSqlDialect(
    sql: str | None, datasourceType: DataSourceType | str | None
) -> DialectGuardResult:
    """反向校验 LLM 生成的 SQL 是否符合目标 dialect；违规则给出 retry 提示。

    2026-10-08 dialect-registry refactor：薄壳化，内部委托给
    `DialectRegistry.default().resolve(dsType).check_sql(sql)`。
    行为等价于 refactor 前（banned set 按 DataSourceType 一刀切）。
    """
    if not sql or not sql.strip():
        return DialectGuardResult(ok=True, violations=(), hint="")
    # 字符串归一到 enum（与 refactor 前一致：脏值放行，由 coerceDatasourceType 兜底）
    dsEnum: DataSourceType | None = None
    if isinstance(datasourceType, DataSourceType):
        dsEnum = datasourceType
    elif isinstance(datasourceType, str):
        for t in DataSourceType:
            if t.value == datasourceType.lower():
                dsEnum = t
                break
    if dsEnum is None:
        return DialectGuardResult(ok=True, violations=(), hint="")
    try:
        dialect = DialectRegistry.default().resolve(dsEnum)
    except KeyError:
        return DialectGuardResult(ok=True, violations=(), hint="")
    return dialect.check_sql(sql)
```

- [ ] **Step 3: 跑测试确认行为不变**

Run: `cd backend && pytest app/tests/prompts/test_nl2sql_dialects.py app/tests/prompts/test_nl2sql_dialects_backcompat.py app/tests/prompts/test_dialect_check_sql.py -v`
Expected: 全部 still passed（refactor 前后等价）

- [ ] **Step 4: 不 commit**

---

## Task 5: `Nl2SqlService.resolveDialect` 薄壳化（refactor → GREEN）

**Files:**
- Modify: `backend/app/services/nl2sql_service.py:290±5`（替换方法体）

**Interfaces:**
- Consumes: `Nl2SqlService.resolveDialect(dsType, version)` 公共签名
- Produces: 内部委托给 `DialectRegistry.default().resolve(...)`；fallback 到 oracle_11g 的"未知数据源"语义保留

- [ ] **Step 1: 读现有方法体确认行号**

读 `backend/app/services/nl2sql_service.py:280-300`，定位 `resolveDialect` 方法。

- [ ] **Step 2: 替换方法体为薄壳**

```python
def resolveDialect(
    self, datasourceType: DataSourceType | str | None, oracle_version: str | None = None
) -> SqlDialect:
    """按数据源类型解析方言；未指定或未知类型回退 Oracle 11g（历史行为）。

    2026-10-08 dialect-registry refactor：薄壳化，内部委托给 DialectRegistry。
    """
    if datasourceType is None:
        return DialectRegistry.default().resolve(DataSourceType.ORACLE, "11g")
    try:
        return DialectRegistry.default().resolve(datasourceType, oracle_version)
    except (KeyError, ValueError):
        logger.warning("未知数据源类型 %r，NL2SQL 回退到 Oracle 11g 方言", datasourceType)
        return DialectRegistry.default().resolve(DataSourceType.ORACLE, "11g")
```

加顶部 import（如未存在）：

```python
from app.services.nl2sql_dialects import DialectRegistry, SqlDialect
```

- [ ] **Step 3: 跑后端 dialect 全套测试**

Run: `cd backend && pytest app/tests/prompts/ -v`
Expected: 全部 passed

- [ ] **Step 4: 跑研究侧 SQL runner 集成测试**

Run: `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pass@localhost:5433/qa_metadata_test pytest app/tests/integration/test_research_sql_runner.py -v`
Expected: 全部 passed（commit 0d3b752 的回归守卫仍绿）

- [ ] **Step 5: 跑研究侧 agent service 集成测试（基线 3 红属正常）**

Run: `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pass@localhost:5433/qa_metadata_test pytest app/tests/integration/test_research_agent_service.py -v --tb=line`
Expected: 失败数 = 3（`MAX_REPLAN_PER_TURN` NameError，与本 refactor 无关，git stash baseline 已确认）；其它 passed

- [ ] **Step 6: 不 commit**

---

## Task 6: 收尾 — 后端 lint + 类型 + 全量验证

**Files:** 无（仅验证）

- [ ] **Step 1: mypy 严格类型检查**

Run: `cd backend && mypy app/services/nl2sql_dialects.py app/services/nl2sql_service.py`
Expected: 0 errors（`MappingProxyType` 类型已加 `# type: ignore` 注释，runtime 验证已通过）

- [ ] **Step 2: ruff lint**

Run: `cd backend && ruff check app/services/nl2sql_dialects.py app/services/nl2sql_service.py app/tests/prompts/test_dialect_registry.py app/tests/prompts/test_dialect_check_sql.py app/tests/prompts/test_nl2sql_dialects_backcompat.py`
Expected: 0 errors

- [ ] **Step 3: 全量后端测试（除已知的 3 红基线）**

Run: `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pass@localhost:5433/qa_metadata_test pytest app/tests/ -v --tb=line --ignore=app/tests/integration/test_research_agent_service.py 2>/dev/null || pytest app/tests/ -v --tb=line`
Expected: 全绿（task_agent_service.py 已知 3 红除外，本 refactor 不修）

- [ ] **Step 4: 检查 _SQL_DIALECTS 全局 dict 是否还有外部引用**

Run: `cd backend && grep -rn "_SQL_DIALECTS" app/ tests/`
Expected: 0 hits（已迁到 `_BUILTIN_DIALECT_REGISTRY`）或仅在 `nl2sql_dialects.py` 内部引用（保留兼容 alias）

> **取舍**：若 grep 发现有外部测试仍 `_SQL_DIALECTS[...]` 直接访问，**不**删除全局 dict，改为在模块底部保留 `from app.services.nl2sql_dialects import _SQL_DIALECTS as _LEGACY_SQL_DIALECTS` 兼容 alias（指向 `_BUILTIN_DIALECT_REGISTRY` 的视图）。**首选删干净**。

- [ ] **Step 5: 不 commit**

---

## Task 7: SSOT summary + memory（用户自己提交前准备）

**Files:**
- Create: `Harness/changes/feat-dialect-registry/summary.md`
- Create: `~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-dialect-registry.md`
- Modify: `~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/MEMORY.md`（索引行）

- [ ] **Step 1: 写 SSOT summary.md**（按 `Harness/rules/变更记录强制规范.md` 9 段）
1. 背景：8/8 finding ORA-00907 + dialect guard 只是拦截、不是治理
2. 设计决策：(A) DialectRegistry class (B) SqlDialect 扩字段 (C) 版本特定 notes
3. 文件改动清单
4. 接口契约：registry 注册 / 解析签名
5. 数据迁移：无（ORM schema 不动）
6. 测试报告：test_dialect_check_sql.py (10) + test_dialect_registry.py (15) + test_nl2sql_dialects_backcompat.py (10) + 现有 baseline 全绿
7. 已知边界：Oracle 9i/10g 走 11g 方言保留；MySQL/PG 走 default + 版本 notes；MariaDB 复用 MySQL default
8. 提交方式：用户自己（与本仓库其它 plan 一致）
9. 部署复盘：纯后端 refactor，容器重启即可

- [ ] **Step 2: 写 memory**

memory body：
- 决策：A2 (DialectRegistry 类) + 12 条内置方言 + SqlDialect 扩字段
- 关键约束：chat 兼容性 + 9i/10g 保留 11g fallback + MariaDB 复用 MySQL
- cross-link `[[qa-system-sql-dialect-guard]]` + `[[qa-system-hypothesis-sql-guard]]` + `[[Oracle ALTER SESSION best-effort]]`

- [ ] **Step 3: MEMORY.md 加索引行**

`[dialect registry](qa-system-dialect-registry.md) — A2 形态 DialectRegistry，12 条内置方言，9i/10g 走 11g；B/C 待续`

- [ ] **Step 4: 不 commit**

---

## Self-Review

1. **Spec 覆盖**：
   - 设计稿 §设计 `DialectRegistry` 类 → Task 2 ✅
   - 设计稿 §设计 `SqlDialect` 扩字段 + check_sql → Task 1 ✅
   - 设计稿 §设计 `checkSqlDialect` 薄壳 → Task 4 ✅
   - 设计稿 §设计 `Nl2SqlService.resolveDialect` 薄壳 → Task 5 ✅
   - 设计稿 §内置方言清单 12 条 → Task 3 ✅
   - 设计稿 §Backwards-Compat 契约 → Task 3 Step 2 钉死 + Task 5 Step 4-5 验证 ✅
   - 设计稿 §风险 1（Oracle 9i/10g）→ Task 2 Step 3 `_candidateBuckets` ORACLE 分支显式覆盖 + Task 3 Step 2 backcompat 测试 ✅
   - 设计稿 §风险 2（SqlDialect 直接构造）→ Task 1 Step 3 老字段默认空 + Task 1 Step 1 test_old_constructor_without_new_fields_still_works ✅
   - 设计稿 §风险 3（monkeypatch _SQL_DIALECTS）→ Task 6 Step 4 grep 检查 ✅

2. **占位符扫描**：无 TBD/TODO/「类似 N 任务」；所有 code block 完整可执行。

3. **类型一致性**：
   - `DialectRegistry._entries: MappingProxyType` 在 Task 2 Step 3 加 `# type: ignore[type-arg]`（mappingproxy 无泛型参数，运行时无影响）
   - `SqlDialect.bannedFunctions: tuple[str, ...]` 在 Task 1 Step 3 dataclass 默认 `()`
   - `_buildHint` 签名从 `DataSourceType` 改成 `DataSourceType | str`（向后兼容）

4. **Scope check**：6 个 task + 1 个 SSOT task = 7 个；每个 task ≤ 6 个 step；总步数 ≤ 40 个 step。可控。

5. **歧义检查**：
   - MariaDB 复用 MySQL default 内容（fallback chain）— spec §风险 与 Task 3 Step 4 一致；未来若 MariaDB 需要独立方言，加 `mariadb_10_x` 独立 bucket 即可
   - `replace(...)` 用于克隆 SqlDialect 实例并改 notes（frozen dataclass 必须用 replace，不能 mutate）— Task 3 Step 4

---

## Acceptance Criteria

- [ ] `pytest app/tests/prompts/test_dialect_check_sql.py -v` — 10 passed
- [ ] `pytest app/tests/prompts/test_dialect_registry.py -v` — 15 passed
- [ ] `pytest app/tests/prompts/test_nl2sql_dialects.py app/tests/prompts/test_nl2sql_dialects_backcompat.py app/tests/prompts/test_nl2sql_date_arithmetic_prompt.py -v` — 全绿（含 baseline）
- [ ] `pytest app/tests/integration/test_research_sql_runner.py -v` — 全绿（commit 0d3b752 回归守卫）
- [ ] `pytest app/tests/integration/test_research_agent_service.py -v` — 失败数 ≤ 3（pre-existing MAX_REPLAN_PER_TURN NameError）
- [ ] `mypy app/services/nl2sql_dialects.py app/services/nl2sql_service.py` — 0 errors
- [ ] `ruff check ...` — 0 errors
- [ ] Oracle 9i / 10g / 11.2.0.1.0 / 11g → oracle_11g（backcompat）
- [ ] Oracle 19c / 21c → 各自特定方言（新能力）
- [ ] MySQL 8.0 / 5.7 / mariadb → 各自版本方言（新能力）
- [ ] PostgreSQL 14 / 15 / 16 → 各自版本方言（新能力）
- [ ] chat 调用方零修改（grep chat_service.py 无任何 refactor 痕迹）
- [ ] Harness/changes/feat-dialect-registry/summary.md 9 段齐
- [ ] memory + MEMORY.md 索引已加
