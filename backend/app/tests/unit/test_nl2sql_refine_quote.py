"""nl2sql_refine 改写的 quote-aware 与 AST 化测试。

RED: 现 _rewriteLimit 用 re.sub() 误改字符串字面量内的 LIMIT；
    _rewriteOrderBy 嵌套 SQL 时改到内层 ORDER BY（不是"只改顶层"）。
"""
from __future__ import annotations

import pytest

from app.services.nl2sql_refine import rewriteTopLimit, rewriteOrderBy


def test_limit_does_not_touch_string_literal():
    """字符串字面量内的 LIMIT 不被改写。

    'SELECT id FROM orders WHERE note = ''LIMIT 10'' LIMIT 20' + n=3
    → 字符串内 LIMIT 10 不动；末尾 LIMIT 20 → LIMIT 3。
    """
    sql = "SELECT id FROM orders WHERE note = 'LIMIT 10' LIMIT 20"
    result = rewriteTopLimit(sql, 3)
    assert result is not None
    assert "'LIMIT 10'" in result  # 字符串内 LIMIT 不变
    assert result.rstrip().endswith("LIMIT 3")


def test_limit_on_nested_sql_falls_back():
    """嵌套 SQL（带子查询）→ rewriteTopLimit 返 None（让两阶段生成接手）。"""
    sql = "SELECT id FROM (SELECT id FROM orders LIMIT 5) sub LIMIT 20"
    result = rewriteTopLimit(sql, 3)
    assert result is None  # 自动回退


def test_limit_on_cte_falls_back():
    sql = "WITH t AS (SELECT id FROM x LIMIT 5) SELECT id FROM t LIMIT 20"
    result = rewriteTopLimit(sql, 3)
    assert result is None


def test_limit_simple_top_select_succeeds():
    sql = "SELECT id, name FROM orders WHERE status = 'active' LIMIT 20"
    result = rewriteTopLimit(sql, 3)
    assert result is not None
    assert result.rstrip().endswith("LIMIT 3")


def test_order_by_inner_only_rewrites_outer():
    """内层有 ORDER BY 时，"按 id 降序" 在外层加 ORDER BY（不动内层）。"""
    sql = "SELECT id FROM (SELECT id FROM x ORDER BY created_at DESC) sub LIMIT 5"
    result = rewriteOrderBy(sql, "id", "DESC")
    assert result is not None
    # 内层 ORDER BY 不动
    assert "ORDER BY created_at DESC" in result
    # 新增的外层 ORDER BY
    assert result.count("ORDER BY") == 2
    # 外层 ORDER BY 在末尾
    outer_idx = result.rfind("ORDER BY")
    assert "ID" in result[outer_idx:].upper().split("BY", 1)[1]


def test_order_by_simple_top_select_adds():
    """简单 SELECT 无 ORDER BY → 末尾追加 ORDER BY col dir。"""
    sql = "SELECT id FROM orders LIMIT 20"
    result = rewriteOrderBy(sql, "id", "DESC")
    assert result is not None
    assert "ORDER BY id DESC" in result
    assert result.count("ORDER BY") == 1


def test_order_by_invalid_sql_falls_back():
    """解析失败 → 返 None（不抛错）。"""
    result = rewriteOrderBy("INVALID SQL @#$", "id", "DESC")
    assert result is None
