"""B5 Memory Phase A：字段继承纯函数单元测试。

覆盖：
- _parsePlanTime：从 plan dict conditions 解析时间表达式
- _shiftTimeHint：省略式追问时间递减（去年/上季度/上个月）
- _mergeFilters：过滤叠加（不覆盖同 key，不同 key 堆叠）
- InheritedState dataclass 不可变性
- 解析失败 best-effort 降级（不抛错）
"""
from __future__ import annotations

import pytest

from app.services.chat_context import (
    _parsePlanTime,
    _shiftTimeHint,
    _mergeFilters,
    InheritedState,
    TimeHint,
)


class TestParsePlanTime:
    """时间条件解析：conditions 中的时间表达式提取。"""

    def test_year_condition_parses(self) -> None:
        """2025 年条件 → {year: 2025}。"""
        plan = {
            "conditions": [
                "年份 = 2025",
                "供应商 = 'A'",
            ],
            "aggregations": [],
            "selectedProperties": ["sales_amount"],
        }
        result = _parsePlanTime(plan)
        assert result is not None
        assert result.year == 2025

    def test_year_greater_than_parses(self) -> None:
        """>= 2025 年条件 → {year: 2025}。"""
        plan = {
            "conditions": ["year >= 2025"],
            "aggregations": [],
            "selectedProperties": [],
        }
        result = _parsePlanTime(plan)
        assert result is not None
        assert result.year == 2025

    def test_period_condition_parses(self) -> None:
        """period = '2025-04' → {year: 2025, month: 4}。"""
        plan = {
            "conditions": ["period = '2025-04'"],
            "aggregations": [],
            "selectedProperties": [],
        }
        result = _parsePlanTime(plan)
        assert result is not None
        assert result.year == 2025
        assert result.month == 4

    def test_quarter_condition_parses(self) -> None:
        """季度条件 → {year: 2025, quarter: 2}。"""
        plan = {
            "conditions": ["季度 = 2", "year = 2025"],
            "aggregations": [],
            "selectedProperties": [],
        }
        result = _parsePlanTime(plan)
        assert result is not None
        assert result.year == 2025
        assert result.quarter == 2

    def test_no_time_condition_returns_none(self) -> None:
        """无时间条件 → None。"""
        plan = {
            "conditions": ["供应商 = 'A'"],
            "aggregations": [],
            "selectedProperties": [],
        }
        result = _parsePlanTime(plan)
        assert result is None

    def test_malformed_conditions_returns_none(self) -> None:
        """畸形 conditions → None（best-effort，不抛错）。"""
        result = _parsePlanTime({})
        assert result is None

        result2 = _parsePlanTime({"conditions": None})
        assert result2 is None

        result3 = _parsePlanTime({"conditions": [None, 123]})
        assert result3 is None


class TestShiftTimeHint:
    """时间递减：「去年呢」「上季度呢」类省略式追问。"""

    def test_last_year_question(self) -> None:
        """「去年呢」→ year 2025→2024。"""
        hint = TimeHint(year=2025)
        shifted = _shiftTimeHint(hint, "去年呢")
        assert shifted is not None
        assert shifted.year == 2024
        assert shifted.shifted_from == 2025

    def test_last_quarter_question(self) -> None:
        """「上季度呢」→ quarter 2→1。"""
        hint = TimeHint(year=2025, quarter=2)
        shifted = _shiftTimeHint(hint, "上季度呢")
        assert shifted is not None
        assert shifted.quarter == 1
        assert shifted.shifted_from == 2025

    def test_last_month_question(self) -> None:
        """「上个月呢」→ month 4→3。"""
        hint = TimeHint(year=2025, month=4)
        shifted = _shiftTimeHint(hint, "上个月呢")
        assert shifted is not None
        assert shifted.month == 3

    def test_non_time_question_returns_original(self) -> None:
        """无关追问 → 原样返回（不递减）。"""
        hint = TimeHint(year=2025)
        shifted = _shiftTimeHint(hint, "华东地区呢")
        assert shifted is not None
        assert shifted.year == 2025
        assert shifted.shifted_from is None

    def test_no_hint_returns_none(self) -> None:
        """hint 为 None → None（不抛错）。"""
        result = _shiftTimeHint(None, "去年呢")
        assert result is None


class TestMergeFilters:
    """过滤叠加：新过滤与上一轮过滤合并（同 key 不覆盖，不同 key 堆叠）。"""

    def test_new_key_appends(self) -> None:
        """region=华东叠加 prior{company=A} → 两 key 均存。"""
        new = {"region": "华东"}
        prior = {"company": "A"}
        merged = _mergeFilters(new, prior)
        assert merged == {"company": "A", "region": "华东"}

    def test_same_key_keeps_prior(self) -> None:
        """同 key（如 region）→ 保留上一轮值，不覆盖。"""
        new = {"region": "华南"}
        prior = {"region": "华东", "company": "A"}
        merged = _mergeFilters(new, prior)
        assert merged["region"] == "华东"
        assert merged["company"] == "A"

    def test_empty_prior(self) -> None:
        """prior=None → 只含新过滤。"""
        new = {"region": "华东"}
        merged = _mergeFilters(new, None)
        assert merged == {"region": "华东"}

    def test_empty_new(self) -> None:
        """new=None/{} → 返回 prior 副本。"""
        prior = {"company": "A"}
        assert _mergeFilters(None, prior) == {"company": "A"}
        assert _mergeFilters({}, prior) == {"company": "A"}

    def test_both_empty(self) -> None:
        """无新无旧 → 空 dict。"""
        assert _mergeFilters(None, None) == {}
        assert _mergeFilters({}, {}) == {}


class TestInheritedStateImmutable:
    """InheritedState dataclass 不可变性。"""

    def test_frozen_dataclass(self) -> None:
        """frozen=True → 构造后不可修改属性。"""
        from app.services.chat_context import InheritedState

        state = InheritedState(
            inherited_metric="SA",
            inherited_time=None,
            inherited_filters={"region": "华东"},
            inheritance_confidence=0.85,
        )
        with pytest.raises(AttributeError):
            state.inherited_metric = "SB"  # type: ignore[misc]

    def test_fields_optional(self) -> None:
        """所有字段有默认值，可全空构造。"""
        from app.services.chat_context import InheritedState

        state = InheritedState()
        assert state.inherited_metric is None
        assert state.inherited_time is None
        assert state.inherited_filters is None
        assert state.inheritance_confidence == 0.0
