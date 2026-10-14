"""data_summary distinct 计数对 unhashable 列容错。

RED: 现 {row.get(col) for row in data} 遇 dict/list 列抛 unhashable。
"""
from __future__ import annotations

import pytest

from app.services.data_summary import summarize_data


def test_distinct_set_column_does_not_raise():
    """列值是 set 时，distinct 不抛错；summary 标记 skip_distinct。"""
    rows = [
        {"id": 1, "items": {"a", "b"}},
        {"id": 2, "items": {"c"}},
        {"id": 3, "items": {"a", "b"}},
    ]
    summary = summarize_data(
        data=rows,
        head_size=10,
        tail_size=10,
    )
    # set is non-scalar → marked as skip_distinct
    assert "items" in summary.get("skip_distinct_columns", [])
    # id is NUMBER type → not in distinct_counts
    assert "id" not in summary.get("distinct_counts", {})


def test_distinct_list_column_does_not_raise():
    rows = [
        {"id": 1, "tags": ["a", "b"]},
        {"id": 2, "tags": ["c"]},
        {"id": 3, "tags": ["a", "b"]},
    ]
    summary = summarize_data(
        data=rows,
        head_size=10,
        tail_size=10,
    )
    # list is non-scalar → marked as skip_distinct
    assert "tags" in summary.get("skip_distinct_columns", [])
    # distinct count via JSON: ["a","b"] == ["a","b"], ["c"] distinct → 2
    assert summary["distinct_counts"]["tags"] == 2


def test_distinct_scalar_column_unchanged():
    rows = [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
        {"id": 3, "name": "a"},
    ]
    summary = summarize_data(
        data=rows,
        head_size=10,
        tail_size=10,
    )
    assert summary["distinct_counts"]["name"] == 2
    assert "name" not in summary.get("skip_distinct_columns", [])
