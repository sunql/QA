"""QueryPlan 数据结构的单元测试。

覆盖：
- QueryPlan 不可变性（frozen dataclass）
- 嵌套结构 Aggregation/JoinSpec/SortSpec 默认值
- to_dict / from_dict 序列化往返
- 空 plan 序列化
"""

from __future__ import annotations

import pytest

from app.domain.query_plan import Aggregation, JoinSpec, QueryPlan, SortSpec, planToText


class TestQueryPlan:
    def test_is_frozen(self) -> None:
        plan = QueryPlan(target="查询", selectedClasses=("PRECEIPT",))
        with pytest.raises(Exception):
            plan.target = "修改"  # type: ignore[misc]

    def test_defaults_empty_tuples(self) -> None:
        plan = QueryPlan(target="查询")
        assert plan.selectedClasses == ()
        assert plan.selectedProperties == ()
        assert plan.conditions == ()
        assert plan.aggregations == ()
        assert plan.groupBy == ()
        assert plan.joins == ()
        assert plan.sortBy == ()
        assert plan.rowLimit is None

    def test_to_dict_roundtrip(self) -> None:
        plan = QueryPlan(
            target="各供应商收货数量",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("BPSNUM", "QTY"),
            conditions=("BPSNUM 不为空",),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
            groupBy=("BPSNUM",),
            joins=(JoinSpec(sourceClass="PRECEIPT", targetClass="PRECEIPT", columns=("BPSNUM",)),),
            sortBy=(SortSpec(property="TOTAL_QTY", direction="desc"),),
            rowLimit=100,
        )
        data = plan.to_dict()
        assert data["target"] == "各供应商收货数量"
        assert data["aggregations"][0]["function"] == "SUM"
        restored = QueryPlan.from_dict(data)
        assert restored == plan

    def test_from_dict_omits_optional_fields(self) -> None:
        plan = QueryPlan.from_dict({"target": "简单查询"})
        assert plan == QueryPlan(target="简单查询")
        assert plan.rowLimit is None

    def test_aggregation_default_alias(self) -> None:
        agg = Aggregation(function="COUNT", property="id")
        assert agg.alias is None

    def test_empty_plan_serializes(self) -> None:
        plan = QueryPlan(target="")
        data = plan.to_dict()
        assert data["selectedClasses"] == []
        assert QueryPlan.from_dict(data) == plan

    def test_from_dict_ignores_unknown_keys_in_nested_objects(self) -> None:
        plan = QueryPlan.from_dict(
            {
                "target": "查询",
                "aggregations": [
                    {"function": "SUM", "property": "QTY", "alias": "TOTAL_QTY", "extra": "oops"}
                ],
                "joins": [
                    {"sourceClass": "A", "targetClass": "B", "columns": ["ID"], "extra": 1}
                ],
                "sortBy": [{"property": "QTY", "direction": "desc", "extra": True}],
            }
        )
        assert plan.aggregations == (Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),)
        assert plan.joins == (JoinSpec(sourceClass="A", targetClass="B", columns=("ID",)),)
        assert plan.sortBy == (SortSpec(property="QTY", direction="desc"),)

    def test_from_dict_tolerates_non_list_field_values(self) -> None:
        plan = QueryPlan.from_dict(
            {"target": "查询", "selectedClasses": "PRECEIPT", "aggregations": "oops", "groupBy": 42}
        )
        assert plan.selectedClasses == ()
        assert plan.aggregations == ()
        assert plan.groupBy == ()

    def test_from_dict_tolerates_non_dict_input(self) -> None:
        plan = QueryPlan.from_dict(None)  # type: ignore[arg-type]
        assert plan == QueryPlan(target="")

    def test_from_dict_drops_non_string_entries(self) -> None:
        plan = QueryPlan.from_dict({"target": "查询", "selectedClasses": ["PRECEIPT", 7, None]})
        assert plan.selectedClasses == ("PRECEIPT",)

    def test_is_unanswerable_marks_cannot_answer_target(self) -> None:
        """target=无法回答 → 不生成 SQL、不执行查询，由流水线短路为友好回答。"""
        assert QueryPlan(target="无法回答").isUnanswerable is True
        # 模型契约：无法回答时其余字段留空；即便残留类名也不得执行 SQL
        assert QueryPlan(target="无法回答", selectedClasses=("PRECEIPT",)).isUnanswerable is True
        # 正常计划与空 target（from_dict 容错产物）均不算无法回答
        assert QueryPlan(target="各供应商的收货数量汇总").isUnanswerable is False
        assert QueryPlan(target="").isUnanswerable is False

    def test_from_dict_skips_corrupted_nested_entries(self) -> None:
        plan = QueryPlan.from_dict(
            {"target": "查询", "aggregations": [{"function": "SUM"}, {"function": "AVG", "property": "QTY"}]}
        )
        # 缺必填字段 property 的条目被跳过，其余保留
        assert plan.aggregations == (Aggregation(function="AVG", property="QTY"),)

    def test_aggregation_formula_default_none(self) -> None:
        agg = Aggregation(function="SUM", property="QTY")
        assert agg.formula is None

    def test_aggregation_formula_roundtrip(self) -> None:
        plan = QueryPlan(
            target="各物料到货数量占比",
            selectedClasses=("PRECEIPTD",),
            selectedProperties=("物料编号", "收货数量"),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="收货数量",
                    alias="占比",
                    formula="SUM(收货数量) / SUM(SUM(收货数量)) OVER ()",
                ),
            ),
            groupBy=("物料编号",),
        )
        data = plan.to_dict()
        assert data["aggregations"][0]["formula"] == "SUM(收货数量) / SUM(SUM(收货数量)) OVER ()"
        assert QueryPlan.from_dict(data) == plan

    def test_from_dict_preserves_formula_and_ignores_unknown_keys(self) -> None:
        plan = QueryPlan.from_dict(
            {
                "target": "占比",
                "aggregations": [
                    {
                        "function": "SUM",
                        "property": "收货数量",
                        "alias": "占比",
                        "formula": "SUM(收货数量) / SUM(SUM(收货数量)) OVER ()",
                        "extra": "oops",
                    }
                ],
            }
        )
        assert plan.aggregations == (
            Aggregation(
                function="SUM",
                property="收货数量",
                alias="占比",
                formula="SUM(收货数量) / SUM(SUM(收货数量)) OVER ()",
            ),
        )

    def test_plan_to_text_renders_formula_with_alias(self) -> None:
        plan = QueryPlan(
            target="占比",
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="收货数量",
                    alias="占比",
                    formula="SUM(收货数量) / SUM(SUM(收货数量)) OVER ()",
                ),
            ),
        )
        text = planToText(plan)
        assert "SUM(收货数量) / SUM(SUM(收货数量)) OVER () AS 占比" in text

    def test_plan_to_text_renders_formula_without_alias(self) -> None:
        plan = QueryPlan(
            target="占比",
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="收货数量",
                    formula="SUM(收货数量) / SUM(SUM(收货数量)) OVER ()",
                ),
            ),
        )
        text = planToText(plan)
        assert "SUM(收货数量) / SUM(SUM(收货数量)) OVER ()" in text
        # 无 alias 时不追加 AS
        assert "AS" not in text

    def test_plan_to_text_falls_back_to_function_property_without_formula(self) -> None:
        plan = QueryPlan(
            target="汇总",
            aggregations=(Aggregation(function="SUM", property="收货数量", alias="TOTAL_QTY"),),
        )
        text = planToText(plan)
        assert "SUM(收货数量) AS TOTAL_QTY" in text

    def test_plan_to_text_strips_compound_form_selected_properties(self) -> None:
        # 真实回归（2026-09-18）：LLM 偶尔把 schema 渲染格式 '业务名 (alias)' 原样
        # 抄进 property_name，state 持久化后 planToText 回灌进 prompt 会诱导下一轮
        # LLM 持续使用复合形式。planToText 渲染时必须拆括号、丢弃别名，仅保留
        # 业务名，避免污染下一轮 prompt。
        plan = QueryPlan(
            target="TOP3",
            selectedProperties=("供应商 (BPSNUM_0)", "物料编号 (ITMREF_0)"),
            groupBy=("供应商 (BPSNUM_0)",),
            partitionBy=("供应商 (BPSNUM_0)",),
        )
        text = planToText(plan)
        # 业务名保留
        assert "供应商" in text
        assert "物料编号" in text
        # ASCII 括号复合形式不再出现
        assert "供应商 (BPSNUM_0)" not in text
        assert "物料编号 (ITMREF_0)" not in text

    def test_plan_to_text_strips_compound_form_aggregation(self) -> None:
        plan = QueryPlan(
            target="汇总",
            aggregations=(Aggregation(function="SUM", property="收货数量 (QTYUOM_0)", alias="TOTAL"),),
            sortBy=(SortSpec(property="收货数量 (QTYUOM_0)", direction="desc"),),
        )
        text = planToText(plan)
        # SUM(prop) 中 prop 已是业务名
        assert "SUM(收货数量) AS TOTAL" in text
        # 排序也是纯业务名
        assert "收货数量 desc" in text
        # 复合形式剥离
        assert "收货数量 (QTYUOM_0)" not in text


