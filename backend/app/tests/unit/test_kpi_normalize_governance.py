"""P0 治理规范化纯逻辑单测（无 IO）。

钉住的是**派生规则本身**：
- 已有 measure ⇒ 治理列 = measure.expr 的列集合；
- MEASURE 缺 measure ⇒ 从 formula 外层聚合派生；
- RATIO 缺两侧 measure ⇒ 按顶层 `/` 拆分派生；
- execution_source=NONE ⇒ 结构化字段清空（unknown / Tier C）；
- 派生列必须能在源类物理列里找到，否则拒绝。

反假绿：第 7 条用例证明「列集合错一列」时对账确实变红。
"""

from __future__ import annotations

import pytest

from app.domain.models import KpiCatalog, OntologyMetric
from app.services.kpi_governance_normalizer import _assertPhysical
from app.services.kpi_metric_reconcile import (
    GovernanceSpec,
    reconcileMetric,
)
from app.services.kpi_metric_spec import techniqueOf
from scripts.kpi_normalize_governance import (
    NormalizationError,
    deriveNormalization,
    planNormalizations,
    renderReport,
    splitTopLevelDivision,
    stripAliases,
)

_OTD_FORMULA = (
    "SELECT COUNT(CASE WHEN R.RECEIPT_DATE <= O.PROMISED_RECEIPT_DATE THEN 1 END) "
    "/ NULLIF(COUNT(O.PROMISED_RECEIPT_DATE), 0) "
    "FROM THBI.DWD_PURCHASE_ORDER_LINE O JOIN THBI.DWD_GOODS_RECEIPT_LINE R "
    "ON O.PO_NO = R.PO_NO AND O.PO_LINE_NO = R.PO_LINE_NO"
)
_CYCLE_FORMULA = (
    "SELECT AVG(TRUNC(R.RECEIPT_DATE) - TRUNC(L.CREATION_DATE)) "
    "FROM THBI.DWD_REQUISITION_ORDER_LINK K "
    "JOIN THBI.DWD_PURCHASE_REQUISITION_LINE L ON K.REQUISITION_NO = L.REQUISITION_NO"
)
_PRICE_VARIANCE_FORMULA = (
    "SELECT AVG((O.UNIT_PRICE - P.UNIT_PRICE) / NULLIF(P.UNIT_PRICE, 0)) "
    "FROM THBI.DWD_PURCHASE_ORDER_LINE O JOIN THBI.DWD_SUPPLIER_PRICE_LIST P "
    "ON P.MATERIAL_CODE = O.SUPPLIER_MATERIAL_CODE"
)


def _kpi(
    code: str,
    *,
    num: list[str] | None = None,
    den: list[str] | None = None,
    grain: list[str] | None = None,
    metric_id: int | None = 1,
) -> KpiCatalog:
    return KpiCatalog(
        kpi_code=code,
        metric_id=metric_id,
        numerator_columns=num,
        denominator_columns=den,
        grain_keys=grain,
    )


def _metric(
    *,
    metric_type: str,
    source: str,
    formula: str = "",
    num: dict | None = None,
    den: dict | None = None,
    grain: list[str] | None = None,
    order: str | None = None,
) -> OntologyMetric:
    return OntologyMetric(
        metric_name="测试指标",
        metric_type=metric_type,
        execution_source=source,
        aggregation_order=order,
        numerator_measure=num,
        denominator_measure=den,
        grain_keys=grain or [],
        formula=formula,
    )


