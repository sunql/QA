"""KPI 指标技术形态的**候选映射表**（12 条 KPI → 本体指标技术形态）。

本模块是纯数据 + 纯函数（不碰库），被回填脚本与 Task 16 的真机验证脚本共用。

四条硬约束（都是实测逼出来的）：

1. **不照抄治理口径的列名**。dev 库 12 条 KPI 的口径引用的 15 个列名里只有 3 个
   在物理 schema 存在（``SUPPLIER_CODE`` / ``FACILITY_CODE`` / ``DUE_DATE``），
   其余 12 个 0 命中（``ACTUAL_RECEIPT_DATE`` / ``PROMISED_DATE`` / …）。
   照抄 ⇒ 度量指向不存在的列 ⇒ 生成的 SQL 必然报错。
   ⇒ 结构化度量用**物理列**，且落库前**逐列到 schema_cache 校验**。

2. **治理列 ≠ 物理列**（T15-L）。``kpi_catalog.numerator_columns`` 等结构化列
   写的是**业务原声明**（如 ``ACTUAL_RECEIPT_DATE``），不是物理列
   （``RECEIPT_DATE``）——否则层 2 对账变成「脚本自己和自己比」，发布闸门
   退化成「脚本自己签字」。⇒ 人工按审核清单逐条确认映射后，把治理列改成
   物理列，层 2 才 pass、闸门才放行。

3. **class_id 不硬编码**（T15-C）。``ontology_class.id`` 是自增主键，与插入顺序
   绑定 ⇒ 硬编码 id 换库即失效。⇒ 类引用按 **source_table（表名）** 记录，
   落库前按表名查 ``ontology_class`` 拿 id；查不到 ⇒ 该条跳过并进审核清单。

4. **公式按 Oracle 方言**（T15-M）。DW 层是 Oracle（``THBI.*``）⇒
   ``TRUNC(a) - TRUNC(b)``（不是 ``DATEDIFF``）、JOIN 必须带
   ``(po_no, po_line_no)`` 双键、物理源不可确认的条目**不写可执行公式**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.enums import (
    MetricAggregationOrder,
    MetricExecutionSource,
    MetricSourceRole,
    MetricStatus,
    MetricType,
)
from app.domain.schemas import MeasureSpecSchema

# 回填用的系统身份（脚本是运维动作，不归属某个自然人）。
BACKFILL_ACTOR_ID = "system-backfill"


@dataclass(frozen=True)
class MeasurePlan:
    """结构化度量候选（写入 ``ontology_metric.*_measure`` 的 JSONB）。

    ``classTable`` 是 **source_table**（权威键）：落库时按它查 ``ontology_class.id``。
    ``classId`` 只在解析之后才有值 ⇒ 计划常量里**不出现**硬编码主键。
    """

    agg: str
    expr: str
    classTable: str
    classAlias: str
    filter: str | None = None
    classId: int | None = None

    def toSchema(self, classId: int | None = None) -> MeasureSpecSchema:
        """构造 DTO 用的 `MeasureSpecSchema`（字段形状以 schema 为准，不手写）。"""
        resolved = classId if classId is not None else self.classId
        return MeasureSpecSchema(
            agg=self.agg,
            expr=self.expr,
            filter=self.filter,
            class_id=resolved,
            class_alias=self.classAlias,
        )

    def toRaw(self) -> dict[str, Any]:
        """落库形态：**snake_case** 键（与 ``MeasureSpec.fromRaw`` 的读法一致）。

        未解析出 class_id 时**不写该键**（``exclude_none``）⇒ 计划层不携带
        任何硬编码主键。
        """
        return self.toSchema().model_dump(exclude_none=True)


@dataclass(frozen=True)
class KpiBackfillPlan:
    """一条 KPI 的技术形态候选。

    - ``numerator`` / ``denominator``：结构化度量（A 档才有）；
    - ``grainKeys``：**技术**粒度（物理列）；
    - ``governanceNumerator`` / ``governanceDenominator`` / ``governanceGrain``：
      **业务原声明**（治理侧），与上面**刻意不同**（T15-L）；
    - ``classRefs``：``(source_table, role)`` 列表，role ∈ NUMERATOR/DENOMINATOR/FILTER/BASE；
    - ``formula``：可执行形态（``ESCAPE_HATCH``）或口径声明文本（``NONE``）。
    """

    kpiCode: str
    metricName: str
    metricType: str
    aggregationOrder: str
    numerator: MeasurePlan | None
    denominator: MeasurePlan | None
    grainKeys: tuple[str, ...]
    formula: str
    aggFunction: str
    executionSource: str
    classRefs: tuple[tuple[str, str], ...]
    governanceNumerator: tuple[str, ...]
    governanceDenominator: tuple[str, ...]
    governanceGrain: tuple[str, ...]
    deviations: tuple[str, ...]

    def tier(self) -> str:
        """A 结构可映射 / B 口径含算术或物理源部分缺失 / C 物理源缺失。

        - **A**：分子分母都有结构化度量；
        - **C**：``execution_source = NONE`` **且**没有任何可确认的物理源
          （⇒ 连 ``ontology_metric_source`` 行都写不出来）；
        - **B**：其余（口径含算术式，或物理源只存在一部分）。
        """
        if self.numerator is not None and self.denominator is not None:
            return "A"
        if self.executionSource == MetricExecutionSource.NONE.value and not self.classRefs:
            return "C"
        return "B"

    def hasStructuredMeasures(self) -> bool:
        """是否带结构化度量（A 档判定 + 「治理列要不要写」的依据）。"""
        return self.numerator is not None and self.denominator is not None


# ── 12 条映射 ─────────────────────────────────────────────────────────

_PLAN_LIST: tuple[KpiBackfillPlan, ...] = (
    KpiBackfillPlan(
        kpiCode="KPI_SUPPLIER_OTD",
        metricName="KPI_SUPPLIER_OTD 供应商准时交付率",
        metricType=MetricType.RATIO.value,
        aggregationOrder=MetricAggregationOrder.WEIGHTED_RATIO.value,
        numerator=MeasurePlan(
            agg="COUNT",
            expr="RECEIPT_DATE",
            classTable="DWD_GOODS_RECEIPT_LINE",
            classAlias="DWD_GOODS_RECEIPT_LINE",
            filter="RECEIPT_DATE <= PROMISED_RECEIPT_DATE",
        ),
        denominator=MeasurePlan(
            agg="COUNT",
            expr="PROMISED_RECEIPT_DATE",
            classTable="DWD_PURCHASE_ORDER_LINE",
            classAlias="DWD_PURCHASE_ORDER_LINE",
        ),
        grainKeys=("SUPPLIER_CODE", "FACILITY_CODE"),
        formula=(
            "SELECT COUNT(CASE WHEN R.RECEIPT_DATE <= O.PROMISED_RECEIPT_DATE THEN 1 END)"
            " / NULLIF(COUNT(O.PROMISED_RECEIPT_DATE), 0)"
            " FROM THBI.DWD_PURCHASE_ORDER_LINE O"
            " JOIN THBI.DWD_GOODS_RECEIPT_LINE R"
            " ON O.PO_NO = R.PO_NO AND O.PO_LINE_NO = R.PO_LINE_NO"
            " WHERE O.PROMISED_RECEIPT_DATE IS NOT NULL"
        ),
        aggFunction="COUNT",
        executionSource=MetricExecutionSource.ESCAPE_HATCH.value,
        classRefs=(
            ("DWD_GOODS_RECEIPT_LINE", MetricSourceRole.NUMERATOR.value),
            ("DWD_PURCHASE_ORDER_LINE", MetricSourceRole.DENOMINATOR.value),
        ),
        governanceNumerator=("ACTUAL_RECEIPT_DATE",),
        governanceDenominator=("PROMISED_DATE",),
        governanceGrain=("供应商", "工厂", "月"),
        deviations=(
            "治理列 ACTUAL_RECEIPT_DATE ↔ 物理列 THBI.DWD_GOODS_RECEIPT_LINE.RECEIPT_DATE",
            "治理列 PROMISED_DATE ↔ 物理列 THBI.DWD_PURCHASE_ORDER_LINE.PROMISED_RECEIPT_DATE",
            "口径粒度「月」未落成列（SQL 侧按日期列截断到月）",
            "分子条件同时比较承诺日（PROMISED_RECEIPT_DATE 属订单行表，已落在分母度量）",
        ),
    ),
    KpiBackfillPlan(
        kpiCode="KPI_SUPPLIER_OVERDUE_RATIO",
        metricName="KPI_SUPPLIER_OVERDUE_RATIO 供应商逾期率",
        metricType=MetricType.RATIO.value,
        aggregationOrder=MetricAggregationOrder.WEIGHTED_RATIO.value,
        numerator=MeasurePlan(
            agg="COUNT",
            expr="RECEIPT_DATE",
            classTable="DWD_GOODS_RECEIPT_LINE",
            classAlias="DWD_GOODS_RECEIPT_LINE",
            filter="RECEIPT_DATE > PROMISED_RECEIPT_DATE",
        ),
        denominator=MeasurePlan(
            agg="COUNT",
            expr="PROMISED_RECEIPT_DATE",
            classTable="DWD_PURCHASE_ORDER_LINE",
            classAlias="DWD_PURCHASE_ORDER_LINE",
        ),
        grainKeys=("SUPPLIER_CODE", "FACILITY_CODE"),
        formula=(
            "SELECT COUNT(CASE WHEN R.RECEIPT_DATE > O.PROMISED_RECEIPT_DATE THEN 1 END)"
            " / NULLIF(COUNT(O.PROMISED_RECEIPT_DATE), 0)"
            " FROM THBI.DWD_PURCHASE_ORDER_LINE O"
            " JOIN THBI.DWD_GOODS_RECEIPT_LINE R"
            " ON O.PO_NO = R.PO_NO AND O.PO_LINE_NO = R.PO_LINE_NO"
            " WHERE O.PROMISED_RECEIPT_DATE IS NOT NULL"
        ),
        aggFunction="COUNT",
        executionSource=MetricExecutionSource.ESCAPE_HATCH.value,
        classRefs=(
            ("DWD_GOODS_RECEIPT_LINE", MetricSourceRole.NUMERATOR.value),
            ("DWD_PURCHASE_ORDER_LINE", MetricSourceRole.DENOMINATOR.value),
        ),
        governanceNumerator=("ACTUAL_RECEIPT_DATE",),
        governanceDenominator=("PROMISED_DATE",),
        governanceGrain=("供应商", "工厂", "月"),
        deviations=(
            "治理列 ACTUAL_RECEIPT_DATE ↔ 物理列 THBI.DWD_GOODS_RECEIPT_LINE.RECEIPT_DATE",
            "治理列 PROMISED_DATE ↔ 物理列 THBI.DWD_PURCHASE_ORDER_LINE.PROMISED_RECEIPT_DATE",
            "口径粒度「月」未落成列（SQL 侧按日期列截断到月）",
        ),
    ),
    KpiBackfillPlan(
        kpiCode="KPI_PAYMENT_ON_TIME",
        metricName="KPI_PAYMENT_ON_TIME 付款及时率",
        metricType=MetricType.RATIO.value,
        aggregationOrder=MetricAggregationOrder.WEIGHTED_RATIO.value,
        numerator=MeasurePlan(
            agg="COUNT",
            expr="VALUE_DATE",
            classTable="DWD_SUPPLIER_PAYMENT",
            classAlias="DWD_SUPPLIER_PAYMENT",
            filter="VALUE_DATE <= DUE_DATE",
        ),
        denominator=MeasurePlan(
            agg="COUNT",
            expr="DUE_DATE",
            classTable="DWD_SUPPLIER_PAYMENT",
            classAlias="DWD_SUPPLIER_PAYMENT",
        ),
        grainKeys=("SUPPLIER_CODE",),
        formula=(
            "SELECT COUNT(CASE WHEN VALUE_DATE <= DUE_DATE THEN 1 END)"
            " / NULLIF(COUNT(DUE_DATE), 0) FROM THBI.DWD_SUPPLIER_PAYMENT"
        ),
        aggFunction="COUNT",
        executionSource=MetricExecutionSource.ESCAPE_HATCH.value,
        classRefs=(("DWD_SUPPLIER_PAYMENT", MetricSourceRole.BASE.value),),
        governanceNumerator=("PAYMENT_DATE",),
        governanceDenominator=("DUE_DATE",),
        governanceGrain=("供应商", "月"),
        deviations=(
            "治理列 PAYMENT_DATE ↔ 物理列 THBI.DWD_SUPPLIER_PAYMENT.VALUE_DATE（银行付款日）",
            "KPI 的 data_source 写 DWD_PAYMENT，物理表实际是 DWD_SUPPLIER_PAYMENT",
            "口径粒度「月」未落成列（SQL 侧按日期列截断到月）",
        ),
    ),
    KpiBackfillPlan(
        kpiCode="KPI_INVOICE_MATCH_RATE",
        metricName="KPI_INVOICE_MATCH_RATE 发票三单匹配率",
        metricType=MetricType.RATIO.value,
        aggregationOrder=MetricAggregationOrder.WEIGHTED_RATIO.value,
        numerator=MeasurePlan(
            agg="SUM",
            expr="LINE_AMOUNT_EXCL_TAX",
            classTable="DWD_PURCHASE_INVOICE_LINE",
            classAlias="DWD_PURCHASE_INVOICE_LINE",
            filter=(
                "PO_NO = O.PO_NO AND PO_LINE_NO = O.PO_LINE_NO "
                "AND QUANTITY = O.ORDER_QTY AND UNIT_PRICE_NET = O.UNIT_PRICE"
            ),
        ),
        denominator=MeasurePlan(
            agg="SUM",
            expr="AMOUNT_EXCL_TAX",
            classTable="DWD_PURCHASE_INVOICE",
            classAlias="DWD_PURCHASE_INVOICE",
        ),
        grainKeys=("SUPPLIER_CODE", "INVOICE_DATE"),
        formula=(
            "SELECT SUM(CASE WHEN IL.PO_NO = O.PO_NO AND IL.PO_LINE_NO = O.PO_LINE_NO"
            " AND IL.QUANTITY = O.ORDER_QTY AND IL.UNIT_PRICE_NET = O.UNIT_PRICE"
            " THEN IL.LINE_AMOUNT_EXCL_TAX ELSE 0 END)"
            " / NULLIF(SUM(I.AMOUNT_EXCL_TAX), 0)"
            " FROM THBI.DWD_PURCHASE_INVOICE I"
            " JOIN THBI.DWD_PURCHASE_INVOICE_LINE IL"
            " ON IL.INVOICE_TYPE = I.INVOICE_TYPE AND IL.INVOICE_NO = I.INVOICE_NO"
            " JOIN THBI.DWD_PURCHASE_ORDER_LINE O"
            " ON O.PO_NO = IL.PO_NO AND O.PO_LINE_NO = IL.PO_LINE_NO"
        ),
        aggFunction="SUM",
        executionSource=MetricExecutionSource.ESCAPE_HATCH.value,
        classRefs=(
            ("DWD_PURCHASE_INVOICE_LINE", MetricSourceRole.NUMERATOR.value),
            ("DWD_PURCHASE_INVOICE", MetricSourceRole.DENOMINATOR.value),
        ),
        governanceNumerator=("PO", "QTY", "PRICE"),
        governanceDenominator=(),
        governanceGrain=("供应商", "月"),
        deviations=(
            "治理列 PO ↔ 物理列 THBI.DWD_PURCHASE_INVOICE_LINE.PO_NO",
            "治理列 QTY ↔ 物理列 THBI.DWD_PURCHASE_INVOICE_LINE.QUANTITY",
            "治理列 PRICE ↔ 物理列 THBI.DWD_PURCHASE_INVOICE_LINE.UNIT_PRICE_NET",
            "治理分子「发票金额」未点名列 ↔ 物理列 THBI.DWD_PURCHASE_INVOICE_LINE.LINE_AMOUNT_EXCL_TAX",
            "治理分母「总发票金额」未点名列 ↔ 物理列 THBI.DWD_PURCHASE_INVOICE.AMOUNT_EXCL_TAX",
            "「三单」按 PO 号 + 行号 + 数量 + 单价 四要素实现（口径未定义「单」的确切含义）",
        ),
    ),
    KpiBackfillPlan(
        kpiCode="KPI_PURCHASE_CYCLE_TIME",
        metricName="KPI_PURCHASE_CYCLE_TIME 采购周期",
        metricType=MetricType.MEASURE.value,
        aggregationOrder=MetricAggregationOrder.NONE.value,
        numerator=None,
        denominator=None,
        grainKeys=("SUPPLIER_CODE", "FACILITY_CODE"),
        formula=(
            "SELECT AVG(TRUNC(R.RECEIPT_DATE) - TRUNC(L.CREATION_DATE))"
            " FROM THBI.DWD_REQUISITION_ORDER_LINK K"
            " JOIN THBI.DWD_PURCHASE_REQUISITION_LINE L"
            " ON K.REQUISITION_NO = L.REQUISITION_NO"
            " AND K.REQUISITION_LINE_NO = L.REQUISITION_LINE_NO"
            " JOIN THBI.DWD_GOODS_RECEIPT_LINE R"
            " ON K.PO_NO = R.PO_NO AND K.PO_LINE_NO = R.PO_LINE_NO"
        ),
        aggFunction="AVG",
        executionSource=MetricExecutionSource.ESCAPE_HATCH.value,
        classRefs=(
            ("DWD_GOODS_RECEIPT_LINE", MetricSourceRole.BASE.value),
            ("DWD_PURCHASE_REQUISITION_LINE", MetricSourceRole.BASE.value),
            ("DWD_REQUISITION_ORDER_LINK", MetricSourceRole.BASE.value),
        ),
        governanceNumerator=("GOODS_RECEIPT_DATE", "PR_CREATED_DATE"),
        governanceDenominator=(),
        governanceGrain=("供应商", "工厂", "月"),
        deviations=(
            "口径是差值（收货日 - 请购单创建日），结构化词表表达不了 ⇒ 不给结构化度量",
            "治理列 GOODS_RECEIPT_DATE ↔ 物理列 THBI.DWD_GOODS_RECEIPT_LINE.RECEIPT_DATE",
            "治理列 PR_CREATED_DATE ↔ 物理列 THBI.DWD_PURCHASE_REQUISITION_LINE.CREATION_DATE",
            "口径表名 DWD_PURCHASE_REQUISITION 物理上拆成 _LINE + DWD_REQUISITION_ORDER_LINK",
        ),
    ),
    KpiBackfillPlan(
        kpiCode="KPI_SUPPLIER_DELAY_DAYS",
        metricName="KPI_SUPPLIER_DELAY_DAYS 供应商平均延误天数",
        metricType=MetricType.MEASURE.value,
        aggregationOrder=MetricAggregationOrder.NONE.value,
        numerator=None,
        denominator=None,
        grainKeys=("SUPPLIER_CODE", "FACILITY_CODE"),
        formula=(
            "SELECT AVG(TRUNC(R.RECEIPT_DATE) - TRUNC(O.PROMISED_RECEIPT_DATE))"
            " FROM THBI.DWD_PURCHASE_ORDER_LINE O"
            " JOIN THBI.DWD_GOODS_RECEIPT_LINE R"
            " ON O.PO_NO = R.PO_NO AND O.PO_LINE_NO = R.PO_LINE_NO"
            " WHERE R.RECEIPT_DATE > O.PROMISED_RECEIPT_DATE"
        ),
        aggFunction="AVG",
        executionSource=MetricExecutionSource.ESCAPE_HATCH.value,
        classRefs=(
            ("DWD_GOODS_RECEIPT_LINE", MetricSourceRole.BASE.value),
            ("DWD_PURCHASE_ORDER_LINE", MetricSourceRole.BASE.value),
        ),
        governanceNumerator=("ACTUAL_RECEIPT_DATE", "PROMISED_DATE"),
        governanceDenominator=(),
        governanceGrain=("供应商", "工厂", "月"),
        deviations=(
            "口径是差值且带「仅逾期」过滤 ⇒ 结构化词表表达不了",
            "治理列 ACTUAL_RECEIPT_DATE ↔ 物理列 THBI.DWD_GOODS_RECEIPT_LINE.RECEIPT_DATE",
            "治理列 PROMISED_DATE ↔ 物理列 THBI.DWD_PURCHASE_ORDER_LINE.PROMISED_RECEIPT_DATE",
        ),
    ),
    KpiBackfillPlan(
        kpiCode="KPI_COST_SAVING",
        metricName="KPI_COST_SAVING 采购成本节约",
        metricType=MetricType.MEASURE.value,
        aggregationOrder=MetricAggregationOrder.NONE.value,
        numerator=None,
        denominator=None,
        grainKeys=("SUPPLIER_CODE", "MATERIAL_CODE"),
        formula=(
            "SELECT SUM((P.UNIT_PRICE - O.UNIT_PRICE) * O.ORDER_QTY)"
            " FROM THBI.DWD_PURCHASE_ORDER_LINE O"
            " JOIN THBI.DWD_SUPPLIER_PRICE_LIST P"
            " ON P.MATERIAL_CODE = O.SUPPLIER_MATERIAL_CODE"
            " AND P.VALID_FROM <= O.ORDER_DATE"
            " AND (P.VALID_TO IS NULL OR P.VALID_TO >= O.ORDER_DATE)"
        ),
        aggFunction="SUM",
        executionSource=MetricExecutionSource.ESCAPE_HATCH.value,
        classRefs=(
            ("DWD_SUPPLIER_PRICE_LIST", MetricSourceRole.BASE.value),
            ("DWD_PURCHASE_ORDER_LINE", MetricSourceRole.BASE.value),
        ),
        governanceNumerator=("STANDARD_PRICE", "ACTUAL_PRICE"),
        governanceDenominator=(),
        governanceGrain=("供应商", "物料", "月"),
        deviations=(
            "口径写 STANDARD_PRICE / ACTUAL_PRICE，物理列是 "
            "THBI.DWD_SUPPLIER_PRICE_LIST.UNIT_PRICE / THBI.DWD_PURCHASE_ORDER_LINE.UNIT_PRICE",
            "口径写 QTY，物理列是 THBI.DWD_PURCHASE_ORDER_LINE.ORDER_QTY",
            "标准价与实际价的关联口径（按物料 + 有效期 + 价目表优先级）需业务确认",
        ),
    ),
    KpiBackfillPlan(
        kpiCode="KPI_PURCHASE_PRICE_VARIANCE",
        metricName="KPI_PURCHASE_PRICE_VARIANCE 采购价格偏差率",
        metricType=MetricType.RATIO.value,
        aggregationOrder=MetricAggregationOrder.AVG_OF_RATIOS.value,
        numerator=None,
        denominator=None,
        grainKeys=("MATERIAL_CODE", "FACILITY_CODE"),
        formula=(
            "SELECT AVG((O.UNIT_PRICE - P.UNIT_PRICE) / NULLIF(P.UNIT_PRICE, 0))"
            " FROM THBI.DWD_PURCHASE_ORDER_LINE O"
            " JOIN THBI.DWD_SUPPLIER_PRICE_LIST P"
            " ON P.MATERIAL_CODE = O.SUPPLIER_MATERIAL_CODE"
            " AND P.VALID_FROM <= O.ORDER_DATE"
            " AND (P.VALID_TO IS NULL OR P.VALID_TO >= O.ORDER_DATE)"
        ),
        aggFunction="AVG",
        executionSource=MetricExecutionSource.ESCAPE_HATCH.value,
        classRefs=(
            ("DWD_SUPPLIER_PRICE_LIST", MetricSourceRole.BASE.value),
            ("DWD_PURCHASE_ORDER_LINE", MetricSourceRole.BASE.value),
        ),
        governanceNumerator=("ACTUAL_PRICE", "STANDARD_PRICE"),
        governanceDenominator=("STANDARD_PRICE",),
        governanceGrain=("物料", "工厂", "月"),
        deviations=(
            "口径是比值算术式 ⇒ 结构化词表表达不了",
            "聚合阶取 AVG_OF_RATIOS（先算单行比值再平均）；若业务要「总额比」需改为 WEIGHTED_RATIO",
        ),
    ),
    KpiBackfillPlan(
        kpiCode="KPI_SUPPLIER_MAINTENANCE_RATIO",
        metricName="KPI_SUPPLIER_MAINTENANCE_RATIO 供应商资质合规率",
        metricType=MetricType.RATIO.value,
        aggregationOrder=MetricAggregationOrder.WEIGHTED_RATIO.value,
        numerator=None,
        denominator=None,
        grainKeys=("SUPPLIER_CODE",),
        formula=(
            "口径声明：有效期内资质证书齐全的供应商数 / 活跃供应商总数。"
            "技术形态待业务补：资质表 supply_quality_certificate 在 srm 数据源，"
            "与 THBI 跨库，无法写成单条可执行 SQL ⇒ execution_source = NONE。"
        ),
        aggFunction="COUNT",
        executionSource=MetricExecutionSource.NONE.value,
        classRefs=(("DIM_SUPPLIER", MetricSourceRole.BASE.value),),
        governanceNumerator=(),
        governanceDenominator=(),
        governanceGrain=("供应商（快照）",),
        deviations=(
            "资质表是 SRM 侧 supply_quality_certificate，**不在本体也不在 KPI 的 data_source 里**",
            "SUPPLY_ID ↔ THBI.DIM_SUPPLIER.BPSNUM_0 的关联键未确认",
            "THBI.DIM_SUPPLIER 的属性是 ERP 原生名（*_0 后缀），与业务名列不一致",
            "跨数据源（THBI Oracle + srm）⇒ 无法写成单条可执行 SQL ⇒ execution_source = NONE",
        ),
    ),
    KpiBackfillPlan(
        kpiCode="KPI_SUPPLIER_DEFECT_RATE",
        metricName="KPI_SUPPLIER_DEFECT_RATE 供应商来料不良率",
        metricType=MetricType.RATIO.value,
        aggregationOrder=MetricAggregationOrder.WEIGHTED_RATIO.value,
        numerator=None,
        denominator=None,
        grainKeys=("SUPPLIER_CODE", "MATERIAL_CODE", "FACILITY_CODE"),
        formula=(
            "口径声明：检验结果为 FAIL 的检验数量 / 检验总数量。"
            "技术形态待业务补：物理源 DWD_INCOMING_INSPECTION 在 schema_cache 里不存在"
            " ⇒ execution_source = NONE。"
        ),
        aggFunction="COUNT",
        executionSource=MetricExecutionSource.NONE.value,
        classRefs=(),
        governanceNumerator=("IQCRESULT",),
        governanceDenominator=(),
        governanceGrain=("供应商", "物料", "工厂", "月"),
        deviations=(
            "**物理源缺失**：DWD_INCOMING_INSPECTION 在 schema_cache 里不存在",
            "口径写 IQCRESULT，全库 0 命中；最接近的代理列是 "
            "THBI.DWD_GOODS_RECEIPT_LINE.REJECTED_QTY（代理口径需业务确认）",
        ),
    ),
    KpiBackfillPlan(
        kpiCode="KPI_SUPPLIER_FPY",
        metricName="KPI_SUPPLIER_FPY 供应商一次合格率",
        metricType=MetricType.RATIO.value,
        aggregationOrder=MetricAggregationOrder.WEIGHTED_RATIO.value,
        numerator=None,
        denominator=None,
        grainKeys=("SUPPLIER_CODE", "MATERIAL_CODE", "FACILITY_CODE"),
        formula=(
            "口径声明：首次检验即通过且无复检的批数 / 检验总批数。"
            "技术形态待业务补：物理源 DWD_INCOMING_INSPECTION 不存在"
            " ⇒ execution_source = NONE。"
        ),
        aggFunction="COUNT",
        executionSource=MetricExecutionSource.NONE.value,
        classRefs=(),
        governanceNumerator=("INSPECTION_RETRY_COUNT", "IQCRESULT"),
        governanceDenominator=(),
        governanceGrain=("供应商", "物料", "工厂", "月"),
        deviations=(
            "**物理源缺失**：DWD_INCOMING_INSPECTION 不存在",
            "口径写 IQCRESULT / INSPECTION_RETRY_COUNT，全库 0 命中",
        ),
    ),
    KpiBackfillPlan(
        kpiCode="KPI_SUPPLIER_NCR_RATE",
        metricName="KPI_SUPPLIER_NCR_RATE 供应商 NCR 率",
        metricType=MetricType.RATIO.value,
        aggregationOrder=MetricAggregationOrder.WEIGHTED_RATIO.value,
        numerator=None,
        denominator=None,
        grainKeys=("SUPPLIER_CODE", "MATERIAL_CODE", "FACILITY_CODE"),
        formula=(
            "口径声明：触发 NCR 的检验批数 / 检验总批数。"
            "技术形态待业务补：物理源 DWD_INCOMING_INSPECTION 与 DWD_NCR 都不存在"
            " ⇒ execution_source = NONE。"
        ),
        aggFunction="COUNT",
        executionSource=MetricExecutionSource.NONE.value,
        classRefs=(),
        governanceNumerator=(),
        governanceDenominator=(),
        governanceGrain=("供应商", "物料", "工厂", "月"),
        deviations=(
            "**物理源缺失**：DWD_INCOMING_INSPECTION 与 DWD_NCR 都不存在",
            "「NCR 数 / 检验批数」的去重口径需业务确认",
        ),
    ),
)

PLAN: tuple[KpiBackfillPlan, ...] = _PLAN_LIST


def buildPlans() -> tuple[KpiBackfillPlan, ...]:
    """返回 12 条映射（纯函数，供测试与 CLI 共用）。

    ⚠️ `PLAN` 是**人工核证的常量**，没有外部校验来源（不读 seed 文件 / 不查库
    生成）⇒ 「常量 vs 构造器不漂移」不是可测命题，这里只保证它是**唯一入口**。
    """
    return PLAN


def defaultMetricStatus() -> str:
    """回填产出的指标状态 —— 一律 DRAFT（发布闸门要求 PUBLISHED ⇒ 人工审核后手工发布）。"""
    return MetricStatus.DRAFT.value
