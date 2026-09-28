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

from app.domain.models import OntologyClass, OntologyProperty
from app.domain.query_plan import JoinSpec, QueryPlan
from app.services.nl2sql_schema import _pruneClassesForSql, buildSchemaText


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


def _propClass(props: list[dict]) -> OntologyClass:
    """构造带属性的类，用于列行渲染契约测试。"""
    return OntologyClass(
        id=1,
        class_name="T",
        source_table="THBI.T",
        properties=[OntologyProperty(**p) for p in props],
    )


class TestColumnEchoElision:
    """`(column=X)` 只在左侧 token 读不出物理列时渲染（feat-token-prune，2026-09-28）。

    实测依据：全库 3163 条活属性 `source_column` 恒等于 `property_name`（无 NULL / 空串 /
    偏离）；容器内 dump 的 636/636 渲染行，其物理列都能从 nameToken 读出
    （`TSICOD_0: STRING (column=TSICOD_0)`）→ 该标注是零信息量回声，占 schema 文本
    **39.0%**，且计划阶段与 SQL 阶段各投一遍。

    与方案 A 的差别：这不是「子集裁剪」而是「等价重编码」，不丢任何信息；唯一需要
    保住的是 `column=未映射`（`nl2sql_prompts` 三处规则引用它），故按「X 读不出来才渲染」
    的口径它天然保留。
    """

    def test_omits_echo_when_column_equals_property_name(self) -> None:
        text = buildSchemaText(
            [
                _propClass(
                    [
                        {
                            "property_name": "STATUS",
                            "data_type": "STRING",
                            "source_column": "STATUS",
                        }
                    ]
                )
            ]
        )
        assert "STATUS: STRING" in text
        assert "(column=STATUS)" not in text

    def test_omits_echo_when_column_equals_alias(self) -> None:
        # 单别名渲染在名称位（`BPSNUM (BPSNUM_0)`）——物理列已在括号里，无需再回声。
        text = buildSchemaText(
            [
                _propClass(
                    [
                        {
                            "property_name": "BPSNUM",
                            "property_alias": "BPSNUM_0",
                            "data_type": "STRING",
                            "source_column": "BPSNUM_0",
                        }
                    ]
                )
            ]
        )
        assert "BPSNUM (BPSNUM_0): STRING" in text
        assert "(column=BPSNUM_0)" not in text

    def test_keeps_column_when_it_differs_from_name_and_alias(self) -> None:
        # 名字与别名都读不出物理列 → 标注是唯一来源，必须保留（未来启用别名映射时靠这条兜底）。
        text = buildSchemaText(
            [
                _propClass(
                    [
                        {
                            "property_name": "PTHNUM",
                            "property_alias": "订单号",
                            "data_type": "STRING",
                            "source_column": "PTHNUM_0",
                        }
                    ]
                )
            ]
        )
        assert "PTHNUM (订单号): STRING (column=PTHNUM_0)" in text

    def test_keeps_unmapped_marker(self) -> None:
        # `column=未映射` 是承重信号（prompt 规则禁止选中/引用未映射属性），恒保留。
        text = buildSchemaText(
            [
                _propClass(
                    [
                        {
                            "property_name": "QTY",
                            "data_type": "DECIMAL",
                            "source_column": None,
                        }
                    ]
                )
            ]
        )
        assert "QTY: DECIMAL (column=未映射)" in text

    def test_markers_still_render_after_elision(self) -> None:
        # 省略 column 标注后，PK/FK 标记的拼接不能少空格、不能丢。
        text = buildSchemaText(
            [
                _propClass(
                    [
                        {
                            "property_name": "STATUS",
                            "data_type": "STRING",
                            "source_column": "STATUS",
                            "is_primary_key": True,
                        }
                    ]
                )
            ]
        )
        assert "STATUS: STRING [PK]" in text