def test_measure_json_drives_governance_columns() -> None:
    """已有 measure：治理列取 measure.expr 列集合（大写），measure 原样保留。"""
    plan = deriveNormalization(
        _kpi(
            "KPI_SUPPLIER_OTD",
            num=["ACTUAL_RECEIPT_DATE"],
            den=["PROMISED_DATE"],
            grain=["供应商", "工厂", "月"],
        ),
        _metric(
            metric_type="RATIO",
            source="ESCAPE_HATCH",
            order="WEIGHTED_RATIO",
            formula=_OTD_FORMULA,
            num={"agg": "COUNT", "expr": "RECEIPT_DATE"},
            den={"agg": "COUNT", "expr": "PROMISED_RECEIPT_DATE"},
            grain=["SUPPLIER_CODE", "FACILITY_CODE"],
        ),
        frozenset(
            {"RECEIPT_DATE", "PROMISED_RECEIPT_DATE", "SUPPLIER_CODE", "FACILITY_CODE"}
        ),
    )

    assert plan.numerator_columns == ("RECEIPT_DATE",)
    assert plan.denominator_columns == ("PROMISED_RECEIPT_DATE",)
    assert plan.grain_keys == ("SUPPLIER_CODE", "FACILITY_CODE")
    assert plan.numerator_measure is None
    assert plan.denominator_measure is None
    assert plan.basis == "measure_json"
    assert plan.verdict_before == "mismatch"
    assert plan.verdict_after == "pass"
    assert plan.tier_after == "A"
    assert plan.is_noop is False


def test_measure_metric_derives_numerator_measure_from_formula() -> None:
    """MEASURE 缺 measure：外层聚合 ⇒ 可对账的结构化度量（表达式原样保留语义）。"""
    plan = deriveNormalization(
        _kpi(
            "KPI_PURCHASE_CYCLE_TIME",
            num=["GOODS_RECEIPT_DATE", "PR_CREATED_DATE"],
            grain=["供应商", "工厂", "月"],
        ),
        _metric(
            metric_type="MEASURE",
            source="ESCAPE_HATCH",
            formula=_CYCLE_FORMULA,
            grain=["SUPPLIER_CODE", "FACILITY_CODE"],
        ),
        frozenset({"RECEIPT_DATE", "CREATION_DATE", "SUPPLIER_CODE", "FACILITY_CODE"}),
    )

    assert plan.basis == "formula_split_measure"
    # 别名与 TRUNC 必须原样保留：去掉别名会把 `R.RECEIPT_DATE` / `L.CREATION_DATE`
    # 压成同一个列名，去掉 TRUNC 会改变取整语义 —— 落库的表达式必须可执行。
    assert plan.numerator_measure == {
        "agg": "AVG",
        "expr": "TRUNC(R.RECEIPT_DATE) - TRUNC(L.CREATION_DATE)",
    }
    assert plan.numerator_columns == ("CREATION_DATE", "RECEIPT_DATE")
    assert plan.denominator_columns == ()
    assert plan.verdict_after == "pass"


def test_ratio_without_measures_splits_top_level_division() -> None:
    """RATIO 缺两侧 measure：顶层 `/` 左/右分别成为分子/分母度量。"""
    plan = deriveNormalization(
        _kpi(
            "KPI_PURCHASE_PRICE_VARIANCE",
            num=["ACTUAL_PRICE", "STANDARD_PRICE"],
            den=["STANDARD_PRICE"],
            grain=["物料", "工厂", "月"],
        ),
        _metric(
            metric_type="RATIO",
            source="ESCAPE_HATCH",
            order="AVG_OF_RATIOS",
            formula=_PRICE_VARIANCE_FORMULA,
            grain=["MATERIAL_CODE", "FACILITY_CODE"],
        ),
        frozenset({"UNIT_PRICE", "MATERIAL_CODE", "FACILITY_CODE"}),
    )

    assert plan.basis == "formula_split_ratio"
    # 反假绿：`O.UNIT_PRICE - P.UNIT_PRICE` 去掉别名会退化成 `UNIT_PRICE - UNIT_PRICE`
    # （恒等于 0）。表达式必须保留限定名，列集合仍只含物理列。
    assert plan.numerator_measure == {
        "agg": "AVG",
        "expr": "(O.UNIT_PRICE - P.UNIT_PRICE)",
    }
    assert plan.denominator_measure == {"agg": "AVG", "expr": "NULLIF(P.UNIT_PRICE, 0)"}
    assert plan.numerator_columns == ("UNIT_PRICE",)
    assert plan.denominator_columns == ("UNIT_PRICE",)
    assert plan.verdict_after == "pass"


