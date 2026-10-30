"""SQL 标识符词法抽取（B4 提取的纯函数 + measure 列抽取薄封装）。"""

from __future__ import annotations

from app.services.nl2sql_semantic_guard import (
    extractSqlIdentifiers,
    measureColumnIdentifiers,
)


def test_extract_returns_lowercase() -> None:
    assert extractSqlIdentifiers("ACTUAL_RECEIPT_DATE") == {"actual_receipt_date"}


def test_extract_drops_sql_keywords_and_functions() -> None:
    cols = extractSqlIdentifiers("SELECT SUM(QTY) AS TOTAL FROM T WHERE STATUS = 'OK'")
    assert "sum" not in cols
    assert "select" not in cols
    assert "from" not in cols
    assert "where" not in cols
    assert "qty" in cols
    assert "total" in cols


def test_extract_on_none_returns_empty() -> None:
    assert extractSqlIdentifiers(None) == set()


def test_extract_on_empty_returns_empty() -> None:
    assert extractSqlIdentifiers("") == set()
    assert extractSqlIdentifiers("   ") == set()


def test_extract_on_pure_chinese_returns_empty() -> None:
    """中文业务描述抽不出列名 —— 对账层据此判 unknown，不谎报任一方向。"""
    assert extractSqlIdentifiers("按期交付的订单数量") == set()


def test_extract_handles_multiple_columns() -> None:
    cols = extractSqlIdentifiers("ACTUAL_RECEIPT_DATE - PROMISED_DATE")
    assert cols == {"actual_receipt_date", "promised_date"}


def test_measure_columns_from_expr() -> None:
    assert measureColumnIdentifiers("QTY * 2") == {"qty"}


def test_measure_columns_ignores_filter() -> None:
    """filter 是谓词（可能引用别的类的列），不算进列集合 —— 否则误判「技术跑偏」。"""
    assert measureColumnIdentifiers("QTY") == {"qty"}


def test_measure_columns_on_none_returns_empty() -> None:
    assert measureColumnIdentifiers(None) == set()
