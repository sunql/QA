"""REFINE 捷径的方言感知改写测试（P0-1）。

回归：sqlglot 未指定方言时用默认方言解析/输出，Oracle 的
``FETCH FIRST n ROWS ONLY`` 会被改写成 ``LIMIT n``（Oracle 语法错误），
``ROWNUM`` 行数限制也会被追加 ``LIMIT``。改写必须按数据源方言解析与输出，
且无法安全改写时返回 None 退回 LLM 两阶段。
"""

from __future__ import annotations

import pytest

from app.domain.enums import DataSourceType
from app.domain.query_plan import QueryPlan
from app.services.nl2sql_refine import rewriteOrderBy, rewriteTopLimit
from app.services.nl2sql_service import Nl2SqlService


def _plan(*props: str) -> QueryPlan:
    return QueryPlan(target="t", selectedProperties=tuple(props))


def _direct(
    sql: str,
    plan: QueryPlan | None,
    question: str,
    datasourceType: DataSourceType | str | None = None,
) -> str | None:
    return Nl2SqlService().applyRefineDirect(
        sql, plan, question, datasourceType=datasourceType
    )


class TestLimitDialect:
    def test_oracle_fetch_first_stays_fetch_first(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T ORDER BY NAME ASC FETCH FIRST 10 ROWS ONLY"
        out = rewriteTopLimit(sql, 3, dialect="oracle")
        assert out is not None
        assert "FETCH FIRST 3 ROWS ONLY" in out
        assert "LIMIT" not in out

    def test_oracle_rownum_is_not_given_a_limit(self) -> None:
        # ROWNUM 不是 AST 的 limit 节点；追加 LIMIT/FETCH 会改变语义或语法非法。
        sql = "SELECT NAME FROM ZJTH.T WHERE ROWNUM <= 10"
        assert rewriteTopLimit(sql, 3, dialect="oracle") is None

    def test_mysql_limit_rewrite(self) -> None:
        out = rewriteTopLimit("SELECT NAME FROM ZJTH.T LIMIT 10", 5, dialect="mysql")
        assert out == "SELECT NAME FROM ZJTH.T LIMIT 5"

    def test_direct_wrapper_propagates_oracle_enum(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T ORDER BY NAME ASC FETCH FIRST 10 ROWS ONLY"
        out = _direct(sql, _plan("NAME"), "只要前 3 条", DataSourceType.ORACLE)
        assert out is not None
        assert "FETCH FIRST 3 ROWS ONLY" in out
        assert "LIMIT" not in out

    def test_direct_wrapper_accepts_raw_type_string(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T FETCH FIRST 10 ROWS ONLY"
        out = _direct(sql, _plan(), "只看前 5 名", "oracle")
        assert out is not None
        assert "FETCH FIRST 5 ROWS ONLY" in out
        assert "LIMIT" not in out

    def test_direct_wrapper_unknown_type_keeps_default_behavior(self) -> None:
        # 方言未知时退回默认方言：FETCH FIRST 仍会被改写成 LIMIT。
        # 边界由上游 coerceDatasourceType fail-fast 保证，这里只是最佳努力。
        sql = "SELECT NAME FROM ZJTH.T FETCH FIRST 10 ROWS ONLY"
        out = _direct(sql, _plan(), "只看前 5 名", "unknown-db")
        assert out is not None
        assert "LIMIT 5" in out


class TestOrderByDialect:
    def test_oracle_order_by_preserves_fetch_first(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T FETCH FIRST 10 ROWS ONLY"
        out = _direct(sql, _plan("NAME"), "按 NAME 降序", DataSourceType.ORACLE)
        assert out is not None
        assert "ORDER BY NAME DESC FETCH FIRST 10 ROWS ONLY" in out
        assert "LIMIT" not in out

    def test_oracle_desc_does_not_add_nulls_clause(self) -> None:
        # Oracle 的 DESC 默认 NULLS FIRST；显式输出 NULLS LAST 会改变排序语义。
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME"), "按 NAME 降序", DataSourceType.ORACLE)
        assert out is not None
        assert out.endswith("ORDER BY NAME DESC")
        assert "NULLS" not in out

    def test_postgres_desc_does_not_add_nulls_clause(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME"), "按 NAME 降序", DataSourceType.POSTGRESQL)
        assert out is not None
        assert out.endswith("ORDER BY NAME DESC")
        assert "NULLS" not in out

    def test_mysql_order_by_keeps_mysql_syntax(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T LIMIT 10"
        out = _direct(sql, _plan("NAME"), "按 NAME 升序", DataSourceType.MYSQL)
        assert out is not None
        assert out.endswith("ORDER BY NAME ASC LIMIT 10")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (DataSourceType.MYSQL, "mysql"),
        (DataSourceType.POSTGRESQL, "postgres"),
        (DataSourceType.ORACLE, "oracle"),
        ("MYSQL", "mysql"),
        ("oracle", "oracle"),
        (None, None),
        ("sqlserver", None),
    ],
)
def test_sqlglot_dialect_mapping(raw, expected) -> None:
    from app.services.nl2sql_refine import _sqlglotDialect

    assert _sqlglotDialect(raw) == expected