def test_none_execution_source_clears_structured_fields() -> None:
    """NONE：无可执行技术形态 ⇒ 清空结构化字段，unknown / Tier C，业务待补项入报告。"""
    todo_text = "口径声明：检验结果为 FAIL 的检验数量 / 检验总数量。技术形态待业务补。"
    plan = deriveNormalization(
        _kpi(
            "KPI_SUPPLIER_DEFECT_RATE",
            num=["IQCRESULT"],
            grain=["供应商", "物料", "工厂", "月"],
        ),
        _metric(
            metric_type="RATIO",
            source="NONE",
            order="WEIGHTED_RATIO",
            formula=todo_text,
            grain=["SUPPLIER_CODE", "MATERIAL_CODE", "FACILITY_CODE"],
        ),
        frozenset({"SUPPLIER_CODE"}),
    )

    assert plan.basis == "none_tier_c"
    assert plan.numerator_columns == ()
    assert plan.denominator_columns == ()
    assert plan.grain_keys == ()
    assert plan.numerator_measure is None
    assert plan.verdict_before == "mismatch"
    assert plan.verdict_after == "unknown"
    assert plan.tier_after == "C"
    assert plan.business_todo == todo_text


def test_formula_column_missing_from_source_classes_is_rejected() -> None:
    """物理列校验：formula 引用了源类里不存在的列 ⇒ 拒绝（脚本退出码 2）。"""
    with pytest.raises(NormalizationError, match="MYSTERY_COL"):
        deriveNormalization(
            _kpi("KPI_BAD", num=["SOMETHING"]),
            _metric(
                metric_type="MEASURE",
                source="ESCAPE_HATCH",
                formula="SELECT SUM(MYSTERY_COL) FROM THBI.SOME_TABLE",
                grain=["SUPPLIER_CODE"],
            ),
            frozenset({"UNIT_PRICE"}),
        )


def test_plan_is_idempotent() -> None:
    """把派生结果喂回去 ⇒ 不再产生任何变更（apply 可安全重跑）。"""
    class_cols = frozenset(
        {"RECEIPT_DATE", "PROMISED_RECEIPT_DATE", "SUPPLIER_CODE", "FACILITY_CODE"}
    )
    metric = _metric(
        metric_type="RATIO",
        source="ESCAPE_HATCH",
        order="WEIGHTED_RATIO",
        formula=_OTD_FORMULA,
        num={"agg": "COUNT", "expr": "RECEIPT_DATE"},
        den={"agg": "COUNT", "expr": "PROMISED_RECEIPT_DATE"},
        grain=["SUPPLIER_CODE", "FACILITY_CODE"],
    )
    first = deriveNormalization(
        _kpi("KPI_SUPPLIER_OTD", num=["ACTUAL_RECEIPT_DATE"], den=["PROMISED_DATE"]),
        metric,
        class_cols,
    )
    second = deriveNormalization(
        _kpi(
            "KPI_SUPPLIER_OTD",
            num=list(first.numerator_columns),
            den=list(first.denominator_columns),
            grain=list(first.grain_keys),
        ),
        metric,
        class_cols,
    )

    assert second.is_noop is True
    assert second.numerator_columns == first.numerator_columns
    assert second.verdict_after == "pass"


def test_reverse_guard_wrong_column_set_is_still_mismatch() -> None:
    """反假绿：派生结果少一列 ⇒ 对账必须变红（证明断言不是恒真）。"""
    plan = deriveNormalization(
        _kpi("KPI_SUPPLIER_OTD", num=["ACTUAL_RECEIPT_DATE"], den=["PROMISED_DATE"]),
        _metric(
            metric_type="RATIO",
            source="ESCAPE_HATCH",
            order="WEIGHTED_RATIO",
            formula=_OTD_FORMULA,
            num={"agg": "COUNT", "expr": "RECEIPT_DATE"},
            den={"agg": "COUNT", "expr": "PROMISED_RECEIPT_DATE"},
            grain=["SUPPLIER_CODE", "FACILITY_CODE"],
        ),
        frozenset({"RECEIPT_DATE", "PROMISED_RECEIPT_DATE", "SUPPLIER_CODE"}),
    )
    # 空集合会被判「治理未声明」= unknown；要证明比较器会红，必须给错列名。
    broken = GovernanceSpec(
        numerator_columns=("NOT_A_COLUMN",),
        denominator_columns=plan.denominator_columns,
        grain_keys=plan.grain_keys,
    )
    result = reconcileMetric(
        broken,
        techniqueOf(
            _metric(
                metric_type="RATIO",
                source="ESCAPE_HATCH",
                order="WEIGHTED_RATIO",
                formula=_OTD_FORMULA,
                num={"agg": "COUNT", "expr": "RECEIPT_DATE"},
                den={"agg": "COUNT", "expr": "PROMISED_RECEIPT_DATE"},
                grain=["SUPPLIER_CODE", "FACILITY_CODE"],
            )
        ),
    )

    assert result.verdict == "mismatch"