class TestQueryPlanPartitionTopN:
    """2026-09-09：partitionBy/perGroupLimit ——「分别/各 X 的 Top N」逐组取前 N 槽位。"""

    def test_partition_fields_default_empty(self) -> None:
        plan = QueryPlan(target="查询")
        assert plan.partitionBy == ()
        assert plan.perGroupLimit is None

    def test_partition_fields_roundtrip(self) -> None:
        plan = QueryPlan(
            target="三个供应商各自的 Top3 物料",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("BPSNUM", "MATERIAL", "QTY"),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
            groupBy=("BPSNUM", "MATERIAL"),
            sortBy=(SortSpec(property="TOTAL_QTY", direction="desc"),),
            partitionBy=("BPSNUM",),
            perGroupLimit=3,
        )
        data = plan.to_dict()
        assert data["partitionBy"] == ["BPSNUM"]
        assert data["perGroupLimit"] == 3
        restored = QueryPlan.from_dict(data)
        assert restored == plan
        assert restored.partitionBy == ("BPSNUM",)
        assert restored.perGroupLimit == 3

    def test_from_dict_tolerates_missing_partition_fields(self) -> None:
        plan = QueryPlan.from_dict({"target": "简单查询"})
        assert plan.partitionBy == ()
        assert plan.perGroupLimit is None

    def test_from_dict_coerces_per_group_limit_to_positive_int(self) -> None:
        # 数字字符串可解析；0/负数/垃圾一律归一为 None（与 rowLimit 同口径）
        plan = QueryPlan.from_dict({"target": "x", "partitionBy": ["BPSNUM"], "perGroupLimit": "3"})
        assert plan.perGroupLimit == 3
        assert QueryPlan.from_dict({"target": "x", "perGroupLimit": 0}).perGroupLimit is None
        assert QueryPlan.from_dict({"target": "x", "perGroupLimit": -1}).perGroupLimit is None
        assert QueryPlan.from_dict({"target": "x", "perGroupLimit": "abc"}).perGroupLimit is None

    def test_plan_to_text_renders_per_group_topn(self) -> None:
        plan = QueryPlan(
            target="供应商 Top3",
            groupBy=("BPSNUM", "MATERIAL"),
            sortBy=(SortSpec(property="TOTAL_QTY", direction="desc"),),
            partitionBy=("BPSNUM",),
            perGroupLimit=3,
        )
        text = planToText(plan)
        assert "每组 Top-N" in text
        assert "按 BPSNUM 分区" in text
        assert "TOTAL_QTY desc" in text
        assert "取前 3 行" in text

    def test_plan_to_text_omits_per_group_when_unset(self) -> None:
        assert "每组 Top-N" not in planToText(QueryPlan(target="x"))
        assert "每组 Top-N" not in planToText(
            QueryPlan(target="x", partitionBy=("BPSNUM",))
        )


