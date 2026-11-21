"""M2-2b 受控邻接（`GET /procurement/knowledge/graph/neighbors`，设计稿 §8.3）。

判据要点：
1. **关系白名单是受控边界**：请求的 relationTypes 与 `BUSINESS_RELATION_TYPES`
   取交集——白名单外的类型**不可遍历**（静默丢弃但经 `effectiveRelationTypes`
   自证，调用方不会误以为跑了一条被丢弃的路）；
2. **truncated 自证**：边数超过 `MAX_NEIGHBOR_EDGES` ⇒ 截断 + 置位（防深跳
   大子图撑爆响应——92 万 CONTAINS 行的真实规模）；
3. **复用既有守卫**：label 白名单 / 起点存在性（404）/ 深度 [1,5] 全部来自
   `GraphTraversalService.traverse`，本文件不重复实现；
4. **双接线守卫**：main.py 与 _testapp.py 同块注册（漏一边 ⇒ 404）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.domain.knowledge_schemas import ProcurementGraphNeighborsRead
from app.infrastructure.neo4j_client import BUSINESS_RELATION_TYPES
from app.services.knowledge.graph_neighbors import (
    MAX_NEIGHBOR_EDGES,
    buildNeighborsView,
)


def _hop(depth: int, rel: str, toKey: str, toType: str = "Material") -> SimpleNamespace:
    return SimpleNamespace(
        depth=depth,
        from_key="100001", from_code="SUP001", from_name="泰鸿钢铁",
        from_type="Supplier",
        rel_type=rel,
        to_key=toKey, to_code=toKey, to_name=f"节点{toKey}", to_type=toType,
    )


def _hops(n: int, rel: str = "SUPPLIES") -> list[SimpleNamespace]:
    return [_hop(1, rel, f"M{i:04d}") for i in range(n)]


def _build(hops, requested=None, maxEdges=MAX_NEIGHBOR_EDGES):
    return buildNeighborsView(
        hops,
        startType="Supplier",
        startKey="100001",
        maxHops=2,
        requestedRelationTypes=requested,
        maxEdges=maxEdges,
    )


# ── 1. 纯函数 ─────────────────────────────────────────────────────────────


def test_buildNeighborsView_filtersRelationsByWhitelistIntersection() -> None:
    hops = _hops(3, "SUPPLIES") + [_hop(1, "CONTAINS", "X1")]
    view = _build(hops, requested=["SUPPLIES", "NOT_IN_WHITELIST"])
    assert isinstance(view, ProcurementGraphNeighborsRead)
    assert {e.rel_type for e in view.edges} == {"SUPPLIES"}, "白名单外类型必须被丢弃"
    assert view.effective_relation_types == ("SUPPLIES",), (
        "实际生效的关系类型必须自证（白名单交集）"
    )


def test_buildNeighborsView_emptyRelationTypesUsesFullWhitelist() -> None:
    view = _build(_hops(2, "SUPPLIES") + [_hop(1, "CONTAINS", "X1")], requested=None)
    assert {e.rel_type for e in view.edges} == {"SUPPLIES", "CONTAINS"}
    assert view.effective_relation_types == tuple(sorted(BUSINESS_RELATION_TYPES))


def test_buildNeighborsView_dedupesNodes() -> None:
    hops = [_hop(1, "SUPPLIES", "M1"), _hop(2, "CONTAINS", "M1")]
    view = _build(hops)
    keys = [n.key for n in view.nodes]
    assert len(keys) == len(set(keys)), "节点必须按 key 去重"
    assert "100001" in keys and "M1" in keys


def test_buildNeighborsView_truncatesAtCap() -> None:
    hops = _hops(MAX_NEIGHBOR_EDGES + 5)
    view = _build(hops)
    assert len(view.edges) == MAX_NEIGHBOR_EDGES
    assert view.truncated is True, "超上限必须置 truncated（自证）"


def test_buildNeighborsView_underCapNotTruncated() -> None:
    view = _build(_hops(3))
    assert len(view.edges) == 3 and view.truncated is False


def test_buildNeighborsView_emptyHopsCarriesRequestIdentity() -> None:
    view = _build([])
    assert view.edges == [] and view.nodes == [] and view.truncated is False
    assert view.entity_type == "Supplier" and view.entity_key == "100001"
    assert view.max_hops == 2


# ── 2. 双接线守卫 ─────────────────────────────────────────────────────────


def test_graph_neighbors_registered_in_both_wirings() -> None:
    """main.py 与 _testapp.py 必须同在 enableKnowledgeFacade 块内注册本路由。"""
    backendRoot = Path(__file__).resolve().parents[3]
    for rel in ("app/main.py", "app/tests/_testapp.py"):
        code = (backendRoot / rel).read_text(encoding="utf-8")
        assert "procurement_knowledge_graph" in code, f"{rel} 未导入新路由模块"
        assert "procurement_knowledge_graph.router" in code, (
            f"{rel} 未注册 graph/neighbors 路由（漏一边 ⇒ 那一侧 404）"
        )


def test_invalid_label_returns_422_not_500() -> None:
    """M2-2b 修正：非法 entityType ⇒ **422**（初版 ValueError 直穿成 500，Q01 探针实证）。"""
    import inspect

    from app.api.v1 import procurement_knowledge_graph as mod

    src = inspect.getsource(mod.getProcurementGraphNeighbors)
    assert "BUSINESS_ENTITY_LABELS" in src, "必须校验 entityType 白名单"
    assert "HTTPException" in src and "422" in src, "非法 label 必须转 422"
