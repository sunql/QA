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


def test_measure_columns_strips_table_aliases() -> None:
    """`O.UNIT_PRICE` 的限定名不是列：measure 列集合只含物理列。

    与 ``extractSqlIdentifiers``（刻意宽松、保留别名）不同 —— measure 走
    governance ⇄ technique 的**集合相等**比较，别名残留会让对账恒 mismatch。
    """
    assert measureColumnIdentifiers("O.UNIT_PRICE - P.UNIT_PRICE") == {"unit_price"}


def test_measure_columns_drops_function_names() -> None:
    """`TRUNC`/`NVL` 不是列（`trunc`/`nvl` 不在 SQL 停用词表里，必须单独剔除）。"""
    assert measureColumnIdentifiers("TRUNC(R.RECEIPT_DATE) - NVL(QTY, 0)") == {
        "receipt_date",
        "qty",
    }


def test_extract_stays_loose_for_sql_text() -> None:
    """反向守卫：`extractSqlIdentifiers` 行为不变（SQL 侧只做「声明 ⊆ 实际」）。"""
    assert extractSqlIdentifiers("O.UNIT_PRICE") == {"o", "unit_price"}


def test_measure_columns_on_none_returns_empty() -> None:
    assert measureColumnIdentifiers(None) == set()
