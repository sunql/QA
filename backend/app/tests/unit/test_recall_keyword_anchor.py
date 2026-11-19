"""recall_keyword_anchor 纯函数单测：字面包含匹配兜向量措辞敏感的底。

生产事故对照：问「公司目前有多少供应商」时向量漏 DIM_SUPPLIER（别名
「供应商主数据」），字面锚定必须把它捞回来。
"""

from types import SimpleNamespace

from app.services.recall_keyword_anchor import (
    selectAnchoredClasses,
    selectAnchoredMatches,
)


def _cls(cid: int, name: str, alias: str | None) -> SimpleNamespace:
    return SimpleNamespace(id=cid, class_name=name, class_alias=alias)


def test_alias_containment_anchors_missed_class() -> None:
    dim = _cls(12, "DIM_SUPPLIER", "供应商主数据")
    other = _cls(30, "SUPPLY_ACCESS", "供应商准入")
    anchored = selectAnchoredClasses("公司目前有多少供应商", [other, dim], maxAnchored=5)
    # 两个类都被「供应商」片段命中；精确/更长片段优先，id 兜底确定性
    assert [c.id for c in anchored][:2] == [12, 30]  # 供应商主数据(片段更长)在前


def test_exact_match_beats_containment() -> None:
    exact = _cls(1, "SUPPLIER", "供应商")
    partial = _cls(2, "DIM_SUPPLIER", "供应商主数据")
    anchored = selectAnchoredClasses("供应商", [partial, exact], maxAnchored=5)
    assert [c.id for c in anchored] == [1, 2]


def test_cap_limits_anchored_count() -> None:
    classes = [_cls(i, f"TABLE_{i}", f"供应商{i}号档案") for i in range(10)]
    anchored = selectAnchoredClasses("供应商", classes, maxAnchored=5)
    assert len(anchored) == 5


def test_no_match_returns_empty() -> None:
    anchored = selectAnchoredClasses(
        "完全无关的问题", [_cls(1, "DIM_SUPPLIER", "供应商主数据")], maxAnchored=5,
    )
    assert anchored == []


def test_zero_cap_or_blank_question_is_noop() -> None:
    dim = _cls(12, "DIM_SUPPLIER", "供应商主数据")
    assert selectAnchoredClasses("供应商", [dim], maxAnchored=0) == []
    assert selectAnchoredClasses("   ", [dim], maxAnchored=5) == []


# ── 命中强度明细（供降级救援路径做证据闸门）────────────────────────


def test_matches_expose_fragment_length_and_exactness() -> None:
    """包含匹配：非精确 + 片段长度；精确等值：isExact=True。"""
    partial = _cls(1, "DIM_SUPPLIER", "供应商主数据")
    exact = _cls(2, "SUPPLIER", "供应商")

    matches = selectAnchoredMatches("供应商", [partial, exact], maxAnchored=5)

    byId = {m.cls.id: m for m in matches}
    assert byId[1].isExact is False
    assert byId[1].fragmentLength == 3  # 「供应商」
    assert byId[2].isExact is True
    assert byId[2].fragmentLength == 3


def test_matches_report_short_fragment_for_common_word() -> None:
    """常用词只命中 2 字片段 ⇒ 证据弱（下游据此不收窄窗口）。"""
    company = _cls(1, "DIM_COMPANY", "公司主数据")

    matches = selectAnchoredMatches("公司信息有哪些", [company], maxAnchored=5)

    assert len(matches) == 1
    assert matches[0].isExact is False
    assert matches[0].fragmentLength == 2  # 「公司」


def test_matches_keep_same_order_and_cap_as_classes_api() -> None:
    """两个入口顺序/上限一致（兼容层不是另一套实现）。"""
    classes = [_cls(i, f"T{i}", f"供应商{i}号档案") for i in range(1, 6)]

    matches = selectAnchoredMatches("供应商", classes, maxAnchored=3)
    plain = selectAnchoredClasses("供应商", classes, maxAnchored=3)

    assert [m.cls.id for m in matches] == [c.id for c in plain]
    assert len(matches) == 3


def test_matches_noop_on_empty_input() -> None:
    assert selectAnchoredMatches("供应商", [], maxAnchored=5) == []
    assert selectAnchoredMatches("  ", [_cls(1, "A", "供应商")], maxAnchored=5) == []
    assert selectAnchoredMatches("供应商", [_cls(1, "A", "供应商")], maxAnchored=0) == []
