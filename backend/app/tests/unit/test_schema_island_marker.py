"""Prompt 孤岛类标记单元测试（纯函数，不调 LLM、不碰 DB）。

背景（2026-10-03 真机）：`buildSchemaText` 渲染 `### JOIN 关系` 段时，
召回集内**零 JOIN 边**的类（孤岛）根本不出现在该段里，而 schema 文本
没有任何文字说明「这个类不可跨表 JOIN」。模型只能靠猜 —— 真机上 MiniMax-M3
选了孤岛类 DIM_FACILITY，连通性校验失败且**不可自愈**（不回灌重试）。

本模块锁的契约：对孤岛类加显式标记，让模型第一眼就知道不能拿它做跨表关联。

⚠️ 三条易错边界（都有对应用例）：
1. `joins` 为空/图为空 ⇒ **不标记** —— 此时 `validateConnectivity` 也不拦
   （`nl2sql_plan.validateConnectivity` 首行 `if not joins: return []`），
   标记会让 prompt 与校验口径不一致，模型被误导去避一个其实能用的类。
2. **只标零边类**，有边类一个字都不加 —— schema 文本已占 19k 字符，
   多余标注是纯 token 浪费。
3. 断言必须**双向**（孤岛被标 + 连通类不被标），否则一个「全标」的实现
   也能让只查孤岛的用例通过。
"""

from __future__ import annotations

from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty
from app.services.nl2sql_schema import buildSchemaText

# 孤岛标记的完整字面量。改动它必须同步改 buildSchemaText 里的常量，
# 并检查 nl2sql_prompts.py 的规则文案是否引用了同一措辞。
ISLAND_MARKER = "无关联边，不可跨表JOIN"


def _cls(name: str, cls_id: int) -> OntologyClass:
    return OntologyClass(
        id=cls_id,
        class_name=name,
        source_table=f"T_{name}",
        properties=[
            OntologyProperty(property_name="c1", data_type="STRING", source_column="c1")
        ],
    )


def _join(srcId: int, tgtId: int) -> OntologyJoin:
    return OntologyJoin(
        source_class_id=srcId,
        source_columns=["c1"],
        target_class_id=tgtId,
        target_columns=["c1"],
        join_key=f"{srcId}|c1->{tgtId}|c1",
    )


def _headerLine(text: str, className: str) -> str:
    """取某类的 header 行（`### NAME...: table=X`），供逐类断言。"""
    for line in text.splitlines():
        if line.startswith(f"### {className}") and ": table=" in line:
            return line
    raise AssertionError(f"schema 文本里找不到类 {className} 的 header 行")


class TestIslandMarker:
    def test_island_class_gets_marker(self) -> None:
        # A--B 有边；C 是孤岛（零边）。只有 C 该被标。
        classes = [_cls("A", 1), _cls("B", 2), _cls("C", 3)]
        text = buildSchemaText(classes, joins=[_join(1, 2)])

        assert ISLAND_MARKER in _headerLine(text, "C")

    def test_connected_class_has_no_marker(self) -> None:
        # 双向断言的另一半：连通类一个字都不能多。
        classes = [_cls("A", 1), _cls("B", 2), _cls("C", 3)]
        text = buildSchemaText(classes, joins=[_join(1, 2)])

        assert ISLAND_MARKER not in _headerLine(text, "A")
        assert ISLAND_MARKER not in _headerLine(text, "B")

    def test_no_joins_at_all_means_no_marker(self) -> None:
        # joins 为空 ⇒ validateConnectivity 首行就放行，prompt 不得说「不可 JOIN」。
        classes = [_cls("A", 1), _cls("B", 2)]
        text = buildSchemaText(classes, joins=None)

        assert ISLAND_MARKER not in text

    def test_empty_join_list_means_no_marker(self) -> None:
        # joins=[] 与 None 同义：图建不起来，校验不拦。
        text = buildSchemaText([_cls("A", 1), _cls("B", 2)], joins=[])

        assert ISLAND_MARKER not in text

    def test_edges_outside_recall_yield_empty_graph_so_no_marker(self) -> None:
        # joins 非空但两端都不在召回集 ⇒ _buildJoinGraph 丢弃该边 ⇒ 图为空。
        # 图空时 validateConnectivity 首行就放行（`if not graph: return []`），
        # 此时标孤岛会让 prompt 劝退一个校验根本不拦的类 —— 必须与校验同口径。
        classes = [_cls("A", 1), _cls("C", 3)]
        text = buildSchemaText(classes, joins=[_join(1, 999)])

        assert ISLAND_MARKER not in text

    def test_partial_recall_marks_only_the_truly_isolated(self) -> None:
        # A--B 有边且两端都在召回集（进图）；C 没有任何边 ⇒ 图非空，只有 C 是孤岛。
        # 这条覆盖「图非空但仍含孤岛」这一真实情形（真机 step2 召回集正是如此）。
        classes = [_cls("A", 1), _cls("B", 2), _cls("C", 3), _cls("D", 4)]
        text = buildSchemaText(classes, joins=[_join(1, 2), _join(3, 999)])

        assert ISLAND_MARKER in _headerLine(text, "C")
        assert ISLAND_MARKER not in _headerLine(text, "A")
        assert ISLAND_MARKER not in _headerLine(text, "B")

    def test_marker_is_a_hint_not_a_ban(self) -> None:
        # 标记只说「不可跨表 JOIN」，不能写成「不可用」—— 孤岛类做单表查询
        # 完全合法（真机 deepseek 就是拿裸 RCV_SITE_CODE 分组跑通的）。
        classes = [_cls("A", 1), _cls("B", 2), _cls("C", 3)]
        line = _headerLine(buildSchemaText(classes, joins=[_join(1, 2)]), "C")

        assert "不可用" not in line
        assert "禁用" not in line

    def test_class_without_source_table_is_not_marked(self) -> None:
        # 无 source_table 的类连 buildJoinGraph 都进不去，标记它没有意义
        # （且它在主循环里就被 `if not cls.source_table: continue` 跳过了）。
        orphan = OntologyClass(id=9, class_name="ORPHAN", source_table=None)
        classes = [_cls("A", 1), _cls("B", 2), orphan]
        text = buildSchemaText(classes, joins=[_join(1, 2)])

        assert "ORPHAN" not in text