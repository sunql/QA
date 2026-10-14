"""SqlDialect.check_sql 实例方法单测（不进数据库，纯 Python）。

2026-10-08 dialect-registry refactor：bannedFunctions / keywordPatterns /
intervalAllowed 字段从模块级 dict 搬到 SqlDialect 实例；check_sql 成为实例方法。
"""
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
    d = _mk(
        keyword_patterns=(
            (r"\bINTERVAL\s+(\d|['\"])", "INTERVAL", "用 ADD_MONTHS 替代"),
        )
    )
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
