"""数据质量评估器单测（5 维 evaluator + 共享 validators）。

不依赖真实 DB —— 适配器通过 SimpleNamespace stub 出 execute_read_only；校验
逻辑（identifier / expression 白名单 + 禁用关键字）直接在 _common.py 单测覆盖。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.enums import RuleType, Severity
from app.domain.exceptions import ValidationError
from app.domain.models import DataQualityRule
from app.infrastructure.business_db_pool import _OracleAdapter, _SqlaAdapter
from app.services.data_quality_evaluators._common import (
    quote_identifier,
    quote_qualified_name,
    validate_expression,
    validate_identifier,
)
from app.services.data_quality_evaluators.completeness import evaluate as compEval
from app.services.data_quality_evaluators.consistency import evaluate as consEval
from app.services.data_quality_evaluators.referential import (
    evaluate as refEval,
    parse_ref_expression,
)
from app.services.data_quality_evaluators.uniqueness import evaluate as uniqEval
from app.services.data_quality_evaluators.validity import evaluate as valEval


class _StubAdapter:
    """单次响应式适配器，按预设返回行（无状态时最后一次值生效）。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
        self.calls.append(sql)
        return self.rows


class _FakeOracleAdapter(_OracleAdapter):
    """绕开 __init__ 的轻量 stub：直接构造 isinstance 判定需要的字段。"""

    def __init__(self) -> None:  # noqa: D401 - test stub
        # 不调父类 __init__（避免连真 DSN / 用户名），仅保留 isinstance 链
        self.dialect = "oracle"


class _FakeMysqlAdapter(_SqlaAdapter):
    """SQLAlchemy MySQL adapter stub：保留 isinstance 链 + 暴露 dialect='mysql'。

    不调父类 __init__（避免创建真 async engine），并 override execute_read_only
    用预设 rows 响应——否则 _SqlaAdapter._ensureEngine() 会在没有 _engine 时抛
    AttributeError，evaluator 跑不下去。
    """

    def __init__(self) -> None:  # noqa: D401 - test stub
        # 不调父类 __init__（避免创建真 async engine）
        self._url = "mysql+aiomysql://u:p@h:3306/db"
        self.dialect = "mysql"
        self.rows: list[dict[str, Any]] = []
        self.calls: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
        self.calls.append(sql)
        return self.rows


class _FakePostgresAdapter(_SqlaAdapter):
    """SQLAlchemy PG adapter stub（同 MySQL stub 模式：跳过 engine 初始化）。"""

    def __init__(self) -> None:  # noqa: D401 - test stub
        self._url = "postgresql+asyncpg://u:p@h:5432/db"
        self.dialect = "postgresql"
        self.rows: list[dict[str, Any]] = []
        self.calls: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
        self.calls.append(sql)
        return self.rows


def _run(coro):
    """Run a coroutine in a fresh event loop (Python 3.14 取消了隐式 loop 创建)。"""
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_rule(
    *,
    rule_type: RuleType,
    target_table: str = "PORDER",
    target_column: str | None = "ORDER_QTY",
    rule_expression: str | None = None,
    threshold: str = "95.00",
) -> DataQualityRule:
    """构造一条不落库的 DataQualityRule。"""
    return DataQualityRule(
        id=1,
        rule_name="t",
        rule_code="T",
        target_table=target_table,
        target_column=target_column,
        rule_type=rule_type,
        rule_expression=rule_expression,
        threshold=threshold,
        severity=Severity.MEDIUM,
        is_enabled=True,
        version="v1.0",
    )


# ===== _common validators =====


class TestValidateIdentifier:
    def test_accepts_simple_identifier(self) -> None:
        assert validate_identifier("PORDER", role="table") == "PORDER"
        assert validate_identifier("_priv", role="table") == "_priv"
        assert validate_identifier("col_1", role="column") == "col_1"

    @pytest.mark.parametrize("bad", ["1col", "DROP TABLE", "t;DROP", "a b", "a-b", ""])
    def test_rejects_invalid(self, bad) -> None:
        with pytest.raises(ValidationError):
            validate_identifier(bad, role="table")


