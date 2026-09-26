"""查询计划丢弃报告（M3）单元测试。

覆盖：
- from_dictWithReport 对每一类损坏输入的丢弃报告（按原因分类）
- **语义不变**：plan 与 M3 之前的实现逐字段相同（黄金快照，见 _GOLDEN_SEMANTICS）
- 报告本身：不可变、单行格式化、同类条目聚合计数

背景：from_dict 有 11 处静默丢弃点（"绝不抛错" 是刻意契约），静默到无法诊断
「模型说了什么、被丢掉了什么」，故补一份**报告**而非改成抛错。

⚠️ 语义不变的守卫为什么不是「与 from_dict 对比」：from_dict 现在**就是**
`from_dictWithReport(data)[0]`（纯委托），拿它当基准等于自己跟自己比，恒真 —— 一条
永远通过的断言。故改为在 M3 之前**机械导出**的黄金快照（见下），基准取自旧实现而非
新实现，任何解析漂移都会被逐字段钉住。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, astuple

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


# 有效 / 混合载荷：黄金快照必须同时钉住「保留」的一侧，否则只验证了「坏字段被丢」
_VALID_PAYLOADS: list[dict] = [
    {
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
    },
    {"target": "查采购", "selectedClasses": ["DIM_SUPPLIER"], "selectedProperties": "BAD"},
    {"target": "  ", "selectedClasses": []},
    {"joins": [{"sourceClass": "A", "targetClass": "B", "columns": "A1 = B1"}]},
    {
        "aggregations": [{"function": "SUM", "property": "QTY"}],
        "groupBy": ["QTY"],
        "sortBy": [1, {"property": "QTY", "direction": "asc"}],
    },
    {
        "aggregations": [
            {"function": "SUM", "property": "QTY"},
            {"function": "AVG", "property": "PRICE", "alias": "P"},
        ],
        "rowLimit": 7,
    },
]

_GOLDEN_PAYLOADS: list[object] = [*_BROKEN_PAYLOADS, *_VALID_PAYLOADS]

# ── 黄金快照：由 M3 **之前**的实现（aefabd3 的 from_dict）机械导出 ────────────────
# 导出方式（可复现）：
#   git worktree add --detach /tmp/pre aefabd3
#   python -c 'import dataclasses as d, app.domain.query_plan as q;
#              print(repr(d.astuple(q.QueryPlan.from_dict(PAYLOAD))))'
# 基准取**旧实现**，不取新实现 ⇒ 不可能是恒真断言。
# 元组字段顺序 = QueryPlan 定义顺序：
#   target, selectedClasses, selectedProperties, conditions, aggregations, groupBy,
#   joins, sortBy, rowLimit, partitionBy, perGroupLimit, interpretation
_GOLDEN_SEMANTICS: list[tuple] = [
    ("", (), (), (), (), (), (), (), None, (), None, None),  # None
    ("", (), (), (), (), (), (), (), None, (), None, None),  # "不是 dict"
    ("", (), (), (), (), (), (), (), None, (), None, None),  # ["也不是"]
    ("", (), (), (), (), (), (), (), None, (), None, None),  # {}
    ("", (), (), (), (), (), (), (), None, (), None, None),  # target 非 str
    ("", (), (), (), (), (), (), (), None, (), None, None),  # selectedClasses 非列表
    ("", (), ("BPSNUM",), (), (), (), (), (), None, (), None, None),  # 混入 42/None → 只留 str
    ("", (), (), (), (), (), (), (), None, (), None, None),  # conditions 非列表
    ("", (), (), (), (), (), (), (), None, (), None, None),  # aggregations 条目非 dict
    ("", (), (), (), (), (), (), (), None, (), None, None),  # aggregations 缺必填
    ("", (), (), (), (), (), (), (), None, (), None, None),  # joins 缺必填
    ("", (), (), (), (), (), (), (), None, (), None, None),  # sortBy 条目非 dict
    ("", (), (), (), (), ("BPSNUM",), (), (), None, (), None, None),  # groupBy 混入 1 → 只留 str
    ("", (), (), (), (), (), (), (), None, (), None, None),  # perGroupLimit "abc"
    ("", (), (), (), (), (), (), (), None, (), None, None),  # perGroupLimit 0（非正）
    ("", (), (), (), (), (), (), (), None, (), None, None),  # perGroupLimit True（bool 拒收）
    ("", (), (), (), (), (), (), (), None, (), None, None),  # interpretation 非 str
    ("查询", (), (), (), (), (), (), (), {"bad": 1}, (), None, None),  # rowLimit 不校验，原样透传
    (
        "各供应商收货数量", ("PRECEIPT",), ("BPSNUM", "QTY"), ("BPSNUM 不为空",),
        (("SUM", "QTY", "TOTAL", None),), ("BPSNUM",), (("A", "B", ("A1", "B1")),),
        (("TOTAL", "desc"),), 100, ("BPSNUM",), 3, "按供应商汇总",
    ),  # 全部有效字段
    ("查采购", ("DIM_SUPPLIER",), (), (), (), (), (), (), None, (), None, None),  # 一半有效一半坏
    ("  ", (), (), (), (), (), (), (), None, (), None, None),  # 纯空白 target 原样保留
    (
        "", (), (), (), (), (), (("A", "B", ("A", "1", " ", "=", " ", "B", "1")),),
        (), None, (), None, None,
    ),  # join.columns 传 str → 按字符拆（既有归一行为，非丢弃）
    (
        "", (), (), (), (("SUM", "QTY", None, None),), ("QTY",), (),
        (("QTY", "asc"),), None, (), None, None,
    ),  # 有效聚合 + 无效 sortBy 条目被过滤
    (
        "", (), (), (), (("SUM", "QTY", None, None), ("AVG", "PRICE", "P", None)),
        (), (), (), 7, (), None, None,
    ),  # 两个聚合条目保序
]


def _dropsFor(payload: object) -> tuple[PlanDrop, ...]:
    _, drops = QueryPlan.from_dictWithReport(payload)  # type: ignore[arg-type]
    return drops


def _reasonsByField(drops: tuple[PlanDrop, ...]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for d in drops:
        out.setdefault(d.field, set()).add(d.reason)
    return out


@pytest.mark.parametrize(
    ("payload", "golden"),
    zip(_GOLDEN_PAYLOADS, _GOLDEN_SEMANTICS, strict=True),
)
def test_from_dict_with_report_matches_pre_m3_golden(payload: object, golden: tuple) -> None:
    """本批只加观测，**不得**改变解析语义：plan 必须与 M3 之前的实现逐字段相同。"""
    plan, _ = QueryPlan.from_dictWithReport(payload)  # type: ignore[arg-type]
    assert astuple(plan) == golden


def test_golden_snapshot_covers_every_payload() -> None:
    """载荷表与黄金表必须等长 —— 防「加了载荷忘了黄金」这种两边悄悄错位。"""
    assert len(_GOLDEN_PAYLOADS) == len(_GOLDEN_SEMANTICS)


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
