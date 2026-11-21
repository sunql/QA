"""A6：``property_alias`` 回填规划的纯函数单测。

钉死的四条：
1. **绝不覆盖**已有人工 alias（ASSET 域 8 条是别人的资产）；
2. 映射写错（类名/列名不存在、重复行）必须**报错**，不能静默跳过；
3. 同作用域一个 alias 指向两个物理列 ⇒ 必须报冲突（否则 resolver 返回 unknown）；
   **冲突判定必须把库内已有 alias 算进去**（只算本批 fills 会漏掉「回填撞存量」）；
4. 不同作用域的同名 alias 不算冲突（反向守卫：否则会把正常映射判红）；
5. 映射必须覆盖被测出的全部引用列（``entries ∪ excluded``），否则「45 = 32 + 13」
   只是文档里的手工对账；同名类的多条有效记录必须报错（写入目标不确定）。
"""

from __future__ import annotations

import pytest

from app.services.kpi_property_alias_backfill import (
    ACTION_FILL,
    ACTION_KEEP,
    AliasBackfillError,
    AliasEntry,
    ExcludedTarget,
    PropertyRow,
    assertMappingCoverage,
    findScopeCollisions,
    parseExcluded,
    parseMapping,
    planAliasFill,
    renderReport,
)

_ROW = PropertyRow(
    class_name="DWD_PURCHASE_ORDER_LINE",
    property_name="RECEIPT_DATE",
    source_column="RECEIPT_DATE",
    current_alias=None,
)


class TestParseMapping:
    def test_接受对象或裸数组(self):
        rows = [{"className": "A", "sourceColumn": "C", "alias": "别名"}]
        assert parseMapping({"entries": rows}) == parseMapping(rows)

    def test_空数组被拒绝(self):
        with pytest.raises(AliasBackfillError):
            parseMapping({"entries": []})

    def test_缺字段被拒绝(self):
        with pytest.raises(AliasBackfillError):
            parseMapping([{"className": "A", "alias": "别名"}])

    def test_重复的类列组合被拒绝(self):
        row = {"className": "A", "sourceColumn": "C", "alias": "别名"}
        with pytest.raises(AliasBackfillError):
            parseMapping([row, row])

    def test_空白被剥离(self):
        (entry,) = parseMapping(
            [{"className": " A ", "sourceColumn": " C ", "alias": " 别名 "}]
        )
        assert (entry.class_name, entry.source_column, entry.alias) == ("A", "C", "别名")


class TestPlanAliasFill:
    def test_空alias填充(self):
        (plan,) = planAliasFill(
            (AliasEntry("DWD_PURCHASE_ORDER_LINE", "RECEIPT_DATE", "收货日期"),), (_ROW,)
        )
        assert plan.action == ACTION_FILL
        assert plan.has_change

    def test_已有人工alias一律保留(self):
        row = PropertyRow("DWD_PURCHASE_ORDER_LINE", "RECEIPT_DATE", "RECEIPT_DATE", "人工名")
        (plan,) = planAliasFill(
            (AliasEntry("DWD_PURCHASE_ORDER_LINE", "RECEIPT_DATE", "收货日期"),), (row,)
        )
        assert plan.action == ACTION_KEEP
        assert not plan.has_change
        assert plan.current_alias == "人工名"

    def test_目标不存在必须报错并列出全部(self):
        entries = (
            AliasEntry("NO_SUCH_CLASS", "C1", "别名"),
            AliasEntry("DWD_PURCHASE_ORDER_LINE", "NO_SUCH_COLUMN", "别名"),
        )
        with pytest.raises(AliasBackfillError) as exc:
            planAliasFill(entries, (_ROW,))
        assert "NO_SUCH_CLASS.C1" in str(exc.value)
        assert "DWD_PURCHASE_ORDER_LINE.NO_SUCH_COLUMN" in str(exc.value)

    def test_物理列为空的属性不参与匹配(self):
        row = PropertyRow("DWD_PURCHASE_ORDER_LINE", "虚拟属性", None, None)
        with pytest.raises(AliasBackfillError):
            planAliasFill((AliasEntry("DWD_PURCHASE_ORDER_LINE", "虚拟属性", "别名"),), (row,))