class TestValidateExpression:
    def test_accepts_simple_predicate(self) -> None:
        assert validate_expression("ORDER_QTY > 0") == "ORDER_QTY > 0"
        assert validate_expression("A + B * 1.05") == "A + B * 1.05"

    def test_accepts_pg_quoted_identifier(self) -> None:
        # PostgreSQL 大小写敏感标识符（如 "ORDERQTY"）必须双引号
        assert validate_expression('"ORDERQTY" > 0') == '"ORDERQTY" > 0'
        assert validate_expression(
            '"PONUM" IN (SELECT "PONUM" FROM "PORDERQ")'
        ) == '"PONUM" IN (SELECT "PONUM" FROM "PORDERQ")'

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            validate_expression("   ")

    @pytest.mark.parametrize(
        "bad",
        ["a; DROP TABLE t", "a UNION SELECT *", "--", "/*", "a`b`"],
    )
    def test_rejects_forbidden_keyword(self, bad) -> None:
        with pytest.raises(ValidationError):
            validate_expression(bad)

    def test_rejects_special_chars(self) -> None:
        # 含分号、管道符
        with pytest.raises(ValidationError, match="非法字符"):
            validate_expression("a; b")
        with pytest.raises(ValidationError, match="非法字符"):
            validate_expression("a|b")

    def test_accepts_literal_and_regex_chars(self) -> None:
        # 单引号（字符串字面量）自 feat-dq-rule-auto-generation 起放行
        # （值域 IN 列表 / POSIX 正则需要），注入面由黑名单 + 只读 adapter 兜底
        assert validate_expression("a = 'x'") == "a = 'x'"


# ===== quote_identifier（feat-dialect-quoting）=====
#
# 衍生 bug：data_quality_evaluators 早期硬编码 f'FROM "{table}"'，对 Oracle/PG 合法，
# 对 MySQL 触发 pymysql 1064（"mdmtoerp" 被当字符串字面量）。修法：按 adapter dialect
# 决定引号——Oracle/PG 双引号、MySQL 反引号。参见 [[silent-failure-hunter]] 复盘。


class TestQuoteIdentifier:
    def test_oracle_uses_double_quotes(self) -> None:
        assert quote_identifier(_FakeOracleAdapter(), "PORDER") == '"PORDER"'

    def test_mysql_uses_backticks(self) -> None:
        assert quote_identifier(_FakeMysqlAdapter(), "PORDER") == "`PORDER`"
        assert quote_identifier(_FakeMysqlAdapter(), "ORDER_QTY") == "`ORDER_QTY`"

    def test_postgres_uses_double_quotes(self) -> None:
        assert (
            quote_identifier(_FakePostgresAdapter(), "PORDER") == '"PORDER"'
        )

    def test_unknown_adapter_falls_back_to_pg_style(self) -> None:
        # 测试 stub（如 _StubAdapter）非 Oracle/PG/MySQL → 默认双引号（不破坏旧单测）
        assert quote_identifier(_StubAdapter(), "PORDER") == '"PORDER"'

    def test_qualified_name_uses_matching_quotes(self) -> None:
        # MySQL：每段都用反引号
        assert (
            quote_qualified_name(_FakeMysqlAdapter(), "t1", "c1")
            == "`t1`.`c1`"
        )
        # Oracle/PG：每段都用双引号
        assert (
            quote_qualified_name(_FakeOracleAdapter(), "t1", "c1")
            == '"t1"."c1"'
        )


# ===== COMPLETENESS =====


class TestCompleteness:
    def test_returns_total_and_passed(self) -> None:
        rule = _make_rule(rule_type=RuleType.COMPLETENESS, target_column="ORDER_QTY")
        adapter = _StubAdapter([{"total": 10, "passed": 7}])
        total, passed = _run(compEval(rule, adapter))
        assert (total, passed) == (10, 7)
        # SQL 应使用 COUNT(col) 且包含正确表名
        sql = adapter.calls[0]
        assert 'FROM "PORDER"' in sql
        assert 'COUNT("ORDER_QTY")' in sql

    def test_mysql_adapter_uses_backticks(self) -> None:
        """回归：MySQL adapter 必须输出反引号，否则 pymysql 报 1064。
        复现 [[silent-failure-hunter]] 里 13 条 COMPLETENESS 规则全 ERROR 的根因。"""
        rule = _make_rule(rule_type=RuleType.COMPLETENESS, target_column="ORDER_QTY")
        adapter = _FakeMysqlAdapter()
        adapter.rows = [{"total": 5, "passed": 3}]
        adapter.calls = []
        _run(compEval(rule, adapter))
        sql = adapter.calls[0]
        assert "FROM `PORDER`" in sql, f"MySQL 应该用反引号，got: {sql}"
        assert "COUNT(`ORDER_QTY`)" in sql
        # 反向断言：不应再用双引号
        assert '"PORDER"' not in sql

    def test_target_column_required(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.COMPLETENESS, target_column=None
        )
        with pytest.raises(Exception, match="target_column"):
            _run(compEval(rule, _StubAdapter()))


# ===== VALIDITY =====


