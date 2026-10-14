"""DialectRegistry 类单测：register / resolve / fallback / 不可变。

2026-10-08 dialect-registry refactor：把 _DIALECT_SEEDS dict + Oracle-only if
重构成显式 DialectRegistry；按 (DataSourceType, versionBucket) 注册/解析，
fallback 链 exact → major → "default" → KeyError。
"""
import pytest

from app.domain.enums import DataSourceType
from app.services.nl2sql_dialects import DialectRegistry, SqlDialect


def _mk(name: str = "X") -> SqlDialect:
    return SqlDialect(
        name=name,
        limitRule="r",
        joinTemplate="j",
        useSchemaPrefix=False,
        sampleLimitSql="{sql} LIMIT {n}",
    )


# ---------- register / withEntry ----------


def test_with_entry_returns_new_registry_with_dialect():
    base = DialectRegistry.default()
    custom = _mk("Oracle-19c-test")
    # ORACLE "19c" 已在 default 注册；用唯一 bucket 名验证 withEntry 注入
    new = base.withEntry(DataSourceType.ORACLE, "19c_override_test", custom)
    assert new is not base  # 不可变：返回新实例
    assert new._entries[(DataSourceType.ORACLE, "19c_override_test")] is custom
    # 原 base 不受影响
    assert (DataSourceType.ORACLE, "19c_override_test") not in base._entries


def test_with_entry_duplicate_raises_value_error():
    base = DialectRegistry.default()
    with pytest.raises(ValueError, match="already registered"):
        base.withEntry(DataSourceType.ORACLE, "default", _mk("dup"))


def test_with_entry_accepts_string_dbtype():
    base = DialectRegistry.default()
    # 字符串 "oracle" 应被归一化为 DataSourceType.ORACLE；用唯一 bucket 名避免重复
    new = base.withEntry("oracle", "test_string_dbtype", _mk("X"))
    assert (DataSourceType.ORACLE, "test_string_dbtype") in new._entries
    assert new._entries[(DataSourceType.ORACLE, "test_string_dbtype")].name == "X"


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
    # 未知 dbType 在 enum coercion 阶段就抛 ValueError
    with pytest.raises(ValueError):
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
