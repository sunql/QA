"""QueryPlan 校验逻辑单元测试。

validatePlan(plan, classes) 纯代码校验计划引用是否在本体 schema 中，不调 LLM。
返回问题列表；空列表 = 通过。
"""

from __future__ import annotations

from app.domain.models import OntologyClass, OntologyProperty
from app.domain.query_plan import Aggregation, JoinSpec, QueryPlan, SortSpec
from app.services.nl2sql_plan import _FORMULA_PROPERTY_HINT, _STRUCTURAL_FORMULA_HINT
from app.services.nl2sql_prompts import _ERROR_SNIPPET_LIMIT, _buildPlanUserPrompt
from app.services.nl2sql_refs import (
    _FORMULA_SQL_KEYWORDS,
    _extractFormulaProperties,
    formulaHasSqlStructure,
)
from app.services.nl2sql_service import Nl2SqlService


def _cls(name: str, props: list[tuple[str, str]], table: str | None = None) -> OntologyClass:
    """构造带属性的本体类。props: (property_name, source_column)。"""
    properties = [
        OntologyProperty(property_name=pn, source_column=sc) for pn, sc in props
    ]
    return OntologyClass(class_name=name, source_table=table or f"T_{name}", properties=properties)


def _receiptCls() -> OntologyClass:
    return _cls("PRECEIPT", [("PTHNUM", "PTHNUM_0"), ("BPSNUM", "BPSNUM_0"), ("QTY", "QTY_0")])


def _supplierCls() -> OntologyClass:
    return _cls("BPSUPPLIER", [("BPSNUM", "BPSNUM_0"), ("NAME", "NAME_0")])


def _service() -> Nl2SqlService:
    return Nl2SqlService()


