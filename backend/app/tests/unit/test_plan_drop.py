"""查询计划丢弃报告（M3）单元测试。

覆盖：
- from_dictWithReport 对每一类损坏输入的丢弃报告（按原因分类）
- **语义不变**：from_dictWithReport 的 plan 与 from_dict 完全一致（逐例参数化）
- 报告本身：不可变、单行格式化、同类条目聚合计数

背景：from_dict 有 11 处静默丢弃点（"绝不抛错" 是刻意契约），静默到无法诊断
「模型说了什么、被丢掉了什么」，故补一份**报告**而非改成抛错。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.domain import plan_drop
from app.domain.plan_drop import (
    DROP_FIELD_NOT_A_LIST,
    DROP_INTERPRETATION_NOT_STR,
    DROP_ITEM_NOT_STR,
    DROP_NESTED_INVALID,
    DROP_NESTED_NOT_A_DICT,
    DROP_NOT_A_DICT,
    DROP_POSITIVE_INT_INVALID,
    DROP_TARGET_NOT_STR,
    PlanDrop,
    formatPlanDrops,
)
from app.domain.query_plan import QueryPlan

# 各类损坏输入：既用于「plan 语义不变」，也用于逐点报告断言
_BROKEN_PAYLOADS: list[dict | list | str | None] = [
    None,
    "不是 dict",
    ["也不是"],
    {},
    {"target": 123},
    {"selectedClasses": "PRECEIPT"},
    {"selectedProperties": ["BPSNUM", 42, None]},
    {"conditions": {"a": 1}},
    {"aggregations": ["SUM(QTY)"]},
    {"aggregations": [{"function": "SUM"}]},  # 缺 property
    {"joins": [{"sourceClass": "A"}]},
    {"sortBy": ["TOTAL desc"]},
    {"groupBy": [1, "BPSNUM"]},
    {"perGroupLimit": "abc"},
    {"perGroupLimit": 0},
    {"perGroupLimit": True},
    {"interpretation": 123},
    {"rowLimit": {"bad": 1}, "target": "查询"},
]


def _dropsFor(payload: object) -> tuple[PlanDrop, ...]:
    _, drops = QueryPlan.from_dictWithReport(payload)  # type: ignore[arg-type]
    return drops


def _reasonsByField(drops: tuple[PlanDrop, ...]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for d in drops:
        out.setdefault(d.field, set()).add(d.reason)
    return out


@pytest.mark.parametrize("payload", _BROKEN_PAYLOADS)
def test_from_dict_with_report_keeps_from_dict_semantics(payload: object) -> None:
    """本批只加观测，**不得**改变解析语义：plan 必须与 from_dict 逐字节相同。"""
    plan, _ = QueryPlan.from_dictWithReport(payload)  # type: ignore[arg-type]
    assert plan == QueryPlan.from_dict(payload)  # type: ignore[arg-type]


def test_clean_payload_reports_no_drops() -> None:
    payload = {
        "target": "各供应商收货数量",
        "selectedClasses": ["PRECEIPT"],
        "selectedProperties": ["BPSNUM", "QTY"],
        "conditions": ["BPSNUM 不为空"],
        "aggregations": [{"function": "SUM", "property": "QTY", "alias": "TOTAL"}],
        "groupBy": ["BPSNUM"],
        "joins": [{"sourceClass": "A", "targetClass": "B", "columns": ["A1 = B1"]}],
        "sortBy": [{"property": "TOTAL", "direction": "desc"}],
        "rowLimit": 100,
        "partitionBy": ["BPSNUM"],
        "perGroupLimit": 3,
        "interpretation": "按供应商汇总",
    }
    plan, drops = QueryPlan.from_dictWithReport(payload)
    assert drops == ()
    assert plan.perGroupLimit == 3


def test_absent_optional_keys_are_not_drops() -> None:
    """缺键是正常形态（LLM 省略可选字段），不是丢弃。"""
    assert _dropsFor({"target": "查询"}) == ()


def test_non_dict_payload_reports_whole_payload_drop() -> None:
    drops = _dropsFor("不是 dict")
    assert [d.reason for d in drops] == [DROP_NOT_A_DICT]
    assert drops[0].field == "<payload>"
    assert drops[0].rawType == "str"


def test_target_wrong_type_reports_drop() -> None:
    assert DROP_TARGET_NOT_STR in _reasonsByField(_dropsFor({"target": 123}))["target"]


def test_field_wrong_container_type_reports_drop() -> None:
    assert DROP_FIELD_NOT_A_LIST in _reasonsByField(_dropsFor({"selectedClasses": "PRECEIPT"}))[
        "selectedClasses"
    ]


def test_non_string_items_are_aggregated_with_count() -> None:
    drops = _dropsFor({"selectedProperties": ["BPSNUM", 42, 7]})
    itemDrops = [d for d in drops if d.reason == DROP_ITEM_NOT_STR]
    assert len(itemDrops) == 1, "同类条目应聚合为一条，避免大批次刷屏"
    assert itemDrops[0].field == "selectedProperties"
    assert itemDrops[0].count == 2
    assert itemDrops[0].rawType == "int"


def test_mixed_item_types_aggregate_separately() -> None:
    """聚合键含 rawType：两种垃圾类型给两条记录，类型信息不丢。"""
    drops = _dropsFor({"groupBy": [1, "BPSNUM", None]})
    itemDrops = [d for d in drops if d.reason == DROP_ITEM_NOT_STR]
    assert {(d.rawType, d.count) for d in itemDrops} == {("int", 1), ("NoneType", 1)}


def test_nested_non_dict_entry_reports_drop() -> None:
    assert DROP_NESTED_NOT_A_DICT in _reasonsByField(_dropsFor({"aggregations": ["SUM(QTY)"]}))[
        "aggregations"
    ]


def test_nested_invalid_entry_reports_drop() -> None:
    """缺必填字段的嵌套条目：factory(**filtered) 抛 TypeError → 跳过并上报。"""
    assert DROP_NESTED_INVALID in _reasonsByField(_dropsFor({"aggregations": [{"function": "SUM"}]}))[
        "aggregations"
    ]


def test_invalid_per_group_limit_reported_but_absent_is_not() -> None:
    assert DROP_POSITIVE_INT_INVALID in _reasonsByField(_dropsFor({"perGroupLimit": "abc"}))[
        "perGroupLimit"
    ]
    assert _dropsFor({"perGroupLimit": None}) == ()


def test_interpretation_wrong_type_reported() -> None:
    assert DROP_INTERPRETATION_NOT_STR in _reasonsByField(_dropsFor({"interpretation": 123}))[
        "interpretation"
    ]


def test_join_equation_normalization_is_not_a_drop() -> None:
    """join.columns 的 'A = B' 拆分是**自愈归一**（M-2 既有行为），不是丢弃。"""
    _, drops = QueryPlan.from_dictWithReport({
        "target": "x",
        "joins": [{"sourceClass": "A", "targetClass": "B", "columns": ["A1 = B1"]}],
    })
    assert drops == ()


def test_report_is_immutable_tuple_of_frozen_drops() -> None:
    plan, drops = QueryPlan.from_dictWithReport({"target": 1})
    assert isinstance(plan, QueryPlan)
    assert isinstance(drops, tuple)
    with pytest.raises(FrozenInstanceError):
        drops[0].field = "改"  # type: ignore[misc]


def test_format_plan_drops_is_single_line_and_names_each_drop() -> None:
    _, drops = QueryPlan.from_dictWithReport({
        "target": 123,
        "selectedProperties": ["BPSNUM", 42, 7],
        "perGroupLimit": "abc",
    })
    text = formatPlanDrops(drops)
    assert "\n" not in text
    assert "target" in text
    assert "selectedProperties" in text
    assert "perGroupLimit" in text
    assert "x2" in text  # 聚合计数


def test_format_plan_drops_on_empty_is_empty_string() -> None:
    assert formatPlanDrops(()) == ""


def test_every_reason_constant_has_a_live_drop_path() -> None:
    """每个原因常量都必须真被某条损坏输入触发（防常量成为装饰品）。"""
    exported = {v for k, v in vars(plan_drop).items() if k.startswith("DROP_")}
    produced = {d.reason for payload in _BROKEN_PAYLOADS for d in _dropsFor(payload)}
    assert exported - produced == set(), f"未被任何损坏输入触发的常量: {exported - produced}"
