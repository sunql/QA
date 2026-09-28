"""SQL 阶段 schema 裁剪单元测试（纯函数，不调 LLM、不碰 DB）。

背景（2026-09-28 token 治理）：`generateSql` 把全量召回 schema（15~30 个类
≈ 16k tokens）传给 `buildSchemaText`，但计划实际只选中 1~3 个类。
`_pruneClassesForSql` 按 plan 引用裁剪 classes，把 SQL 阶段的 schema 文本
压到只含真正用到的类。

契约（fail-closed）：任何「不确定」的情形一律返回全量——宁可多投 token，
不裁出一个缺表的 schema。因为 `buildSchemaText` 对缺类是**静默降级**
（JOIN 行跳过、继承标注丢失），缺表不会报错、只会生成错 SQL。
"""

from __future__ import annotations

from app.domain.models import OntologyClass
from app.domain.query_plan import JoinSpec, QueryPlan
from app.services.nl2sql_schema import _pruneClassesForSql


def _cls(name: str, cls_id: int) -> OntologyClass:
    """构造最小本体类（只需 class_name + id 参与裁剪判定）。"""
    return OntologyClass(id=cls_id, class_name=name, source_table=f"T_{name}")


def _classes() -> list[OntologyClass]:
    """4 个类：A / B / C / D，其中 D 是无关类（应被裁掉）。"""
    return [_cls("A", 1), _cls("B", 2), _cls("C", 3), _cls("D", 4)]


class TestPruneClassesForSql:
    def test_keeps_selected_and_join_hop_classes(self) -> None:
        # joins 里出现的 C 是 supplementJoinPath 补的中间 hop 类：
        # 它只存在于 plan.joins，不在 selectedClasses 里，必须额外并进来——
        # 否则 SQL 阶段会漏掉 「A JOIN C」的 JOIN 行（JOIN 知识唯一来源是 schema 文本）。
        plan = QueryPlan(
            target="x",
            selectedClasses=("A", "B"),
            joins=(JoinSpec(sourceClass="B", targetClass="C", columns=("B.C1",)),),
        )
        pruned = _pruneClassesForSql(plan, _classes())
        assert [c.class_name for c in pruned] == ["A", "B", "C"]

    def test_drops_unrelated_classes(self) -> None:
        plan = QueryPlan(target="x", selectedClasses=("A",))
        pruned = _pruneClassesForSql(plan, _classes())
        assert [c.class_name for c in pruned] == ["A"]

    def test_fails_closed_on_unresolvable_name(self) -> None:
        # plan 引用了一个不在 classes 里的类名（如 hop 类退化成裸表名）：
        # 无法无损裁剪 → 原样返回全量，绝不裁出缺表 schema。
        plan = QueryPlan(
            target="x",
            selectedClasses=("A",),
            joins=(JoinSpec(sourceClass="A", targetClass="GHOST_TABLE", columns=()),),
        )
        classes = _classes()
        pruned = _pruneClassesForSql(plan, classes)
        assert pruned == classes

    def test_none_plan_returns_full(self) -> None:
        classes = _classes()
        assert _pruneClassesForSql(None, classes) == classes

    def test_empty_selection_returns_full(self) -> None:
        classes = _classes()
        plan = QueryPlan(target="x")
        assert _pruneClassesForSql(plan, classes) == classes

    def test_empty_classes_returns_empty(self) -> None:
        plan = QueryPlan(target="x", selectedClasses=("A",))
        assert _pruneClassesForSql(plan, []) == []

    def test_preserves_object_identity_and_id(self) -> None:
        # 必须返回原对象（保留 .id）：buildSchemaText 的 _renderJoinLines 用
        # {cls.id: cls.source_table} 匹配 join 目录边，id 丢了就渲染不出 JOIN 行。
        classes = _classes()
        plan = QueryPlan(target="x", selectedClasses=("A",))
        pruned = _pruneClassesForSql(plan, classes)
        assert pruned[0] is classes[0]
        assert pruned[0].id == 1