class TestValidatePlan:
    def test_valid_plan_returns_empty(self) -> None:
        plan = QueryPlan(
            target="各供应商收货数量",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("BPSNUM", "QTY"),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
        )
        assert _service().validatePlan(plan, [_receiptCls()]) == []

    def test_unknown_class_reported(self) -> None:
        plan = QueryPlan(target="x", selectedClasses=("GHOST",))
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert any("GHOST" in i and "类" in i for i in issues)

    def test_property_not_in_class_reported(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("NONEXISTENT",),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert any("NONEXISTENT" in i for i in issues)

    def test_cross_class_property_hint_lists_owner_classes(self) -> None:
        # 真实回归（2026-09-16）：「供货量最大供应商」问题中 LLM 用了「供应商名称」
        # 但 selectedClasses 只选了收货明细类（该列只存在于供应商主表）。
        # 原报错「不属于选定的任何类」未说明属性属于哪些类，重试两次仍犯同错
        # → 整轮失败。报错须列出 schema 中拥有该属性的类，引导把类加入
        # selectedClasses 并经 JOIN 关联，让重试可自愈。
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("NAME",),
            groupBy=("NAME",),
        )
        issues = _service().validatePlan(plan, [_receiptCls(), _supplierCls()])
        assert any("NAME" in i and "BPSUPPLIER" in i and "selectedClasses" in i for i in issues)
        # 选中与分组两处都要带可操作指引
        assert any("分组属性" in i and "BPSUPPLIER" in i for i in issues)

    def test_property_nowhere_in_schema_says_so(self) -> None:
        # 属性在本体 schema 中完全不存在（含别名/物理列）时，须如实说明而非
        # 只说「不属于选定的任何类」，避免重试继续幻觉同一属性名。
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("NONEXISTENT",),
        )
        issues = _service().validatePlan(plan, [_receiptCls(), _supplierCls()])
        assert any("NONEXISTENT" in i and "本体 schema" in i for i in issues)

    def test_aggregation_cross_class_property_hint_lists_owner_classes(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            aggregations=(Aggregation(function="SUM", property="NAME"),),
        )
        issues = _service().validatePlan(plan, [_receiptCls(), _supplierCls()])
        assert any("聚合属性" in i and "BPSUPPLIER" in i for i in issues)

    def test_aggregation_property_must_exist(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            aggregations=(Aggregation(function="SUM", property="MISSING"),),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert any("MISSING" in i and "聚合属性" in i for i in issues)

    def test_formula_aggregation_property_alias_reports_actionable_hint(self) -> None:
        # 真实回归（2026-08-14）：公式聚合的 property 被误填成其他聚合的别名
        # （AVG_PRICE_2026），原「不属于选定的任何类」无指引。报错须说明
        # property 应填公式主要引用的真实属性、别名引用应写在 formula 内。
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            aggregations=(
                Aggregation(function="AVG", property="QTY", alias="AVG_PRICE_2025"),
                Aggregation(function="AVG", property="QTY", alias="AVG_PRICE_2026"),
                Aggregation(
                    function="AVG",
                    property="AVG_PRICE_2026",
                    alias="PRICE_DIFF",
                    formula="AVG_PRICE_2026 - AVG_PRICE_2025",
                ),
            ),
        )
        msg = next(i for i in _service().validatePlan(plan, [_receiptCls()]) if "AVG_PRICE_2026" in i)
        assert "公式聚合" in msg
        assert "property" in msg
        assert "formula" in msg

    def test_formula_with_valid_properties_passes(self) -> None:
        plan = QueryPlan(
            target="各物料收货数量占比",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="QTY",
                    alias="占比",
                    formula="SUM(QTY) / SUM(SUM(QTY)) OVER ()",
                ),
            ),
            groupBy=("BPSNUM",),
        )
        assert _service().validatePlan(plan, [_receiptCls()]) == []

    def test_formula_ignores_sql_keywords(self) -> None:
        # SUM / OVER / PARTITION / BY 等是 SQL 关键字，不得误判为属性
        plan = QueryPlan(
            target="占比",
            selectedClasses=("PRECEIPT",),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="QTY",
                    alias="占比",
                    formula="SUM(QTY) / SUM(SUM(QTY)) OVER (PARTITION BY BPSNUM)",
                ),
            ),
        )
        assert _service().validatePlan(plan, [_receiptCls()]) == []

    def test_formula_with_unknown_property_reported(self) -> None:
        plan = QueryPlan(
            target="占比",
            selectedClasses=("PRECEIPT",),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="QTY",
                    alias="占比",
                    formula="SUM(QTY) / SUM(GHOSTFIELD) OVER ()",
                ),
            ),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert any("GHOSTFIELD" in i and "公式中的属性" in i for i in issues)

    def test_formula_may_reference_sibling_aggregation_alias(self) -> None:
        # 跨年比价：差异公式引用同计划内其他聚合的别名（AVG_PRICE_2025/2026）。
        # 与排序放行聚合 alias 一致，公式引用派生别名不得误报"不属于任何类"。
        plan = QueryPlan(
            target="2025与2026采购价差异",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            aggregations=(
                Aggregation(function="AVG", property="QTY", alias="AVG_PRICE_2025"),
                Aggregation(function="AVG", property="QTY", alias="AVG_PRICE_2026"),
                Aggregation(
                    function="AVG",
                    property="QTY",
                    alias="价格差异",
                    formula="AVG_PRICE_2026 - AVG_PRICE_2025",
                ),
            ),
            sortBy=(SortSpec(property="价格差异", direction="desc"),),
        )
        assert _service().validatePlan(plan, [_receiptCls()]) == []

    def test_formula_reference_to_unknown_alias_still_rejected(self) -> None:
        # 放行兄弟聚合别名，但引用不存在的别名仍应拒绝，不得因宽松而漏报。
        plan = QueryPlan(
            target="差异",
            selectedClasses=("PRECEIPT",),
            aggregations=(
                Aggregation(function="AVG", property="QTY", alias="AVG_PRICE_2025"),
                Aggregation(
                    function="AVG",
                    property="QTY",
                    alias="价格差异",
                    formula="AVG_PRICE_9999 - AVG_PRICE_2025",
                ),
            ),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert any("AVG_PRICE_9999" in i and "公式中的属性" in i for i in issues)

    def test_formula_with_cross_class_property_reported(self) -> None:
        # NAME 只属于 BPSUPPLIER，公式在选中 PRECEIPT 时引用 NAME 应被报告
        plan = QueryPlan(
            target="占比",
            selectedClasses=("PRECEIPT",),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="QTY",
                    alias="占比",
                    formula="SUM(QTY) / SUM(NAME) OVER ()",
                ),
            ),
        )
        issues = _service().validatePlan(plan, [_receiptCls(), _supplierCls()])
        assert any("NAME" in i and "公式中的属性" in i for i in issues)

    def test_formula_string_literal_not_treated_as_property(self) -> None:
        # 字符串字面量中的词不应被当作属性校验
        plan = QueryPlan(
            target="占比",
            selectedClasses=("PRECEIPT",),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="QTY",
                    alias="占比",
                    formula="CASE WHEN QTY > 0 THEN 'GHOST' ELSE 'NONE' END",
                ),
            ),
        )
        assert _service().validatePlan(plan, [_receiptCls()]) == []

    def test_formula_to_date_function_not_treated_as_property(self) -> None:
        # 真实回归（2026-08-15）：日期对比类问题中 LLM 在 formula 内用 TO_DATE(...) 做时间
        # 筛选；TO_DATE 是 SQL 函数而非属性，不得误报「公式中的属性 TO_DATE 不属于选定的任何类」。
        receipt = _cls("PRECEIPT", [("REC_DATE", "REC_DATE_0"), ("QTY", "QTY_0")])
        plan = QueryPlan(
            target="2025 年 1-5 月采购金额",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="QTY",
                    alias="金额2025",
                    formula=(
                        "SUM(CASE WHEN TO_DATE(REC_DATE, 'YYYY-MM-DD') >= "
                        "TO_DATE('2025-01-01', 'YYYY-MM-DD') AND "
                        "TO_DATE(REC_DATE, 'YYYY-MM-DD') < TO_DATE('2025-06-01', 'YYYY-MM-DD') "
                        "THEN QTY ELSE 0 END)"
                    ),
                ),
            ),
        )
        assert _service().validatePlan(plan, [receipt]) == []

    def test_formula_function_argument_hallucination_still_reported(self) -> None:
        # 剥离函数名后，函数实参中的幻觉属性仍须被报告（不得因剥离函数名而漏报）。
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="QTY",
                    alias="金额",
                    formula="SUM(NVL(GHOSTFIELD, 0))",
                ),
            ),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert any("GHOSTFIELD" in i and "公式中的属性" in i for i in issues)

    def test_formula_window_order_by_direction_not_treated_as_property(self) -> None:
        # 真实回归（2026-09-28）：排名/累计占比类问题中 LLM 在 formula 内写
        # OVER (ORDER BY SUM(QTY) DESC)；ASC/DESC 是排序方向关键字而非属性，不得误报
        # 「公式中的属性 DESC 不属于选定的任何类」。
        # 线上表现：问「每个供应商采购金额占前 5 名合计的比例」稳定 400（plan 校验不过 →
        # 重试同错 → 整轮失败）；而带空窗口 OVER () 的占比问题恰好绕过，故只在需要排序的
        # 占比/排名问题暴露。根因是 nl2sql_refs._FORMULA_SQL_KEYWORDS 缺 ASC/DESC
        # （formula_parser._SQL_KEYWORDS 有），两套关键字集合漂移。
        for direction in ("DESC", "ASC"):
            plan = QueryPlan(
                target="每个供应商采购金额占前 5 名合计的比例",
                selectedClasses=("PRECEIPT",),
                selectedProperties=("QTY",),
                aggregations=(
                    Aggregation(
                        function="SUM",
                        property="QTY",
                        alias="占比",
                        formula=(
                            "SUM(QTY) / SUM(SUM(QTY)) OVER "
                            f"(ORDER BY SUM(QTY) {direction})"
                        ),
                    ),
                ),
            )
            issues = _service().validatePlan(plan, [_receiptCls()])
            assert issues == [], f"{direction} 被误判为属性: {issues}"

    def test_join_references_existing_class(self) -> None:
        plan = QueryPlan(
            target="x",
            joins=(JoinSpec(sourceClass="PRECEIPT", targetClass="BPSUPPLIER", columns=("BPSNUM",)),),
        )
        issues = _service().validatePlan(plan, [_receiptCls(), _supplierCls()])
        assert issues == []

    def test_join_unknown_target_class_reported(self) -> None:
        plan = QueryPlan(
            target="x",
            joins=(JoinSpec(sourceClass="PRECEIPT", targetClass="GHOST", columns=("BPSNUM",)),),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert any("GHOST" in i for i in issues)

    def test_groupby_property_must_exist(self) -> None:
        plan = QueryPlan(target="x", groupBy=("NOPE",))
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert any("NOPE" in i for i in issues)

    def test_groupby_accepts_table_qualified_column(self) -> None:
        # 真实回归（2026-08-15）：LLM 在 groupBy 写 `PORDERQ.ITMREF_0`（表.物理列）
        # 完整限定名，只按未限定名校验会误杀正确计划。校验须容纳 `表.列` 形式。
        cls = _cls("PurchaseOrderDetail", [("物料编号", "ITMREF_0"), ("订单日期", "ORDDAT_0")], table="PORDERQ")
        plan = QueryPlan(
            target="2025 年采购金额最多的 20 种物料",
            selectedClasses=("PurchaseOrderDetail",),
            aggregations=(Aggregation(function="SUM", property="物料编号", alias="TOTAL_AMT"),),
            groupBy=("PORDERQ.ITMREF_0",),
        )
        assert _service().validatePlan(plan, [cls]) == []

    def test_groupby_accepts_class_qualified_column(self) -> None:
        # 类名限定（PurchaseOrderDetail.ITMREF_0）同样应通过。
        cls = _cls("PurchaseOrderDetail", [("物料编号", "ITMREF_0")], table="PORDERQ")
        plan = QueryPlan(
            target="x",
            selectedClasses=("PurchaseOrderDetail",),
            groupBy=("PurchaseOrderDetail.ITMREF_0",),
        )
        assert _service().validatePlan(plan, [cls]) == []

    def test_groupby_table_qualified_unknown_still_rejected(self) -> None:
        # 放宽后仍须拒绝真正不存在的表.列，不得因宽松而漏报。
        cls = _cls("PurchaseOrderDetail", [("物料编号", "ITMREF_0")], table="PORDERQ")
        plan = QueryPlan(target="x", groupBy=("PORDERQ.GHOST_0",))
        issues = _service().validatePlan(plan, [cls])
        assert any("GHOST_0" in i for i in issues)

    def test_groupby_bare_time_bucket_token_reports_actionable_hint(self) -> None:
        # 真实回归（2026-08-15）：LLM 把「月份」这类时间粒度词直接写进 groupBy，
        # 而非日期属性。原报错「分组属性 月份 不属于选定的任何类」无可操作指引，
        # 重试仍生成同形计划。修复：报错须引导改用日期属性（如 订单日期）。
        cls = OntologyClass(
            class_name="PurchaseOrderDetail",
            source_table="PORDERQ",
            properties=[
                OntologyProperty(property_name="物料编号", source_column="ITMREF_0"),
                OntologyProperty(property_name="订单日期", source_column="ORDDAT_0", data_type="DATETIME"),
            ],
        )
        plan = QueryPlan(
            target="2025 年采购数量每月变化趋势",
            selectedClasses=("PurchaseOrderDetail",),
            aggregations=(Aggregation(function="SUM", property="采购数量", alias="TOTAL_QTY"),),
            groupBy=("月份",),
        )
        msg = next(i for i in _service().validatePlan(plan, [cls]) if "月份" in i)
        assert "时间粒度" in msg
        assert "订单日期" in msg
        assert "TO_CHAR" in msg

    def test_groupby_bare_time_bucket_without_date_property_still_hinted(self) -> None:
        # 选中类无日期属性时，提示仍应引导改用日期属性，不得因无候选而吞掉提示。
        plan = QueryPlan(target="x", selectedClasses=("PRECEIPT",), groupBy=("月份",))
        msg = next(i for i in _service().validatePlan(plan, [_receiptCls()]) if "月份" in i)
        assert "时间粒度" in msg
        assert "日期属性" in msg

    def test_empty_selection_valid(self) -> None:
        plan = QueryPlan(target="x")
        assert _service().validatePlan(plan, [_receiptCls()]) == []

    def test_cross_class_property_rejected(self) -> None:
        # NAME 只属于 BPSUPPLIER，选择 PRECEIPT 类却用 NAME 属性 → 报错
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("NAME",),
        )
        issues = _service().validatePlan(plan, [_receiptCls(), _supplierCls()])
        assert any("NAME" in i for i in issues)

    def test_join_column_must_exist(self) -> None:
        plan = QueryPlan(
            target="x",
            joins=(JoinSpec(sourceClass="PRECEIPT", targetClass="BPSUPPLIER", columns=("MAGIC",)),),
        )
        issues = _service().validatePlan(plan, [_receiptCls(), _supplierCls()])
        assert any("MAGIC" in i for i in issues)

    def test_join_column_equation_token_reports_array_hint(self) -> None:
        # LLM 偶发把 join.columns 写成 "A = B" 等式（2026-09-18 真实回归）：
        # 重试反馈必须指出 join.columns 是列名数组、给出正确写法。
        plan = QueryPlan(
            target="x",
            joins=(JoinSpec(sourceClass="PRECEIPT", targetClass="BPSUPPLIER", columns=("MAGIC",)),),
        )
        issues = _service().validatePlan(plan, [_receiptCls(), _supplierCls()])
        joinIssue = next(i for i in issues if "MAGIC" in i)
        assert "join.columns" in joinIssue
        assert "数组" in joinIssue
        assert "不要写" in joinIssue

    def test_valid_cross_class_join_column_passes(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT", "BPSUPPLIER"),
            joins=(JoinSpec(sourceClass="PRECEIPT", targetClass="BPSUPPLIER", columns=("BPSNUM",)),),
        )
        assert _service().validatePlan(plan, [_receiptCls(), _supplierCls()]) == []

    def test_join_column_accepts_source_column(self) -> None:
        # schema 文本以 `属性名: 类型 (column=物理列)` 呈现，LLM 常写物理列名
        # （如 BPSNUM_0）而非业务名 BPSNUM；校验集合须同时容纳两者，不得误报。
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT", "BPSUPPLIER"),
            joins=(JoinSpec(sourceClass="PRECEIPT", targetClass="BPSUPPLIER", columns=("BPSNUM_0",)),),
        )
        assert _service().validatePlan(plan, [_receiptCls(), _supplierCls()]) == []

    def test_selected_properties_accept_source_column(self) -> None:
        # LLM 在 selectedProperties 写物理列名（QTY_0）也应通过，而非仅业务名 QTY。
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("BPSNUM_0", "QTY_0"),
        )
        assert _service().validatePlan(plan, [_receiptCls()]) == []

    def test_source_column_still_rejects_unknown(self) -> None:
        # 放宽后仍须拒绝真正不存在的列，不得因宽松而漏报。
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            joins=(JoinSpec(sourceClass="PRECEIPT", targetClass="BPSUPPLIER", columns=("MAGIC_0",)),),
        )
        issues = _service().validatePlan(plan, [_receiptCls(), _supplierCls()])
        assert any("MAGIC_0" in i for i in issues)

    def test_selected_property_accepts_business_alias(self) -> None:
        # LLM 可能写本体为属性配置的近义词别名（如"到货行号"→"行号"），
        # business_aliases 是 2-2 字段，校验集合须容纳，否则正确计划被打回。
        cls = OntologyClass(
            class_name="YPRECEIPTD",
            source_table="T_YPRECEIPTD",
            properties=[
                OntologyProperty(
                    property_name="行号",
                    source_column="YPTDLIN_0",
                    business_aliases=["到货行号", "到货行"],
                ),
            ],
        )
        plan = QueryPlan(
            target="x",
            selectedClasses=("YPRECEIPTD",),
            selectedProperties=("到货行号",),
        )
        assert _service().validatePlan(plan, [cls]) == []

    def test_join_column_accepts_business_alias(self) -> None:
        # JOIN 列写别名也应通过（sourceProps/targetProps 均含 business_aliases）。
        cls = OntologyClass(
            class_name="YPRECEIPTD",
            source_table="T_YPRECEIPTD",
            properties=[
                OntologyProperty(
                    property_name="行号",
                    source_column="YPTDLIN_0",
                    business_aliases=["到货行号"],
                ),
            ],
        )
        plan = QueryPlan(
            target="x",
            joins=(JoinSpec(sourceClass="YPRECEIPTD", targetClass="YPRECEIPTD", columns=("到货行号",)),),
        )
        assert _service().validatePlan(plan, [cls]) == []

    def test_sort_by_aggregate_alias_passes(self) -> None:
        # ORDER BY 聚合别名（SUM(...) AS TOTAL_QTY）在 ANSI SQL 合法，
        # 排序属性须允许聚合 alias，不得误报"不属于任何类"。
        plan = QueryPlan(
            target="采购量最大的物料",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
            groupBy=("PTHNUM",),
            sortBy=(SortSpec(property="TOTAL_QTY", direction="desc"),),
        )
        assert _service().validatePlan(plan, [_receiptCls()]) == []

    def test_sort_by_unknown_still_rejected(self) -> None:
        # 非聚合别名、非类属性的排序引用仍应拒绝，不得因宽松而漏报。
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
            sortBy=(SortSpec(property="GHOST_TOTAL", direction="desc"),),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert any("GHOST_TOTAL" in i for i in issues)

    def test_sort_by_undeclared_derived_alias_reports_actionable_hint(self) -> None:
        # 真实回归（2026-08-14）：LLM 把派生别名（PRICE_DIFF/价格差异）只写进 sortBy，
        # 未在聚合中声明公式聚合。原错误「排序属性 X 不属于选定的任何类」不可操作，
        # 重试仍生成同形计划（相关类子集 + few-shot 输入下主模型与备选模型均失败）。
        # 修复：报错须引导把派生指标声明为公式聚合，并给出可用的聚合别名，供重试自愈。
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            aggregations=(
                Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),
                Aggregation(function="AVG", property="QTY", alias="AVG_PRICE_2025"),
                Aggregation(function="AVG", property="QTY", alias="AVG_PRICE_2026"),
            ),
            sortBy=(SortSpec(property="价格差异", direction="desc"),),
        )
        msg = next(i for i in _service().validatePlan(plan, [_receiptCls()]) if "价格差异" in i)
        # 引导性：提示派生指标须声明为公式聚合，并给出可用的聚合别名
        assert "公式聚合" in msg
        assert "聚合别名" in msg
        assert "AVG_PRICE_2025" in msg
        assert "AVG_PRICE_2026" in msg

    # ---- 派生指标（占比/比率/百分比）formula 必填硬约束（2026-08-17 真实回归）-----

    def test_alias_zhanshi_without_formula_reported_with_window_function_hint(self) -> None:
        """真实回归（2026-08-17）：用户问题 'top10 采购物料的占比' 时 LLM 倾向生成
        alias='占比' 但无 formula 的聚合，SQL 不会算百分比，占比沦为列别名，
        validatePlan 重试耗尽后整步被标 '无法回答'，Step 3 因依赖被卡。
        修复：alias 命中派生指标关键词时 formula 必填，报错须可操作，
        引导 LLM 用窗口函数 SUM(x)/SUM(SUM(x)) OVER ()。
        """
        plan = QueryPlan(
            target="4 月份主要 top10 采购物料的占比",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("BPSNUM", "QTY"),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="占比"),),
            groupBy=("BPSNUM",),
            sortBy=(SortSpec(property="占比", direction="desc"),),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        msg = next(i for i in issues if "占比" in i)
        # 必须可操作：提到窗口函数、property、占位提示
        assert "窗口函数" in msg or "OVER" in msg
        assert "property" in msg or "属性" in msg

    def test_alias_zhanshi_with_formula_passes(self) -> None:
        """守约：alias='占比' + 合法 formula 时必须仍通过（与 test_formula_with_valid_properties_passes 一致）。"""
        plan = QueryPlan(
            target="各物料收货数量占比",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="QTY",
                    alias="占比",
                    formula="SUM(QTY) / SUM(SUM(QTY)) OVER ()",
                ),
            ),
            groupBy=("BPSNUM",),
        )
        assert _service().validatePlan(plan, [_receiptCls()]) == []

    def test_alias_ratio_en_without_formula_reported(self) -> None:
        """英文 ratio/percent 也命中关键词（大小写不敏感）。"""
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="RATIO"),),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert any("RATIO" in i and ("公式" in i or "派生" in i or "窗口" in i) for i in issues)

    def test_alias_ordinary_total_qty_not_flagged(self) -> None:
        """守约：普通聚合 alias='TOTAL_QTY' 不含派生指标关键词，必须不被新规则拦下。"""
        plan = QueryPlan(
            target="收货总数量",
            selectedClasses=("PRECEIPT",),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        # 不应出现任何针对 TOTAL_QTY 的报错
        assert not any("TOTAL_QTY" in i for i in issues)
        assert issues == []


class TestValidatePlanPerGroupTopN:
    """2026-09-09：partitionBy/perGroupLimit ——「分别/各/每个 X 的 Top N」逐组取前 N 校验。

    语义红线：每组 Top-N 是「分区内排名取前 N」，不是全局 N×组数。校验强制：
    分区属性真实存在且是分组维、partitionBy 与 perGroupLimit 成对、组内有排序、
    rowLimit 必须为 null（不得与全局行数叠加产生「前 N×组数」坍缩）。
    """

    @staticmethod
    def _matReceiptCls() -> OntologyClass:
        return _cls(
            "PRECEIPT", [("BPSNUM", "BPSNUM_0"), ("MATERIAL", "MAT_0"), ("QTY", "QTY_0")]
        )

    def _valid(self) -> QueryPlan:
        return QueryPlan(
            target="三个供应商各自的 Top3 物料",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("BPSNUM", "MATERIAL", "QTY"),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
            groupBy=("BPSNUM", "MATERIAL"),
            sortBy=(SortSpec(property="TOTAL_QTY", direction="desc"),),
            partitionBy=("BPSNUM",),
            perGroupLimit=3,
        )

    def test_valid_per_group_topn_passes(self) -> None:
        assert _service().validatePlan(self._valid(), [self._matReceiptCls()]) == []

    def test_partition_property_must_exist(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("BPSNUM", "MATERIAL", "QTY"),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
            groupBy=("BPSNUM", "MATERIAL"),
            sortBy=(SortSpec(property="TOTAL_QTY", direction="desc"),),
            partitionBy=("GHOST",),
            perGroupLimit=3,
        )
        issues = _service().validatePlan(plan, [self._matReceiptCls()])
        assert any("GHOST" in i and "分区" in i for i in issues)

    def test_partition_property_must_be_in_group_by(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("BPSNUM", "MATERIAL", "QTY"),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
            groupBy=("MATERIAL",),  # 分区维 BPSNUM 不在分组里 → 无意义
            sortBy=(SortSpec(property="TOTAL_QTY", direction="desc"),),
            partitionBy=("BPSNUM",),
            perGroupLimit=3,
        )
        issues = _service().validatePlan(plan, [self._matReceiptCls()])
        assert any("BPSNUM" in i and "groupBy" in i for i in issues)

    def test_partition_requires_per_group_limit(self) -> None:
        plan = self._valid()
        plan = QueryPlan(
            target=plan.target,
            selectedClasses=plan.selectedClasses,
            selectedProperties=plan.selectedProperties,
            aggregations=plan.aggregations,
            groupBy=plan.groupBy,
            sortBy=plan.sortBy,
            partitionBy=("BPSNUM",),
            perGroupLimit=None,
        )
        issues = _service().validatePlan(plan, [self._matReceiptCls()])
        assert any("perGroupLimit" in i for i in issues)

    def test_per_group_limit_requires_partition(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
            perGroupLimit=3,
            partitionBy=(),
        )
        issues = _service().validatePlan(plan, [self._matReceiptCls()])
        assert any("partitionBy" in i and "perGroupLimit" in i for i in issues)

    def test_per_group_limit_conflicts_with_global_row_limit(self) -> None:
        # 模型此前正是把「3 供应商 × 每供应商 3」折成 rowLimit=9 —— 必须被拦下。
        plan = self._valid()
        plan = QueryPlan(
            target=plan.target,
            selectedClasses=plan.selectedClasses,
            selectedProperties=plan.selectedProperties,
            aggregations=plan.aggregations,
            groupBy=plan.groupBy,
            sortBy=plan.sortBy,
            partitionBy=("BPSNUM",),
            perGroupLimit=3,
            rowLimit=9,
        )
        issues = _service().validatePlan(plan, [self._matReceiptCls()])
        assert any("rowLimit" in i and "9" in i for i in issues)
        assert any("null" in i for i in issues)

    def test_per_group_topn_requires_sort_by(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("BPSNUM", "MATERIAL", "QTY"),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
            groupBy=("BPSNUM", "MATERIAL"),
            partitionBy=("BPSNUM",),
            perGroupLimit=3,
        )
        issues = _service().validatePlan(plan, [self._matReceiptCls()])
        assert any("sortBy" in i for i in issues)


class TestSqlKeywordSetsStayInSync:
    """两套 SQL 关键字集合的一致性守卫（2026-09-28 线上 400 回归）。

    formula_parser._SQL_KEYWORDS 用于「不把关键字当裸列名」，
    nl2sql_refs._FORMULA_SQL_KEYWORDS 用于「不把关键字当公式属性」。
    二者服务于同一件事（都是「公式里出现的 SQL 词不是属性」），一旦漂移就会出现
    「同一 token 在一处被当关键字、在另一处被当属性」——ASC/DESC 缺失导致排名占比
    问题整轮 400 即由此而来。
    """

    def test_formula_parser_keywords_are_covered_by_refs_keywords(self) -> None:
        from app.services.formula_parser import _SQL_KEYWORDS
        from app.services.nl2sql_refs import _FORMULA_SQL_KEYWORDS

        missing = sorted(_SQL_KEYWORDS - _FORMULA_SQL_KEYWORDS)
        assert missing == [], (
            f"nl2sql_refs._FORMULA_SQL_KEYWORDS 缺少 {missing}；"
            f"这些 token 会被 _extractFormulaProperties 当作属性上报"
        )

    def test_only_is_covered(self) -> None:
        """ONLY 来自 FETCH FIRST n ROWS ONLY，是 SQL 词不是属性。

        与 2026-09-28 补的 ASC/DESC 同类漏项（纵深防御，非本次承重修复）。
        """
        assert "ONLY" in _FORMULA_SQL_KEYWORDS
        assert _extractFormulaProperties("FETCH FIRST 3 ROWS ONLY") == set()


class TestFormulaStructurePredicate:
    """formulaHasSqlStructure：公式是否含 SQL 语句结构（独立词 FROM/JOIN）。

    判据刻意**不看首词** —— 线上错误文本只列出被误报的 token，无法区分
    「整条 SELECT」与「表达式里嵌子查询」（两者首词不同但都含 FROM）。
    """

    def test_statement_shapes_are_structural(self) -> None:
        assert formulaHasSqlStructure("SELECT SUM(x) FROM T") is True
        assert formulaHasSqlStructure("select a from t") is True
        assert formulaHasSqlStructure("SELECT a JOIN b ON a.id = b.id") is True

    def test_subquery_expression_is_structural(self) -> None:
        """首词是 SUM 不是 SELECT，仍含 FROM ⇒ 也是语句结构。"""
        assert formulaHasSqlStructure("SUM(a) / (SELECT SUM(b) FROM T)") is True

    def test_expression_shapes_are_not_structural(self) -> None:
        assert formulaHasSqlStructure("SUM(QTY) / SUM(SUM(QTY)) OVER ()") is False
        assert formulaHasSqlStructure("AVG_PRICE_2026 - AVG_PRICE_2025") is False

    def test_from_inside_string_literal_is_not_structural(self) -> None:
        """字面量里的 FROM 不是语句结构（判前先剥字面量）—— 防误判。"""
        assert formulaHasSqlStructure("CASE WHEN X = 'FROM' THEN 1 ELSE 0 END") is False

    def test_word_boundary(self) -> None:
        """FROMX 不是 FROM。"""
        assert formulaHasSqlStructure("SUM(FROMX)") is False

    def test_from_inside_function_is_not_structural(self) -> None:
        """EXTRACT/TRIM 里的 FROM 是**函数实参分隔符**，不是语句子句 —— 防误判。

        code review HIGH（2026-10-01 已复现）：只看 `\\b(FROM|JOIN)\\b` 会把
        `SUM(CASE WHEN EXTRACT(MONTH FROM 到货日期) = 5 THEN QTY ELSE 0 END)/...`
        判成整条语句，而该公式的 token 全是真实属性（改前**能通过校验**），
        于是从「能过」变成「被拒」，且提示语内容不实（说它是整条 SQL 语句）。
        真语句必然同时含 SELECT，故判据要求两者同时出现。
        """
        assert formulaHasSqlStructure("SUM(EXTRACT(MONTH FROM D))") is False
        assert formulaHasSqlStructure("SUM(TRIM(BOTH ' ' FROM D))") is False
        # 双向：真语句仍须判 True
        assert formulaHasSqlStructure("(SELECT SUM(x) FROM T)") is True


class TestValidatePlanFormulaShape:
    """语句形态 formula 的校验口径（2026-10-01 线上回归）。

    LLM 把整条 SQL 语句放进 Aggregation.formula → 内部的 schema 名（THBI）、
    表名（DWD_GOODS_RECEIPT_DTL / DIM_IMATERIAL）、表别名（d2 / m2）、
    ONLY（FETCH FIRST 3 ROWS ONLY）全被 _extractFormulaProperties 当属性逐条上报：

        公式中的属性 THBI 不属于选定的任何类
        公式中的属性 DWD_GOODS_RECEIPT_DTL 不属于选定的任何类
        ...（共 6 条）

    重试反馈无指向 → 模型原样重犯 → maxPlanAttempts 耗尽 → 整轮失败。

    （用户侧报了 6 条：ONLY / m2 / DWD_GOODS_RECEIPT_DTL / DIM_IMATERIAL / d2 / THBI；
    本夹具公式另含 3 个真实列名 RCV_QTY / ITEM_CODE / ITMREF_0，故实测为 9 条。
    两者差集正好是那 3 个真实列名 —— 用户那边它们在 owned 里所以未被上报。）

    修法：含 FROM/JOIN 的公式不做属性存在性校验，改为**一条可操作引导**。
    不豁免而是拒绝，因为 Aggregation.formula 没有确定性渲染器
    （planToText/_aggText 只拼 "{formula} AS {alias}"），豁免会把早期响亮的
    失败换成 SQL 阶段晚期安静的失败。
    """

    _STATEMENT = (
        "SELECT SUM(d2.RCV_QTY) FROM THBI.DWD_GOODS_RECEIPT_DTL d2 "
        "JOIN THBI.DIM_IMATERIAL m2 ON d2.ITEM_CODE = m2.ITMREF_0 "
        "FETCH FIRST 3 ROWS ONLY"
    )

    def _plan(self, formula: str) -> QueryPlan:
        return QueryPlan(
            target="前三家供应商供货量占比",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            aggregations=(
                Aggregation(
                    function="SUM", property="QTY", alias="占比", formula=formula
                ),
            ),
        )

    def test_statement_formula_reports_single_actionable_issue(self) -> None:
        """判别器：修前实测返回 9 条「公式中的属性 … 不属于选定的任何类」。"""
        issues = _service().validatePlan(self._plan(self._STATEMENT), [_receiptCls()])
        assert len(issues) == 1, issues
        assert "公式中的属性" not in issues[0]
        assert "窗口函数" in issues[0]
        assert "CTE" in issues[0]

    def test_subquery_expression_also_reports_structural_issue(self) -> None:
        """另一种形态（表达式里嵌子查询）走同一条分支。"""
        formula = "SUM(QTY) / (SELECT SUM(QTY) FROM THBI.DWD_GOODS_RECEIPT_DTL)"
        issues = _service().validatePlan(self._plan(formula), [_receiptCls()])
        assert len(issues) == 1, issues
        assert "公式中的属性" not in issues[0]
        assert "窗口函数" in issues[0]

    def test_pure_expression_hallucination_still_reported(self) -> None:
        """反向守卫：不含 FROM/JOIN 的纯表达式，幻觉属性仍须被拦。

        防止「把属性存在性校验整条废掉」这种过度修复。
        """
        formula = "SUM(NONEXISTENT) / SUM(SUM(NONEXISTENT)) OVER ()"
        issues = _service().validatePlan(self._plan(formula), [_receiptCls()])
        assert any("公式中的属性" in i and "NONEXISTENT" in i for i in issues), issues

    def test_from_inside_string_literal_is_not_structural(self) -> None:
        """误判守卫：字面量里的 FROM 不算语句结构，公式照常按属性校验且通过。"""
        formula = (
            "SUM(CASE WHEN BPSNUM = 'FROM' THEN QTY ELSE 0 END) "
            "/ SUM(SUM(QTY)) OVER ()"
        )
        assert _service().validatePlan(self._plan(formula), [_receiptCls()]) == []

    def test_extract_function_formula_not_misjudged(self) -> None:
        """回归守卫（code review HIGH）：含 `EXTRACT(... FROM ...)` 的合法公式必须通过。

        该公式提取出的 token 是 {QTY, 到货日期}，全是真实属性 ⇒ 只看 FROM/JOIN 的
        版本会把它误拒；正确判据（要求同时含 SELECT）下返回 []。
        """
        receipt = _cls(
            "PRECEIPT",
            [
                ("PTHNUM", "PTHNUM_0"),
                ("BPSNUM", "BPSNUM_0"),
                ("QTY", "QTY_0"),
                ("到货日期", "RCV_DATE_0"),
            ],
        )
        formula = (
            "SUM(CASE WHEN EXTRACT(MONTH FROM 到货日期) = 5 THEN QTY ELSE 0 END) "
            "/ SUM(SUM(QTY)) OVER ()"
        )
        assert _service().validatePlan(self._plan(formula), [receipt]) == []

    def test_hint_survives_snippet_truncation_alongside_other_issues(self) -> None:
        """提示语必须活过 200 字符的**尾部**截断 —— 排在别的 issue 之后就会被砍掉。

        code review MEDIUM：`_buildPlanUserPrompt` 把 issues 用「；」拼起来后截前
        `_ERROR_SNIPPET_LIMIT=200` 字符，而公式分支原本排在 selectedProperties 之后
        ⇒ 有前置 issue 时引导整个消失，重试又变回无指向 —— 正是本次要修的病。
        故实现把提示 insert 到首位并去重。
        """
        plan = QueryPlan(
            target="前三家供应商供货量占比",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("NONEXISTENT", "QTY"),  # 制造一条排在公式之前的 issue
            aggregations=(
                Aggregation(
                    function="SUM", property="QTY", alias="占比", formula=self._STATEMENT
                ),
            ),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert len(issues) >= 2, issues
        # 用生产构造器断言，不重写拼接逻辑（否则会与实现漂移）
        prompt = _buildPlanUserPrompt("前三家供应商供货量占比", issues)
        assert _STRUCTURAL_FORMULA_HINT in prompt, prompt[-300:]

    def test_cte_formula_still_exempt(self) -> None:
        """CTE 形态仍走豁免（不被新的语句结构分支拦截）。"""
        formula = (
            "WITH top3 AS (SELECT BPSNUM, SUM(QTY) AS qty FROM T_PRECEIPT "
            "GROUP BY BPSNUM) SELECT SUM(qty) AS 占比 FROM top3"
        )
        assert _service().validatePlan(self._plan(formula), [_receiptCls()]) == []


class TestPureExpressionFormulaHint:
    """纯表达式 formula 的幻觉/占位符报错必须带可操作方向（2026-10-01 真机回归）。

    真机实测（`docker logs qa-backend` 的「NL2SQL 计划校验」日志）：问「5月份供货量最多的
    三家供应商所供货物总量占5月份总供货量的比例是多少」时，模型第一次写

        SUM(CASE WHEN SUPPLIER_CODE IN (TOP3) THEN RCV_QTY_PUU ELSE 0 END) / SUM(RCV_QTY_PUU)

    —— 用占位符 `TOP3` 标出「这里要放前三家」。该公式不含 SELECT/FROM，走纯表达式分支，
    而该分支是 validatePlan 里**唯一**不拼任何可操作提示的属性校验分支，只回一句
    `公式中的属性 TOP3 不属于选定的任何类`。模型据此把 `TOP3` **就地展开成子查询**
    （第二轮输出是第一轮的精确回应 —— 表达式结构完全不变，只把占位符换成真实 SQL），
    方向错误 → maxPlanAttempts 耗尽 → 整轮失败。

    修法：该分支补 `_FORMULA_PROPERTY_HINT`（禁用占位符/子查询 + Top-N 用 CTE 形式），
    置 issues 首位并去重 —— 与语句结构分支同一教训（`_ERROR_SNIPPET_LIMIT=200` 从尾部截断）。
    """

    _PLACEHOLDER = "SUM(CASE WHEN BPSNUM IN (TOP3) THEN QTY ELSE 0 END) / SUM(QTY)"

    def _plan(self, formula: str) -> QueryPlan:
        return QueryPlan(
            target="前三家供应商供货量占比",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            aggregations=(
                Aggregation(function="SUM", property="QTY", alias="占比", formula=formula),
            ),
        )

    def test_placeholder_reports_actionable_hint(self) -> None:
        """判别器：修前该支只有裸句，没有任何方向。"""
        issues = _service().validatePlan(self._plan(self._PLACEHOLDER), [_receiptCls()])
        # 原有口径保留：逐 token 报幻觉属性 + 归属提示
        assert any("公式中的属性" in i and "TOP3" in i for i in issues), issues
        # 新增：可操作方向（禁用占位符/子查询 + Top-N 用 CTE）
        assert _FORMULA_PROPERTY_HINT in issues, issues
        # 位次：必须排在属性报错行**之前**（_buildPlanUserPrompt 从**尾部**截断，
        # 排在后面会被砍掉）。用 index 比较而非 `issues[0] ==`：后者把「必须是全局
        # 第 0 位」这个实现细节写死，而结构性公式与纯表达式公式同处一个计划时，
        # 两个 hint 会各占 0/1 位（code review LOW）。
        firstPropRow = min(i for i, x in enumerate(issues) if "公式中的属性" in x)
        assert issues.index(_FORMULA_PROPERTY_HINT) < firstPropRow, issues

    def test_hint_survives_snippet_truncation(self) -> None:
        """提示语必须活过 200 字符尾部截断（前置 issue 会把它挤走）。"""
        plan = QueryPlan(
            target="前三家供应商供货量占比",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("NONEXISTENT", "QTY"),  # 制造一条排在公式之前的 issue
            aggregations=(
                Aggregation(
                    function="SUM", property="QTY", alias="占比", formula=self._PLACEHOLDER
                ),
            ),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert len(issues) >= 3, issues
        # 用生产构造器断言，不重写拼接逻辑（否则会与实现漂移）
        prompt = _buildPlanUserPrompt("前三家供应商供货量占比", issues)
        assert _FORMULA_PROPERTY_HINT in prompt, prompt[-300:]

    def test_hint_survives_even_when_issues_overflow_budget(self) -> None:
        """引导在任何组合下都必须完整 —— 置首位买到的就是这个性质。

        实测（非估算）：两个未知属性 +「有归属」支的长归属提示，issues 拼接达
        **280 字符，超过** `_ERROR_SNIPPET_LIMIT=200`。此时引导仍完整，被截断的只是
        排在它后面的属性报错行尾部。故本测试断言的是「引导存活」，**不是**「总和 ≤ 200」
        —— 后者在边界场景做不到，宣称做到了才是不实。
        """
        receipt = _cls("PRECEIPT", [("QTY", "QTY_0")])
        # NAME 同时属于 5 个类 ⇒ _propertyOwnerHint 走「有归属」支且列满类名（输出最长）
        others = [
            _cls(f"CLS{i}", [("X", "X_0"), ("NAME", f"NAME_{i}")]) for i in range(1, 6)
        ]
        plan = QueryPlan(
            target="前三家供应商供货量占比",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="QTY",
                    alias="占比",
                    formula="SUM(CASE WHEN NAME IN (TOP3) THEN QTY ELSE 0 END) / SUM(QTY)",
                ),
            ),
        )
        issues = _service().validatePlan(plan, [receipt] + others)
        joined = "；".join(issues)
        # 前提守卫（双向）：夹具必须真的超预算，否则本测试没在测它想测的东西
        assert len(joined) > _ERROR_SNIPPET_LIMIT, (
            f"夹具未触发超预算（{len(joined)} ≤ {_ERROR_SNIPPET_LIMIT}），"
            f"本测试失去意义，请调整夹具"
        )
        prompt = _buildPlanUserPrompt("前三家供应商供货量占比", issues)
        assert _FORMULA_PROPERTY_HINT in prompt, prompt[-300:]

    def test_cross_class_reference_does_not_get_placeholder_hint(self) -> None:
        """误伤守卫（code review MEDIUM）：跨类引用是**真实列**，不是占位符。

        `NAME` 属于 BPSUPPLIER 而 selectedClasses 只选了 PRECEIPT —— 模型用的是真列，
        该收到「该属性属于类 BPSUPPLIER，请把对应类加入 selectedClasses」，
        而不是「不得用占位符或子查询」。后者是**错误方向**，比没方向更糟。
        """
        receipt = _cls("PRECEIPT", [("QTY", "QTY_0")])
        supplier = _cls("BPSUPPLIER", [("BPSNUM", "BPSNUM_0"), ("NAME", "NAME_0")])
        plan = QueryPlan(
            target="各供应商收货量占比",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="QTY",
                    alias="占比",
                    formula="SUM(QTY) / SUM(NAME) OVER ()",
                ),
            ),
        )
        issues = _service().validatePlan(plan, [receipt, supplier])
        assert any("公式中的属性 NAME" in i and "属于类 BPSUPPLIER" in i for i in issues), issues
        assert _FORMULA_PROPERTY_HINT not in issues, issues

    def test_shape_hallucination_gets_placeholder_hint(self) -> None:
        """对照（双向守卫）：全 schema 都不存在的 token 才配得上占位符引导。"""
        plan = QueryPlan(
            target="各供应商收货量占比",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="QTY",
                    alias="占比",
                    formula="SUM(GHOSTFIELD) / SUM(QTY) OVER ()",
                ),
            ),
        )
        issues = _service().validatePlan(plan, [_receiptCls()])
        assert _FORMULA_PROPERTY_HINT in issues, issues

    def test_legal_pure_expression_not_disturbed(self) -> None:
        """反向守卫：不含幻觉/占位符的纯表达式不得被新提示打扰。"""
        assert _service().validatePlan(
            self._plan("SUM(QTY) / SUM(SUM(QTY)) OVER ()"), [_receiptCls()]
        ) == []

    def test_structural_hint_also_names_subquery(self) -> None:
        """措辞修正：真机撞上的是「表达式里嵌子查询」，而原提示只说「不能是整条 SQL
        语句」—— 模型会认为自己没犯这条，引导因此打折。"""
        assert "子查询" in _STRUCTURAL_FORMULA_HINT