class TestValidity:
    def test_returns_total_and_passed(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.VALIDITY,
            target_column="ORDER_QTY",
            rule_expression="ORDER_QTY > 0",
        )
        adapter = _StubAdapter([{"total": 100, "passed": 95}])
        total, passed = _run(valEval(rule, adapter))
        assert (total, passed) == (100, 95)
        sql = adapter.calls[0]
        assert "FROM \"PORDER\"" in sql
        assert "CASE WHEN (ORDER_QTY > 0)" in sql

    def test_expression_required(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.VALIDITY, target_column="ORDER_QTY", rule_expression=None
        )
        with pytest.raises(Exception, match="rule_expression"):
            _run(valEval(rule, _StubAdapter()))


# ===== UNIQUENESS =====


class TestUniqueness:
    def test_returns_distinct_count(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.UNIQUENESS, target_column="ORDER_KEY"
        )
        adapter = _StubAdapter([{"total": 10, "passed": 9}])
        total, passed = _run(uniqEval(rule, adapter))
        assert (total, passed) == (10, 9)
        sql = adapter.calls[0]
        assert "COUNT(DISTINCT \"ORDER_KEY\")" in sql

    def test_target_column_required(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.UNIQUENESS, target_column=None
        )
        with pytest.raises(Exception, match="target_column"):
            _run(uniqEval(rule, _StubAdapter()))


# ===== CONSISTENCY =====


class TestConsistency:
    def test_returns_total_and_passed(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.CONSISTENCY,
            target_column=None,
            rule_expression="RECEIVED_QTY <= ORDER_QTY * 1.05",
        )
        adapter = _StubAdapter([{"total": 50, "passed": 49}])
        total, passed = _run(consEval(rule, adapter))
        assert (total, passed) == (50, 49)
        sql = adapter.calls[0]
        assert "RECEIVED_QTY <= ORDER_QTY * 1.05" in sql

    def test_expression_required(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.CONSISTENCY, target_column=None, rule_expression=None
        )
        with pytest.raises(Exception, match="rule_expression"):
            _run(consEval(rule, _StubAdapter()))


# ===== REFERENTIAL =====


class TestReferential:
    def test_parse_ref_expression_ok(self) -> None:
        assert parse_ref_expression("REF SUPPLIER.SUPPLIER_KEY") == (
            "SUPPLIER",
            "SUPPLIER_KEY",
        )
        # 允许前后空白
        assert parse_ref_expression("  ref  supplier.sid ") == (
            "supplier",
            "sid",
        )

    @pytest.mark.parametrize(
        "bad", ["", "REF SUPPLIER", "REFSUPPLIER.SID", "REF SUPPLIER.SID EXTRA", "DROP TABLE"]
    )
    def test_parse_ref_expression_invalid(self, bad) -> None:
        with pytest.raises(ValidationError):
            parse_ref_expression(bad)

    def test_evaluate_returns_matched_count(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.REFERENTIAL,
            target_column="SUPPLIER_KEY",
            rule_expression="REF SUPPLIER.SUPPLIER_KEY",
        )
        adapter = _StubAdapter([{"total": 100, "passed": 97}])
        total, passed = _run(refEval(rule, adapter))
        assert (total, passed) == (100, 97)
        sql = adapter.calls[0]
        assert "FROM \"SUPPLIER\"" in sql
        assert "EXISTS" in sql

    def test_evaluate_requires_target_column_and_expression(self) -> None:
        # 缺 target_column
        rule = _make_rule(
            rule_type=RuleType.REFERENTIAL,
            target_column=None,
            rule_expression="REF S.K",
        )
        with pytest.raises(Exception, match="target_column"):
            _run(refEval(rule, _StubAdapter()))
        # 缺 rule_expression
        rule2 = _make_rule(
            rule_type=RuleType.REFERENTIAL,
            target_column="SUPPLIER_KEY",
            rule_expression=None,
        )
        with pytest.raises(Exception, match="rule_expression"):
            _run(refEval(rule2, _StubAdapter()))


# ===== 空结果 =====


class TestEmptyResult:
    def test_completeness_empty_rows(self) -> None:
        rule = _make_rule(rule_type=RuleType.COMPLETENESS, target_column="X")
        adapter = _StubAdapter([])
        assert _run(compEval(rule, adapter)) == (0, 0)

    def test_validity_empty_rows(self) -> None:
        rule = _make_rule(
            rule_type=RuleType.VALIDITY, target_column="X", rule_expression="X > 0"
        )
        adapter = _StubAdapter([])
        assert _run(valEval(rule, adapter)) == (0, 0)