def test_plan_normalizations_keeps_input_order_and_report_counts() -> None:
    """批量计划保持输入顺序；报告汇总 pass/unknown/mismatch。"""
    otd = _kpi("KPI_SUPPLIER_OTD", num=["ACTUAL_RECEIPT_DATE"], den=["PROMISED_DATE"])
    defect = _kpi("KPI_SUPPLIER_DEFECT_RATE", num=["IQCRESULT"])
    metrics = {
        1: _metric(
            metric_type="RATIO",
            source="ESCAPE_HATCH",
            order="WEIGHTED_RATIO",
            formula=_OTD_FORMULA,
            num={"agg": "COUNT", "expr": "RECEIPT_DATE"},
            den={"agg": "COUNT", "expr": "PROMISED_RECEIPT_DATE"},
            grain=["SUPPLIER_CODE"],
        ),
        2: _metric(
            metric_type="RATIO",
            source="NONE",
            order="WEIGHTED_RATIO",
            formula="口径声明：待补",
            grain=["SUPPLIER_CODE"],
        ),
    }
    class_cols = {1: frozenset({"RECEIPT_DATE", "PROMISED_RECEIPT_DATE"}), 2: frozenset()}
    otd.metric_id = 1
    defect.metric_id = 2

    plans, skipped = planNormalizations([otd, defect], metrics, class_cols)
    assert [p.kpi_code for p in plans] == ["KPI_SUPPLIER_OTD", "KPI_SUPPLIER_DEFECT_RATE"]
    assert skipped == []

    report = renderReport(plans)
    assert "summary: pass=1 unknown=1 mismatch=0" in report
    assert "KPI_SUPPLIER_OTD" in report
    assert "KPI_SUPPLIER_DEFECT_RATE" in report


def test_plan_normalizations_skips_unlinked_draft_kpi() -> None:
    """未挂靠 metric 的草稿 KPI 跳过并报告，**不得**让整批（含 dry-run）失败。

    ``KpiCatalogCreate.metric_id`` 可空、``status`` 默认 DRAFT ⇒ 生产库必然
    存在这类行；它们是合法的「待挂靠」状态，不是工具缺陷。
    """
    linked = _kpi("KPI_LINKED", num=["QTY"], metric_id=1)
    draft = _kpi("KPI_DRAFT", num=[], metric_id=None)
    metrics = {
        1: _metric(
            metric_type="MEASURE",
            source="ESCAPE_HATCH",
            formula="SELECT SUM(QTY) FROM T",
            num={"agg": "SUM", "expr": "QTY"},
            grain=["SUPPLIER_CODE"],
        )
    }

    plans, skipped = planNormalizations(
        [linked, draft], metrics, {1: frozenset({"QTY"})}
    )

    assert [p.kpi_code for p in plans] == ["KPI_LINKED"]
    assert skipped == ["KPI_DRAFT"]


# ---------------------------------------------------------------------------
# 复审修复：SQL 文本处理必须认识字符串字面量 / 函数调用
# ---------------------------------------------------------------------------


def test_alias_stripping_preserves_string_literals() -> None:
    """`'N.A'` 是字面量，不能被当成 `R.` 这类别名前缀去掉。"""
    expr = "CASE WHEN R.STATUS = 'N.A' THEN R.QTY END"

    assert stripAliases(expr) == "CASE WHEN STATUS = 'N.A' THEN QTY END"


