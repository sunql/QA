"""B4：分母契约弱校验（KPI 治理 denominator 声明 vs 生成 SQL 分母表达式）。

纯函数 `denominatorContractWarning(sql, denominatorDeclared)`，家族哲学同
Top-N 分母守卫：宁漏不误报（弱校验、示警不硬拒——治理声明可能有历史误差，
先积累数据）。

判据：治理声明抽得出列名（英文列口径）时，SQL 内全部除法表达式右操作数的
列集合须**包含**声明的列集合（声明 ⊆ SQL 分母列，允许 SQL 分母更复杂如
NULLIF 包裹、多列相加）。判不了 → 一律 None（不谎报一致也不谎报不一致，
同 cost_source/unknown 口径）：
- 声明为中文业务文本（抽不出列名）→ None；
- SQL 无除法表达式 → None（B4 只管「生成了比率但分母错列」，不管「没生成比率」）。
"""

from __future__ import annotations

from app.services.nl2sql_semantic_guard import denominatorContractWarning


def test_compatible_denominator_passes() -> None:
    """SQL 分母包含声明列 → None。"""
    sql = "SELECT supplier, SUM(received_qty)/SUM(order_qty) AS rate FROM t GROUP BY supplier"
    assert denominatorContractWarning(sql, "order_qty") is None


def test_nullif_wrapped_denominator_passes() -> None:
    """NULLIF 包裹（除零保护）仍算兼容——声明列在分母表达式列集合内即可。"""
    sql = "SELECT SUM(a)/NULLIF(SUM(order_qty), 0) AS rate FROM t"
    assert denominatorContractWarning(sql, "order_qty") is None


def test_window_denominator_passes() -> None:
    """窗口占比形态（同块窗口分母）下声明列在分母内 → None。"""
    sql = "SELECT SUM(a)/SUM(order_qty) OVER (PARTITION BY supplier) AS rate FROM t"
    assert denominatorContractWarning(sql, "order_qty") is None


def test_subquery_denominator_found() -> None:
    """分母在子查询里也能找到（整语句扫描）。"""
    sql = "SELECT x, y FROM (SELECT supplier AS x, SUM(a)/SUM(order_qty) AS y FROM t GROUP BY supplier) s"
    assert denominatorContractWarning(sql, "order_qty") is None


def test_wrong_denominator_column_warns() -> None:
    """分母用了声明之外的列 → 示警，缺失列出现在提示中。"""
    sql = "SELECT SUM(received_qty)/SUM(late_qty) AS rate FROM t"
    warning = denominatorContractWarning(sql, "order_qty")
    assert warning is not None
    assert "order_qty" in warning


def test_multiple_declared_partial_missing_warns() -> None:
    """多列声明部分缺失 → 示警只列缺失列；命中的列不进缺失清单。"""
    sql = "SELECT SUM(a)/SUM(order_qty) AS rate FROM t"
    warning = denominatorContractWarning(sql, "order_qty + return_qty")
    assert warning is not None
    assert "return_qty" in warning


def test_declared_column_in_literal_only_still_warns() -> None:
    """声明列只出现在字符串字面量/注释里 ≠ 在分母表达式 → 示警（剥离后重扫）。"""
    sql = "SELECT SUM(a)/SUM(late_qty) AS rate FROM t -- order_qty check\nWHERE note = 'order_qty'"
    assert denominatorContractWarning(sql, "order_qty") is not None


def test_chinese_declaration_cannot_judge() -> None:
    """中文声明抽不出列名 → 判不了 → None（不谎报不一致）。"""
    sql = "SELECT SUM(a)/SUM(late_qty) AS rate FROM t"
    assert denominatorContractWarning(sql, "总到货行数") is None


def test_empty_denominator_declaration_skips() -> None:
    """声明为空（B5 DRAFT 户头形态）→ None。"""
    sql = "SELECT SUM(a)/SUM(b) AS rate FROM t"
    assert denominatorContractWarning(sql, None) is None
    assert denominatorContractWarning(sql, "   ") is None


def test_sql_without_division_skips() -> None:
    """SQL 无除法表达式 → None（B4 只管分母列错配，不管漏生成比率）。"""
    assert denominatorContractWarning("SELECT SUM(order_qty) FROM t", "order_qty") is None


def test_none_sql_skips() -> None:
    assert denominatorContractWarning(None, "order_qty") is None


def test_numerator_side_column_does_not_count() -> None:
    """声明列只在分子侧出现、分母侧没有 → 仍示警（不整语句凑数）。"""
    sql = "SELECT SUM(order_qty)/SUM(late_qty) AS rate FROM t"
    assert denominatorContractWarning(sql, "order_qty") is not None