class TestFindScopeCollisions:
    def _plans(self):
        return planAliasFill(
            (
                AliasEntry("T_A", "COL", "单价"),
                AliasEntry("T_B", "COL", "单价"),
            ),
            (
                PropertyRow("T_A", "COL", "COL", None),
                PropertyRow("T_B", "COL", "COL", None),
            ),
        )

    def test_同作用域同alias多物理列报冲突(self):
        collisions = findScopeCollisions(
            self._plans(), {"K1": {("T_A", "COL"), ("T_B", "COL")}}
        )
        assert len(collisions) == 1
        assert collisions[0].kpi_code == "K1"
        assert collisions[0].alias == "单价"
        assert len(collisions[0].columns) == 2

    def test_不同作用域的同名alias不算冲突(self):
        collisions = findScopeCollisions(
            self._plans(), {"K1": {("T_A", "COL")}, "K2": {("T_B", "COL")}}
        )
        assert collisions == ()

    def test_本批回填与库内已有alias冲突必须报出(self):
        """HIGH 回归：只看本批 fills 会漏掉「存量 alias 撞车」⇒ 写完后 resolver 返 unknown。"""
        plans = planAliasFill(
            (AliasEntry("T_A", "COL", "单价"),),
            (
                PropertyRow("T_A", "COL", "COL", None),
                PropertyRow("T_B", "COL", "COL", "单价"),
            ),
        )
        (collision,) = findScopeCollisions(
            plans,
            {"K1": {("T_A", "COL"), ("T_B", "COL")}},
            {("T_B", "COL"): "单价"},
        )
        assert collision.isIntroduced
        assert collision.fill_columns == ("T_A.COL",)

    def test_不回填时函数看不到存量冲突(self):
        """不传 existingAliases ⇒ 无法判定存量，返回空（调用方必须传）。"""
        plans = planAliasFill(
            (AliasEntry("T_A", "COL", "单价"),),
            (
                PropertyRow("T_A", "COL", "COL", None),
                PropertyRow("T_B", "COL", "COL", "单价"),
            ),
        )
        assert findScopeCollisions(plans, {"K1": {("T_A", "COL"), ("T_B", "COL")}}) == ()

    def test_纯存量冲突只报不拦(self):
        """两列都已有同一 alias 是历史状态，不是本批造成的 ⇒ isIntroduced=False。"""
        plans = planAliasFill(
            (AliasEntry("T_X", "COL", "别名"),),
            (PropertyRow("T_X", "COL", "COL", None),),
        )
        (collision,) = findScopeCollisions(
            plans,
            {"K1": {("T_A", "COL"), ("T_B", "COL")}},
            {("T_A", "COL"): "单价", ("T_B", "COL"): "单价"},
        )
        assert not collision.isIntroduced
        assert collision.fill_columns == ()


class TestMappingCoverage:
    def test_未覆盖的引用列必须报出(self):
        entries = (AliasEntry("T_A", "COL", "别名"),)
        excluded = (ExcludedTarget("T_B", "COL2", "join 键副本"),)
        with pytest.raises(AliasBackfillError) as exc:
            assertMappingCoverage(entries, excluded, {("T_A", "COL"), ("T_B", "COL3")})
        assert "T_B.COL3" in str(exc.value)

    def test_entries与excluded合计覆盖即通过(self):
        assertMappingCoverage(
            (AliasEntry("T_A", "COL", "别名"),),
            (ExcludedTarget("T_B", "COL2", "join 键副本"),),
            {("T_A", "COL"), ("T_B", "COL2")},
        )

    def test_excluded缺字段或重复被拒绝(self):
        with pytest.raises(AliasBackfillError):
            parseExcluded({"excluded": [{"className": "T_A"}]})
        row = {"className": "T_A", "sourceColumn": "COL"}
        with pytest.raises(AliasBackfillError):
            parseExcluded({"excluded": [row, row]})

    def test_同名类多条有效记录必须报错(self):
        rows = (
            PropertyRow("T_A", "COL", "COL", None, class_id=1),
            PropertyRow("T_A", "COL", "COL", None, class_id=2),
        )
        with pytest.raises(AliasBackfillError) as exc:
            planAliasFill((AliasEntry("T_A", "COL", "别名"),), rows)
        assert "class_id" in str(exc.value)


class TestRenderReport:
    def test_报告含动作计数与冲突行(self):
        plans = planAliasFill(
            (AliasEntry("T_A", "COL", "单价"),), (PropertyRow("T_A", "COL", "COL", None),)
        )
        collisions = findScopeCollisions(
            plans, {"K1": {("T_A", "COL")}}  # 单列：无冲突
        )
        text = renderReport(plans, collisions)
        assert "fill=1" in text
        assert "alias 冲突=0" in text
        assert "T_A.COL → 单价" in text