def test_split_top_level_division_ignores_literal_slash() -> None:
    """字面量里的 `/` 不是除法。"""
    assert splitTopLevelDivision("CASE WHEN S = 'A/B' THEN X END") is None


def test_physical_check_ignores_literals_and_function_names() -> None:
    """`'FAIL'` 与 `NVL` 不是列；列名命中即通过（不误拒）。"""
    expr = "NVL(CASE WHEN STATUS = 'FAIL' THEN QTY END, 0)"

    # 不抛异常即通过
    _assertPhysical(expr, frozenset({"status", "qty"}), "KPI_OK")


def test_physical_check_still_rejects_missing_column() -> None:
    """反向守卫：真正缺列仍然拒绝（证明上面的放行不是恒真）。"""
    with pytest.raises(NormalizationError, match="MYSTERY_COL"):
        _assertPhysical("NVL(MYSTERY_COL, 0)", frozenset({"qty"}), "KPI_BAD")


def test_partial_ratio_preserves_existing_measure_json() -> None:
    """RATIO 只有一侧 measure：只写缺的一侧，已有侧原样保留（含 filter）。"""
    plan = deriveNormalization(
        _kpi(
            "KPI_PARTIAL_RATIO",
            num=["RECEIPT_DATE"],
            den=["PROMISED_RECEIPT_DATE"],
            grain=["SUPPLIER_CODE"],
        ),
        _metric(
            metric_type="RATIO",
            source="ESCAPE_HATCH",
            order="WEIGHTED_RATIO",
            formula=_OTD_FORMULA,
            num={
                "agg": "COUNT",
                "expr": "RECEIPT_DATE",
                "filter": "RECEIPT_DATE > SYSDATE",
                "class_id": 7,
            },
            grain=["SUPPLIER_CODE"],
        ),
        frozenset({"RECEIPT_DATE", "PROMISED_RECEIPT_DATE", "SUPPLIER_CODE"}),
    )

    assert plan.numerator_measure is None  # 已有侧不重写，保留 filter/class_id
    # 派生侧保留公式里的限定名（`O.`），不压成裸列名。
    assert plan.denominator_measure == {
        "agg": "COUNT",
        "expr": "O.PROMISED_RECEIPT_DATE",
    }
    assert plan.verdict_after == "pass"


def test_pass_but_degraded_reports_tier_b() -> None:
    """层 2 pass 但层 1 降级 ⇒ Tier B（不是 C，也不是 A）。"""
    plan = deriveNormalization(
        _kpi(
            "KPI_DEGRADED",
            num=["B"],
            grain=["SUPPLIER_CODE"],
        ),
        _metric(
            metric_type="MEASURE",
            source="ESCAPE_HATCH",
            formula="SELECT SUM(A) FROM THBI.T",
            num={"agg": "SUM", "expr": "B"},
            grain=["SUPPLIER_CODE"],
        ),
        frozenset({"A", "B", "SUPPLIER_CODE"}),
    )

    assert plan.verdict_after == "pass"
    assert plan.findings_after  # 有降级 finding
    assert plan.tier_after == "B"


def test_noop_compares_unordered_columns() -> None:
    """已有列集合相同但顺序不同 ⇒ 仍算 noop（不产生无意义重写）。"""
    plan = deriveNormalization(
        _kpi(
            "KPI_UNORDERED",
            num=["RECEIPT_DATE", "CREATION_DATE"],
            grain=["SUPPLIER_CODE"],
        ),
        _metric(
            metric_type="MEASURE",
            source="ESCAPE_HATCH",
            formula="SELECT AVG(R.RECEIPT_DATE - L.CREATION_DATE) FROM THBI.T",
            num={"agg": "AVG", "expr": "(RECEIPT_DATE) - (CREATION_DATE)"},
            grain=["SUPPLIER_CODE"],
        ),
        frozenset({"RECEIPT_DATE", "CREATION_DATE", "SUPPLIER_CODE"}),
    )

    assert plan.numerator_columns == ("CREATION_DATE", "RECEIPT_DATE")
    assert plan.is_noop is True
