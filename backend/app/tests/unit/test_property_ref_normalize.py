"""复合形式 property 名（'业务名 (alias)'）归一化的单元测试。

触发场景：LLM 偶尔从 schema 渲染文本（业务名 (alias): 类型 (column=物理列)）原样
抄 property_name，validatePlan 严格 token 匹配必拒。归一化在 _parsePlanFromResponse
之后 / validatePlan 之前用本体合法引用集合把 'name (alias)' 替换成首个合法 token，
让后续链路不感知复合形式。本测试覆盖拆分与归一化两个 helper 的所有行为分支。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.domain.models import OntologyClass, OntologyProperty
from app.domain.query_plan import Aggregation, JoinSpec, PlanResult, QueryPlan, SortSpec
from app.services.nl2sql_service import (
    Nl2SqlService,
    _normalizePlanProperties,
    _splitCompoundRef,
)


def _cls(name: str, props: list[tuple[str, str]], table: str | None = None) -> OntologyClass:
    """构造带属性的本体类。props: (property_name, source_column)。"""
    properties = [
        OntologyProperty(property_name=pn, source_column=sc) for pn, sc in props
    ]
    return OntologyClass(class_name=name, source_table=table or f"T_{name}", properties=properties)


def _receiptCls() -> OntologyClass:
    # 供应商 业务名 + BPSNUM_0 物理列 / 物料编号 + ITMREF_0 / 库存数量 + QTYUOM_0（biz_aliases=['收货数量', '入库数量']）
    props = [
        OntologyProperty(property_name="PTHNUM", source_column="PTHNUM_0"),
        OntologyProperty(
            property_name="供应商", source_column="BPSNUM_0", property_alias="BPSNUM_0",
        ),
        OntologyProperty(
            property_name="物料编号", source_column="ITMREF_0", property_alias="ITMREF_0",
        ),
        OntologyProperty(
            property_name="库存数量",
            source_column="QTYUOM_0",
            property_alias="QTYUOM_0",
            business_aliases=["收货数量", "入库数量"],
        ),
        OntologyProperty(
            property_name="收货日期", source_column="RCPDAT_0", property_alias="RCPDAT_0",
        ),
    ]
    return OntologyClass(
        class_name="ReceiptDetail", source_table="ODS_PRECEIPTD", properties=props,
    )


class TestSplitCompoundRef:
    def test_standard_compound(self) -> None:
        # 业务名 + 物理列 ASCII 括号（用户报告的实际格式）
        assert _splitCompoundRef("供应商 (BPSNUM_0)") == ("供应商", "BPSNUM_0")

    def test_no_parens_returns_unchanged(self) -> None:
        assert _splitCompoundRef("供应商") == ("供应商", None)

    def test_alone_alias(self) -> None:
        # 仅有括号，括号内是合法 alias
        assert _splitCompoundRef("(BPSNUM_0)") == ("", "BPSNUM_0")

    def test_empty_string(self) -> None:
        assert _splitCompoundRef("") == ("", None)

    def test_whitespace_padding(self) -> None:
        # 双侧空格应 trim
        assert _splitCompoundRef("  供应商  (  BPSNUM_0  )  ") == ("供应商", "BPSNUM_0")

    def test_chinese_parens_not_matched(self) -> None:
        # 中文括号（）≠ ASCII 括号 ()，不被识别为复合形式；
        # 真实数据库 state 里 '收货数量（QTYUOM_0）' 即此形式，原样保留
        assert _splitCompoundRef("收货数量（QTYUOM_0）") == ("收货数量（QTYUOM_0）", None)

    def test_non_string_passthrough(self) -> None:
        # 非字符串输入容错（防御性）
        assert _splitCompoundRef(None) == (None, None)  # type: ignore[arg-type]

    def test_nested_parens_returns_unchanged(self) -> None:
        # 嵌套括号（合法 regex 不会跨层解析），原样保留
        assert _splitCompoundRef("供应商 ((BPSNUM))") == ("供应商 ((BPSNUM))", None)


class TestNormalizePlanProperties:
    def test_compound_resolved_to_business_name(self) -> None:
        # 复合串 + 业务名合法 → 替换为业务名
        plan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            selectedProperties=("供应商 (BPSNUM_0)",),
        )
        result = _normalizePlanProperties(plan, [_receiptCls()])
        assert result.selectedProperties == ("供应商",)
        # 输入 plan 不动（immutable）
        assert plan.selectedProperties == ("供应商 (BPSNUM_0)",)

    def test_compound_resolved_to_alias_when_business_unknown(self) -> None:
        # 业务名不在 schema（用户场景极少但应兜底），alias 在 → 回退 alias
        plan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            selectedProperties=("未知物料 (BPSNUM_0)",),
        )
        result = _normalizePlanProperties(plan, [_receiptCls()])
        assert result.selectedProperties == ("BPSNUM_0",)

    def test_compound_both_sides_unknown_preserves_business_name(self) -> None:
        # 两边都不在 → 保留业务名（让 validatePlan 走原有错误链路）
        plan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            selectedProperties=("鬼字段 (GHOST_0)",),
        )
        result = _normalizePlanProperties(plan, [_receiptCls()])
        assert result.selectedProperties == ("鬼字段",)

    def test_single_token_legal_unchanged(self) -> None:
        # 单 token 合法 → 不动
        plan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            selectedProperties=("供应商", "BPSNUM_0", "收货数量"),
        )
        result = _normalizePlanProperties(plan, [_receiptCls()])
        assert result.selectedProperties == ("供应商", "BPSNUM_0", "收货数量")

    def test_chinese_parens_unchanged(self) -> None:
        # 中文括号不是复合形式，原样保留（validatePlan 后续按 '收货数量（QTYUOM_0）' 处理）
        plan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            selectedProperties=("收货数量（QTYUOM_0）",),
        )
        result = _normalizePlanProperties(plan, [_receiptCls()])
        assert result.selectedProperties == ("收货数量（QTYUOM_0）",)

    def test_normalizes_aggregation_property(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            aggregations=(Aggregation(function="SUM", property="库存数量 (QTYUOM_0)", alias="TOTAL"),),
        )
        out = _normalizePlanProperties(plan, [_receiptCls()])
        assert out.aggregations[0].property == "库存数量"

    def test_normalizes_group_by_partition_by(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            groupBy=("供应商 (BPSNUM_0)", "物料编号 (ITMREF_0)"),
            partitionBy=("供应商 (BPSNUM_0)",),
        )
        out = _normalizePlanProperties(plan, [_receiptCls()])
        assert out.groupBy == ("供应商", "物料编号")
        assert out.partitionBy == ("供应商",)

    def test_normalizes_sort_property(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            sortBy=(SortSpec(property="物料编号 (ITMREF_0)", direction="asc"),),
        )
        out = _normalizePlanProperties(plan, [_receiptCls()])
        assert out.sortBy[0].property == "物料编号"

    def test_normalizes_join_columns(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            joins=(JoinSpec(sourceClass="ReceiptDetail", targetClass="ItemMaster", columns=("物料编号 (ITMREF_0)",)),),
        )
        out = _normalizePlanProperties(plan, [_receiptCls()])
        assert out.joins[0].columns == ("物料编号",)

    def test_empty_classes_returns_plan_unchanged(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            selectedProperties=("供应商 (BPSNUM_0)",),
        )
        result = _normalizePlanProperties(plan, [])
        # 没有任何 classes 时无法做合法 token 校验，原样保留
        assert result.selectedProperties == ("供应商 (BPSNUM_0)",)
        assert result == plan  # frozen 不变对象应直接返回

    def test_returns_new_plan_object(self) -> None:
        plan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            selectedProperties=("供应商 (BPSNUM_0)",),
        )
        out = _normalizePlanProperties(plan, [_receiptCls()])
        assert out is not plan  # 不可变 dataclass 必须返回新对象


class TestValidatePlanAfterNormalize:
    """集成：复合串 plan 经 _normalizePlanProperties 后 validatePlan 应通过。"""

    def test_compound_form_selectable_properties_pass(self) -> None:
        plan = QueryPlan(
            target="3月份供货量TOP3物料占比",
            selectedClasses=("ReceiptDetail",),
            selectedProperties=(
                "供应商 (BPSNUM_0)",
                "物料编号 (ITMREF_0)",
                "收货数量 (QTYUOM_0)",
                "收货日期 (RCPDAT_0)",
            ),
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="库存数量 (QTYUOM_0)",
                    alias="TOTAL_QTY",
                ),
                Aggregation(
                    function="SUM",
                    property="库存数量 (QTYUOM_0)",
                    alias="QTY_PCT",
                    formula="SUM(QTYUOM_0) / SUM(SUM(QTYUOM_0)) OVER ()",
                ),
            ),
            groupBy=("供应商 (BPSNUM_0)", "物料编号 (ITMREF_0)"),
            partitionBy=("供应商 (BPSNUM_0)",),
            perGroupLimit=3,
            sortBy=(SortSpec(property="TOTAL_QTY", direction="desc"),),
        )
        normalized = _normalizePlanProperties(plan, [_receiptCls()])
        issues = Nl2SqlService().validatePlan(normalized, [_receiptCls()])
        assert issues == [], f"复合串 plan 归一化后应通过 validatePlan，实际问题：{issues}"

    def test_truly_unknown_property_still_fails(self) -> None:
        # 复合串两边都不在 → 保留业务名 → validatePlan 仍如实报错（不掩盖）
        plan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            selectedProperties=("鬼字段 (GHOST_0)",),
        )
        normalized = _normalizePlanProperties(plan, [_receiptCls()])
        issues = Nl2SqlService().validatePlan(normalized, [_receiptCls()])
        assert any("鬼字段" in i and "本体 schema" in i for i in issues)


class TestGenerateValidatedPlanRetryNormalizes:
    """集成：generateValidatedPlan 重试路径同样归一化（2026-09-18 code-review HIGH 修复）。

    第一次 generateQueryPlan 返回合法 plan（无复合串）→ 直接 _finalizePlan，循环不执行。
    强制首条返回复合串计划以触发 validatePlan issues → 重试；第二次返回复合串计划
    → 走循环内 normalize → validatePlan 仍能通过（或失败原因不再是复合串）。
    """

    @staticmethod
    def _goodPlan() -> QueryPlan:
        return QueryPlan(
            target="3月份供货量TOP3物料占比",
            selectedClasses=("ReceiptDetail",),
            selectedProperties=("供应商", "物料编号", "库存数量", "收货日期"),
            aggregations=(
                Aggregation(function="SUM", property="库存数量", alias="TOTAL_QTY"),
            ),
            groupBy=("供应商", "物料编号"),
            partitionBy=("供应商",),
            perGroupLimit=3,
            sortBy=(SortSpec(property="TOTAL_QTY", direction="desc"),),
        )

    @staticmethod
    def _compoundPlan() -> QueryPlan:
        # 同 _goodPlan 但所有 property 都是复合形式（验证 normalize 真跑了）
        return QueryPlan(
            target="3月份供货量TOP3物料占比",
            selectedClasses=("ReceiptDetail",),
            selectedProperties=(
                "供应商 (BPSNUM_0)",
                "物料编号 (ITMREF_0)",
                "库存数量 (QTYUOM_0)",
                "收货日期 (RCPDAT_0)",
            ),
            aggregations=(
                Aggregation(function="SUM", property="库存数量 (QTYUOM_0)", alias="TOTAL_QTY"),
            ),
            groupBy=("供应商 (BPSNUM_0)", "物料编号 (ITMREF_0)"),
            partitionBy=("供应商 (BPSNUM_0)",),
            perGroupLimit=3,
            sortBy=(SortSpec(property="TOTAL_QTY", direction="desc"),),
        )

    async def test_retry_plan_compound_form_normalized(self) -> None:
        """code-review HIGH：重试路径上的复合串也必须归一化，否则 validatePlan 会拒。

        模拟：首次 generateQueryPlan 返回带「鬼字段 (GHOST_0)」的计划 → validatePlan 报错 →
        重试 generateQueryPlan 返回复合串计划 → 循环内 normalize → 复合串替换 → 通过。
        """
        service = Nl2SqlService()
        classes = [_receiptCls()]

        # 首次：含未知字段（validatePlan 必报错）
        badPlan = QueryPlan(
            target="x",
            selectedClasses=("ReceiptDetail",),
            selectedProperties=("鬼字段 (GHOST_0)",),
        )
        # 重试：复合串（这是真正的回归路径）
        retryPlan = self._compoundPlan()

        call_count = {"n": 0}

        async def fakeGenPlan(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return PlanResult(plan=badPlan, promptTokens=10, completionTokens=5)
            return PlanResult(plan=retryPlan, promptTokens=10, completionTokens=5)

        # 桩 LLM（generateSql 不会被调到，因为我们 mock 到 _finalizePlan 之前）
        fakeLlm = SimpleNamespace(complete=None)

        # monkey-patch generateQueryPlan + _finalizePlan（只关心 normalize + validate 循环）
        original_gen = service.generateQueryPlan
        original_finalize = service._finalizePlan
        service.generateQueryPlan = fakeGenPlan  # type: ignore[method-assign]
        captured: dict[str, PlanResult] = {}

        def captureFinalize(planResult, *a, **kw):
            captured["pr"] = planResult
            return planResult

        service._finalizePlan = captureFinalize  # type: ignore[method-assign]
        try:
            await service.generateValidatedPlan(
                "这3家供货量最多3种物料占比", classes, fakeLlm, SimpleNamespace(model_name="x", temperature=0.0),
                joins=None,
            )
        finally:
            service.generateQueryPlan = original_gen  # type: ignore[method-assign]
            service._finalizePlan = original_finalize  # type: ignore[method-assign]

        assert call_count["n"] == 2, f"应触发一次重试，实际 {call_count['n']} 次"
        # 关键：归一化后 plan 已无复合串 → 进入 _finalizePlan 的是干净 plan
        final_plan = captured["pr"].plan
        assert all("(" not in p for p in final_plan.selectedProperties), (
            f"重试路径复合串未归一化：{final_plan.selectedProperties}"
        )
        assert final_plan.selectedProperties == ("供应商", "物料编号", "库存数量", "收货日期")
        assert final_plan.aggregations[0].property == "库存数量"