class TestQueryPlanInterpretation:
    def test_interpretation_default_none(self) -> None:
        assert QueryPlan(target="查询").interpretation is None

    def test_interpretation_roundtrip(self) -> None:
        plan = QueryPlan(
            target="各物料到货数量占比",
            interpretation="用户想知道每种物料的收货数量占全部到货数量的比例",
        )
        data = plan.to_dict()
        assert data["interpretation"] == "用户想知道每种物料的收货数量占全部到货数量的比例"
        assert QueryPlan.from_dict(data) == plan

    def test_from_dict_drops_non_string_interpretation(self) -> None:
        plan = QueryPlan.from_dict({"target": "查询", "interpretation": 42})
        assert plan.interpretation is None

    def test_plan_to_text_renders_understanding(self) -> None:
        plan = QueryPlan(target="占比", interpretation="理解为占比计算")
        assert "理解：理解为占比计算" in planToText(plan)

    def test_plan_to_text_omits_empty_interpretation(self) -> None:
        assert "理解" not in planToText(QueryPlan(target="占比"))


class TestJoinColumnNormalization:
    """join.columns 归一化：LLM 偶发把 join.columns 写成 "A = B" 等式字符串，
    而契约是列名数组。from_dict 出口一次性拆分，让校验与 SQL 生成自愈。"""

    def test_from_dict_splits_equation_with_spaces(self) -> None:
        plan = QueryPlan.from_dict({
            "target": "x",
            "joins": [{
                "sourceClass": "A",
                "targetClass": "B",
                "columns": ["SUPPLIER_CODE = PARTNER_CODE"],
            }],
        })
        assert plan.joins[0].columns == ("SUPPLIER_CODE", "PARTNER_CODE")

    def test_from_dict_splits_equation_without_spaces(self) -> None:
        plan = QueryPlan.from_dict({
            "target": "x",
            "joins": [{
                "sourceClass": "A",
                "targetClass": "B",
                "columns": ["SUPPLIER_CODE=PARTNER_CODE"],
            }],
        })
        assert plan.joins[0].columns == ("SUPPLIER_CODE", "PARTNER_CODE")

    def test_plain_columns_untouched(self) -> None:
        plan = QueryPlan.from_dict({
            "target": "x",
            "joins": [{
                "sourceClass": "A",
                "targetClass": "B",
                "columns": ["SUPPLIER_CODE", "PARTNER_CODE"],
            }],
        })
        assert plan.joins[0].columns == ("SUPPLIER_CODE", "PARTNER_CODE")

    def test_mixed_list_normalizes_only_equation_tokens(self) -> None:
        plan = QueryPlan.from_dict({
            "target": "x",
            "joins": [{
                "sourceClass": "A",
                "targetClass": "B",
                "columns": ["GOODS_CODE", "SUPPLIER_CODE = PARTNER_CODE"],
            }],
        })
        assert plan.joins[0].columns == ("GOODS_CODE", "SUPPLIER_CODE", "PARTNER_CODE")

    def test_unsplittable_equation_kept_for_validation(self) -> None:
        # 缺一侧 / 多个等号：无法安全拆分 → 保留原 token，交给 validatePlan 拒绝
        plan = QueryPlan.from_dict({
            "target": "x",
            "joins": [{
                "sourceClass": "A",
                "targetClass": "B",
                "columns": ["= PARTNER_CODE", "A = B = C"],
            }],
        })
        assert plan.joins[0].columns == ("= PARTNER_CODE", "A = B = C")

    def test_non_equation_with_equals_inside_name_untouched(self) -> None:
        # 极端：列名本身含 "="（现实中不存在）且无法拆出两侧非空 → 原样保留
        plan = QueryPlan.from_dict({
            "target": "x",
            "joins": [{"sourceClass": "A", "targetClass": "B", "columns": ["WEIRD="]}],
        })
        assert plan.joins[0].columns == ("WEIRD=",)

    def test_roundtrip_stays_normalized(self) -> None:
        plan = QueryPlan.from_dict({
            "target": "x",
            "joins": [{"sourceClass": "A", "targetClass": "B", "columns": ["A1 = B1"]}],
        })
        assert QueryPlan.from_dict(plan.to_dict()) == plan
