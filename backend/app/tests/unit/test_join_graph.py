"""Feature B JOIN 图与路径发现的单元测试。

覆盖 _buildJoinGraph / _findJoinPath / _resolveRefTable 模块函数，
以及 Nl2SqlService.supplementJoinPath / validateConnectivity /
_buildIndirectJoinHints / _finalizePlan 方法。

JOIN 图自关联关系目录（OntologyJoin）构建，运行时 JOIN 的唯一真源；
外键标志（is_foreign_key / ref_class_id）仅用于 _resolveRefTable 与
buildSchemaText 的信息性 [FK → 目标] 标记，不参与 _buildJoinGraph。
"""

from __future__ import annotations

import pytest

from app.domain.exceptions import Nl2SqlError
from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty
from app.domain.query_plan import JoinSpec, PlanResult, QueryPlan
from app.services.nl2sql_service import (
    Nl2SqlService,
    _buildJoinGraph,
    _findJoinPath,
    _resolveRefTable,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _makeProp(
    name: str,
    *,
    is_fk: bool = False,
    ref_id: int | None = None,
    col: str | None = None,
) -> OntologyProperty:
    return OntologyProperty(
        property_name=name,
        data_type="STRING",
        is_foreign_key=is_fk,
        ref_class_id=ref_id,
        source_column=col,
    )


def _makeClass(name: str, classId: int, props: list[OntologyProperty]) -> OntologyClass:
    return OntologyClass(
        id=classId,
        class_name=name,
        source_table=f"T_{name}",
        properties=props,
    )


def _makeJoin(
    sourceId: int,
    sourceCols: list[str],
    targetId: int,
    targetCols: list[str],
) -> OntologyJoin:
    """构造 join 目录边（join_key 与 service.makeJoinKey 同构，仅供单测构造）。"""
    return OntologyJoin(
        source_class_id=sourceId,
        source_columns=sourceCols,
        target_class_id=targetId,
        target_columns=targetCols,
        join_key=f"{sourceId}|{','.join(sourceCols)}->{targetId}|{','.join(targetCols)}",
    )


def _chainABC() -> list[OntologyClass]:
    """A --FK--> B --FK--> C 链（A 指向 B，B 指向 C，A 与 C 无直接外键）。"""
    a = _makeClass("A", 1, [_makeProp("b_ref", is_fk=True, ref_id=2, col="b_id")])
    b = _makeClass("B", 2, [_makeProp("c_ref", is_fk=True, ref_id=3, col="c_id")])
    c = _makeClass("C", 3, [])
    return [a, b, c]


def _chainABCJoins() -> list[OntologyJoin]:
    """与 _chainABC 对应的 join 目录边（A->B、B->C）。"""
    return [
        _makeJoin(1, ["b_id"], 2, ["b_id"]),
        _makeJoin(2, ["c_id"], 3, ["c_id"]),
    ]


def _service() -> Nl2SqlService:
    return Nl2SqlService()


# ---------------------------------------------------------------------------
# _buildJoinGraph
# ---------------------------------------------------------------------------

class TestBuildJoinGraph:
    def test_empty_classes_returns_empty(self) -> None:
        assert _buildJoinGraph([], []) == {}

    def test_no_joins_returns_empty(self) -> None:
        cls = _makeClass("A", 1, [_makeProp("name")])
        assert _buildJoinGraph([cls], []) == {}

    def test_single_join_creates_bidirectional_edge(self) -> None:
        a = _makeClass("A", 1, [])
        b = _makeClass("B", 2, [])
        graph = _buildJoinGraph([a, b], [_makeJoin(1, ["b_id"], 2, ["b_id"])])
        assert graph == {"T_A": [("T_B", "b_id")], "T_B": [("T_A", "b_id")]}

    def test_chain_join_links_all_three(self) -> None:
        graph = _buildJoinGraph(_chainABC(), _chainABCJoins())
        assert ("T_B", "b_id") in graph["T_A"]
        assert ("T_C", "c_id") in graph["T_B"]
        assert ("T_A", "b_id") in graph["T_B"]
        assert ("T_B", "c_id") in graph["T_C"]

    def test_join_endpoint_outside_subset_skipped(self) -> None:
        # join 边引用类 id=9（不在 classes 子集内）→ 整条边被跳过
        a = _makeClass("A", 1, [])
        b = _makeClass("B", 2, [])
        graph = _buildJoinGraph([a, b], [_makeJoin(1, ["b_id"], 9, ["b_id"])])
        assert graph == {}

    def test_empty_source_columns_uses_empty_via(self) -> None:
        a = _makeClass("A", 1, [])
        b = _makeClass("B", 2, [])
        graph = _buildJoinGraph([a, b], [_makeJoin(1, [], 2, [])])
        assert graph["T_A"] == [("T_B", "")]
        assert graph["T_B"] == [("T_A", "")]


# ---------------------------------------------------------------------------
# _findJoinPath
# ---------------------------------------------------------------------------

class TestFindJoinPath:
    def test_same_node_returns_empty(self) -> None:
        graph = _buildJoinGraph(_chainABC(), _chainABCJoins())
        assert _findJoinPath("T_A", "T_A", graph) == []

    def test_direct_neighbor_returns_empty(self) -> None:
        graph = _buildJoinGraph(_chainABC(), _chainABCJoins())
        assert _findJoinPath("T_A", "T_B", graph) == []

    def test_two_hop_returns_middle_table(self) -> None:
        graph = _buildJoinGraph(_chainABC(), _chainABCJoins())
        assert _findJoinPath("T_A", "T_C", graph) == ["T_B"]

    def test_no_path_returns_none(self) -> None:
        graph = _buildJoinGraph(_chainABC(), _chainABCJoins())
        assert _findJoinPath("T_A", "T_D", graph) is None


# ---------------------------------------------------------------------------
# _resolveRefTable
# ---------------------------------------------------------------------------

class TestResolveRefTable:
    def test_with_ref_class_id_lookup(self) -> None:
        a = _makeClass("A", 1, [_makeProp("b_ref", is_fk=True, ref_id=2, col="b_id")])
        b = _makeClass("B", 2, [])
        classesById = {cls.id: cls for cls in [a, b]}
        prop = a.properties[0]
        assert _resolveRefTable(prop, classesById) == "T_B"

    def test_no_fk_info_returns_none(self) -> None:
        prop = _makeProp("name")
        assert _resolveRefTable(prop, {}) is None


# ---------------------------------------------------------------------------
# supplementJoinPath
# ---------------------------------------------------------------------------

class TestSupplementJoinPath:
    def test_empty_joins_returns_plan_unchanged(self) -> None:
        plan = QueryPlan(target="x")
        result = _service().supplementJoinPath(plan, _chainABC(), _chainABCJoins())
        assert result is plan

    def test_no_join_graph_returns_plan_unchanged(self) -> None:
        plan = QueryPlan(
            target="x",
            joins=(JoinSpec(sourceClass="A", targetClass="B", columns=("x",)),),
        )
        cls = _makeClass("A", 1, [_makeProp("name")])
        result = _service().supplementJoinPath(plan, [cls], [])
        assert result is plan

    def test_direct_join_preserved(self) -> None:
        plan = QueryPlan(
            target="x",
            joins=(JoinSpec(sourceClass="A", targetClass="B", columns=("b_ref",)),),
        )
        result = _service().supplementJoinPath(plan, _chainABC(), _chainABCJoins())
        assert result.joins == plan.joins

    def test_indirect_join_replaced_with_hops(self) -> None:
        # A->C 无直接 join 边，应替换为 A->B + B->C，不保留原始 A->C
        plan = QueryPlan(
            target="x",
            joins=(JoinSpec(sourceClass="A", targetClass="C", columns=()),),
        )
        result = _service().supplementJoinPath(plan, _chainABC(), _chainABCJoins())
        pairs = {(j.sourceClass, j.targetClass) for j in result.joins}
        assert ("A", "C") not in pairs
        assert ("A", "B") in pairs
        assert ("B", "C") in pairs

    def test_no_path_join_preserved(self) -> None:
        # A->D 无路径（D 不在图里），保留原样交由 connectivity 报错
        plan = QueryPlan(
            target="x",
            joins=(JoinSpec(sourceClass="A", targetClass="D", columns=()),),
        )
        result = _service().supplementJoinPath(plan, _chainABC(), _chainABCJoins())
        assert result.joins == plan.joins


# ---------------------------------------------------------------------------
# validateConnectivity
# ---------------------------------------------------------------------------

class TestValidateConnectivity:
    def test_single_class_returns_empty(self) -> None:
        plan = QueryPlan(target="x", selectedClasses=("A",))
        assert _service().validateConnectivity(plan, _chainABC(), _chainABCJoins()) == []

    def test_no_joins_returns_empty(self) -> None:
        plan = QueryPlan(target="x", selectedClasses=("A", "C"))
        assert _service().validateConnectivity(plan, _chainABC(), _chainABCJoins()) == []

    def test_connected_through_intermediate_returns_empty(self) -> None:
        # A、C 通过中间表 B 连通
        plan = QueryPlan(
            target="x",
            selectedClasses=("A", "C"),
            joins=(JoinSpec(sourceClass="A", targetClass="B", columns=("b_ref",)),),
        )
        assert _service().validateConnectivity(plan, _chainABC(), _chainABCJoins()) == []

    def test_disconnected_classes_reported(self) -> None:
        d = _makeClass("D", 4, [])
        classes = _chainABC() + [d]
        plan = QueryPlan(
            target="x",
            selectedClasses=("A", "D"),
            joins=(JoinSpec(sourceClass="A", targetClass="D", columns=()),),
        )
        issues = _service().validateConnectivity(plan, classes, _chainABCJoins())
        # set 迭代顺序不确定，报错可能列 T_A 或 T_D，但必定有且仅有一条
        assert len(issues) == 1
        assert "无法通过关联路径连通" in issues[0]

    def test_no_join_edges_does_not_false_report(self) -> None:
        a = _makeClass("A", 1, [_makeProp("name")])
        b = _makeClass("B", 2, [_makeProp("name")])
        plan = QueryPlan(
            target="x",
            selectedClasses=("A", "B"),
            joins=(JoinSpec(sourceClass="A", targetClass="B", columns=("name",)),),
        )
        assert _service().validateConnectivity(plan, [a, b], []) == []


# ---------------------------------------------------------------------------
# _buildIndirectJoinHints
# ---------------------------------------------------------------------------

class TestBuildIndirectJoinHints:
    def test_no_joins_returns_empty(self) -> None:
        cls = _makeClass("A", 1, [_makeProp("name")])
        assert _service()._buildIndirectJoinHints([cls], []) == ""

    def test_only_direct_join_returns_empty(self) -> None:
        a = _makeClass("A", 1, [])
        b = _makeClass("B", 2, [])
        assert _service()._buildIndirectJoinHints([a, b], [_makeJoin(1, ["b_id"], 2, ["b_id"])]) == ""

    def test_two_hop_path_appears_in_hints(self) -> None:
        hints = _service()._buildIndirectJoinHints(_chainABC(), _chainABCJoins())
        assert "A -> B -> C" in hints
        assert "间接 JOIN" in hints

    def test_skips_path_when_direct_edge_exists(self) -> None:
        # A->B、B->C、A->C 直接，A->B->C 应被跳过
        a = _makeClass("A", 1, [])
        b = _makeClass("B", 2, [])
        c = _makeClass("C", 3, [])
        joins = [
            _makeJoin(1, ["b_id"], 2, ["b_id"]),
            _makeJoin(2, ["c_id"], 3, ["c_id"]),
            _makeJoin(1, ["c_id"], 3, ["c_id"]),
        ]
        assert _service()._buildIndirectJoinHints([a, b, c], joins) == ""


# ---------------------------------------------------------------------------
# _finalizePlan
# ---------------------------------------------------------------------------

class TestFinalizePlan:
    def test_supplements_and_returns_plan(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("A", "C"),
            joins=(JoinSpec(sourceClass="A", targetClass="C", columns=()),),
        )
        planResult = PlanResult(plan=plan, promptTokens=10, completionTokens=5)
        result = _service()._finalizePlan(planResult, _chainABC(), _chainABCJoins())
        assert result.promptTokens == 10
        pairs = {(j.sourceClass, j.targetClass) for j in result.plan.joins}
        assert ("A", "B") in pairs and ("B", "C") in pairs

    def test_raises_on_disconnected(self) -> None:
        d = _makeClass("D", 4, [])
        plan = QueryPlan(
            target="x",
            selectedClasses=("A", "D"),
            joins=(JoinSpec(sourceClass="A", targetClass="D", columns=()),),
        )
        planResult = PlanResult(plan=plan, promptTokens=10, completionTokens=5)
        with pytest.raises(Nl2SqlError):
            _service()._finalizePlan(planResult, _chainABC() + [d], _chainABCJoins())


# ---------------------------------------------------------------------------
# buildSchemaText 注入间接路径 + JOIN 关系
# ---------------------------------------------------------------------------

class TestBuildSchemaTextWithHints:
    def test_schema_text_includes_indirect_join_hints(self) -> None:
        text = _service().buildSchemaText(_chainABC(), joins=_chainABCJoins())
        assert "间接 JOIN 路径参考" in text
        assert "A -> B -> C" in text

    def test_schema_text_renders_join_catalog(self) -> None:
        text = _service().buildSchemaText(_chainABC(), joins=_chainABCJoins())
        assert "### JOIN 关系" in text
        assert "T_A.b_id → T_B.b_id" in text
        assert "T_B.c_id → T_C.c_id" in text

    def test_schema_text_skips_join_outside_subset(self) -> None:
        # join 边引用不在 classes 子集内的类 id → 不渲染该边
        a = _makeClass("A", 1, [])
        b = _makeClass("B", 2, [])
        joins = [
            _makeJoin(1, ["b_id"], 2, ["b_id"]),
            _makeJoin(1, ["x_id"], 9, ["x_id"]),
        ]
        text = _service().buildSchemaText([a, b], joins=joins)
        assert "T_A.b_id → T_B.b_id" in text
        assert "x_id" not in text
