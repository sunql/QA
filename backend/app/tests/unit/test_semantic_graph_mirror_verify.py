"""镜像 vs 计划的**对账**单测（P3b D24）。

对账是**纯比较**（无 IO）：计划（manifest + 解析 ⇒ `buildMirrorPlan` 的产物）为
「应然」，图上读回的节点/边为「实然」，逐条求差：

- `missingNodes` / `extraNodes`：按 (entityType, unified_id) 求差；
- `fieldMismatches`：两边都有的节点逐字段比（实体字段 + 发布标识字段）；
- `missingEdges` / `extraEdges`：按 (relType, 源 uid, 目标 uid) 求差（边无属性 ⇒
  边对账只有端点对，没有字段可比）。

输出**确定性排序** ⇒ 报告可复现、可 diff。
"""

from __future__ import annotations

from app.services.semantic_graph_mirror import MirrorEdge, MirrorNode, MirrorPlan
from app.services.semantic_graph_mirror_verify import verifyMirrorAgainstPlan


def _plan(nodes: list[MirrorNode], edges: list[MirrorEdge]) -> MirrorPlan:
    return MirrorPlan(
        releaseId=7,
        releaseVersion="2026.11.09-1",
        nodes=tuple(nodes),
        edges=tuple(edges),
        unresolved=(),
    )


def _classNode(uid: str, name: str) -> MirrorNode:
    return MirrorNode(
        entityType="class", unifiedId=uid, fields=(("name", name), ("alias", ""))
    )


def _propNode(uid: str, name: str) -> MirrorNode:
    return MirrorNode(entityType="property", unifiedId=uid, fields=(("name", name),))


def _actualNode(uid: str, name: str, *, releaseVersion: str = "2026.11.09-1") -> dict:
    return {
        "unified_id": uid,
        "name": name,
        "alias": "",
        "release_id": 7,
        "release_version": releaseVersion,
        "mirror_of": uid,
    }


def _nodePayload(nodes: list[dict]) -> dict[str, list[dict]]:
    return {"class": list(nodes), "property": [], "metric": [], "kpi": []}


def _edgePayload(edges: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """图上读回的**原始形态**：(relType, 源 uid, 目标 uid) 三元组列表。"""
    return list(edges)


def test_图与计划完全一致时对账通过() -> None:
    plan = _plan([_classNode("obj:CLASS:1", "采购订单")], [])
    result = verifyMirrorAgainstPlan(
        plan, _nodePayload([_actualNode("obj:CLASS:1", "采购订单")]), {}
    )
    assert result.status == "consistent"
    assert not result.missingNodes
    assert not result.extraNodes
    assert not result.fieldMismatches
    assert not result.missingEdges
    assert not result.extraEdges


def test_计划有图上没有的节点记缺失() -> None:
    plan = _plan(
        [_classNode("obj:CLASS:1", "采购订单"), _classNode("obj:CLASS:2", "供应商")], []
    )
    result = verifyMirrorAgainstPlan(
        plan, _nodePayload([_actualNode("obj:CLASS:1", "采购订单")]), {}
    )
    assert result.status == "inconsistent"
    assert [(d.entityType, d.unifiedId) for d in result.missingNodes] == [
        ("class", "obj:CLASS:2")
    ]


def test_图上有计划外的节点记多余() -> None:
    plan = _plan([_classNode("obj:CLASS:1", "采购订单")], [])
    result = verifyMirrorAgainstPlan(
        plan,
        _nodePayload(
            [_actualNode("obj:CLASS:1", "采购订单"), _actualNode("obj:CLASS:9", "残留")]
        ),
        {},
    )
    assert [(d.entityType, d.unifiedId) for d in result.extraNodes] == [
        ("class", "obj:CLASS:9")
    ]


def test_节点字段被改记逐字段差异() -> None:
    plan = _plan([_classNode("obj:CLASS:1", "采购订单")], [])
    result = verifyMirrorAgainstPlan(
        plan, _nodePayload([_actualNode("obj:CLASS:1", "被改过的名字")]), {}
    )
    assert len(result.fieldMismatches) == 1
    m = result.fieldMismatches[0]
    assert (m.entityType, m.unifiedId, m.field) == ("class", "obj:CLASS:1", "name")
    assert m.expected == "采购订单"
    assert m.actual == "被改过的名字"


def test_发布标识字段也要比() -> None:
    """release_version / mirror_of 是镜像身份的一部分 ⇒ 不符就是不一致。"""
    plan = _plan([_classNode("obj:CLASS:1", "采购订单")], [])
    result = verifyMirrorAgainstPlan(
        plan,
        _nodePayload([_actualNode("obj:CLASS:1", "采购订单", releaseVersion="旧版本")]),
        {},
    )
    fields = {(m.field, m.expected, m.actual) for m in result.fieldMismatches}
    assert ("release_version", "2026.11.09-1", "旧版本") in fields


def test_缺边与多边都按端点对记录() -> None:
    plan = _plan(
        [_classNode("obj:CLASS:1", "采购订单"), _propNode("obj:PROPERTY:1", "po_no")],
        [MirrorEdge("HAS_PROPERTY", "class", "obj:CLASS:1", "property", "obj:PROPERTY:1")],
    )
    # 图上缺这条边，多了一条计划外的（图上读回的是**三元组列表**）
    result = verifyMirrorAgainstPlan(
        plan,
        _nodePayload(
            [_actualNode("obj:CLASS:1", "采购订单"), _actualNode("obj:PROPERTY:1", "po_no")]
        ),
        [("HAS_PROPERTY", "obj:CLASS:1", "obj:PROPERTY:2")],
    )
    assert [(e.relType, e.sourceUid, e.targetUid) for e in result.missingEdges] == [
        ("HAS_PROPERTY", "obj:CLASS:1", "obj:PROPERTY:1")
    ]
    assert [(e.relType, e.sourceUid, e.targetUid) for e in result.extraEdges] == [
        ("HAS_PROPERTY", "obj:CLASS:1", "obj:PROPERTY:2")
    ]


def test_对账输出确定性_打乱输入顺序同输出() -> None:
    nodes = [_classNode(f"obj:CLASS:{i}", f"类{i}") for i in range(1, 6)]
    plan = _plan(nodes, [])
    shuffled = list(reversed(nodes))
    r1 = verifyMirrorAgainstPlan(plan, _nodePayload([_actualNode(n.unifiedId, "一致") for n in nodes]), {})
    r2 = verifyMirrorAgainstPlan(
        _plan(shuffled, []),
        _nodePayload([_actualNode(n.unifiedId, "一致") for n in reversed(nodes)]),
        {},
    )
    assert r1 == r2
    plan2 = _plan(nodes, [])
    actual = _nodePayload(
        [_actualNode(n.unifiedId, f"漂移{idx}") for idx, n in enumerate(shuffled)]
    )
    result = verifyMirrorAgainstPlan(plan2, actual, {})
    uids = [m.unifiedId for m in result.fieldMismatches]
    assert uids == sorted(uids)


def test_空计划对空图一致_对非空图全记多余() -> None:
    empty = _plan([], [])
    assert verifyMirrorAgainstPlan(empty, _nodePayload([]), {}).status == "consistent"
    result = verifyMirrorAgainstPlan(
        empty, _nodePayload([_actualNode("obj:CLASS:1", "孤儿")]), {}
    )
    assert [(d.entityType, d.unifiedId) for d in result.extraNodes] == [
        ("class", "obj:CLASS:1")
    ]
