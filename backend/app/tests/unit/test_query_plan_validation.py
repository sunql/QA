"""QueryPlan 校验逻辑单元测试。

validatePlan(plan, classes) 纯代码校验计划引用是否在本体 schema 中，不调 LLM。
返回问题列表；空列表 = 通过。
"""

from __future__ import annotations

from app.domain.models import OntologyClass, OntologyProperty
from app.domain.query_plan import Aggregation, JoinSpec, QueryPlan, SortSpec
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
