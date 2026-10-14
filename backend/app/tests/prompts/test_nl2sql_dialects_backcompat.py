"""Refactor 后行为不变性测试：钉死 oracle 11g/19c/9i/10g/12c 行为与 refactor 前一致。

2026-10-08 dialect-registry refactor：现有 `test_nl2sql_dialects.py` 已覆盖
11g→11g 和 19c→12c（refactor 前）。本文件覆盖 refactor 后新增能力（19c→19c、
21c→21c）+ refactor 前历史行为保留（9i/10g → 11g）。
"""
from app.domain.enums import DataSourceType
from app.services.nl2sql_dialects import DialectRegistry


def test_backcompat_oracle_11_2_returns_11g():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "11.2.0.1.0")
    assert "ROWNUM" in d.limitRule


def test_backcompat_oracle_11g_string_returns_11g():
    d = DialectRegistry.default().resolve(DataSourceType.ORACLE, "11g")
    assert "ROWNUM" in d.limitRule


def test_backcompat_oracle_9i_returns_11g():
    """历史行为：9i 开头 → 11g 方言（保留旧 if 分支）。"""
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
