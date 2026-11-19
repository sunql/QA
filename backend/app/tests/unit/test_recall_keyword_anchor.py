"""recall_keyword_anchor 纯函数单测：字面包含匹配兜向量措辞敏感的底。

生产事故对照：问「公司目前有多少供应商」时向量漏 DIM_SUPPLIER（别名
「供应商主数据」），字面锚定必须把它捞回来。
"""

from types import SimpleNamespace

from app.services.recall_keyword_anchor import selectAnchoredClasses


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
