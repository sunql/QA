"""Phase 4.2 KPI 业务目录种子脚本（幂等）。

12 个核心采购域 KPI（采购域 §六，Sheet 13）：

交付类（4）：OTD / Overdue Ratio / Cycle Time / Delay Days
质量类（3）：Defect Rate / FPY / NCR Rate
价格类（2）：Price Variance / Cost Saving
财务/合规（3）：Invoice Match / Payment On-Time / Maintenance Ratio

幂等：`INSERT ... ON CONFLICT (kpi_code) DO NOTHING`；冲突行由 DB 静默跳过，
重复运行不产生重复数据，并发运行也安全。

`seedKpiCatalog()` 可被集成测试导入（`backend/scripts` 为包），`main()` 供命令行运行。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.domain.enums import KpiStatus  # noqa: E402
from app.domain.models import KpiCatalog  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402

# 12 个核心 KPI 定义（每个 dict 字段与 KpiCatalog ORM 列对齐）
KPI_SEEDS: list[dict[str, Any]] = [
    # ── 交付类（4）───────────────────────────────────────────────────────
    {
        "kpi_code": "KPI_SUPPLIER_OTD",
        "kpi_name": "供应商准时交付率",
        "business_definition": "供应商在承诺交期内完成有效收货的采购订单数量占应交采购订单数量的比例。",
        "formula": "准时交付订单数 / 应交订单数",
        "numerator": "ACTUAL_RECEIPT_DATE <= PROMISED_DATE 的订单行数",
        "denominator": "PROMISED_DATE 位于统计周期内的有效采购订单行数",
        "grain": "供应商+工厂+月",
        "unit": "%",
        "data_source": "DWD_PURCHASE_ORDER + DWD_GOODS_RECEIPT",
        "owner": "采购部门",
        "version": "v1.0",
        "status": KpiStatus.PUBLISHED.value,
        "metric_id": None,
        "created_by": "seed_phase4_2",
    },
    {
        "kpi_code": "KPI_SUPPLIER_OVERDUE_RATIO",
        "kpi_name": "供应商逾期率",
        "business_definition": "承诺交期未能按期完成收货的采购订单行数占应交订单行数的比例。",
        "formula": "逾期订单行数 / 应交订单行数",
        "numerator": "ACTUAL_RECEIPT_DATE > PROMISED_DATE 且差异 > 0",
        "denominator": "PROMISED_DATE 位于统计周期内的有效采购订单行数",
        "grain": "供应商+工厂+月",
        "unit": "%",
        "data_source": "DWD_PURCHASE_ORDER + DWD_GOODS_RECEIPT",
        "owner": "采购部门",
        "version": "v1.0",
        "status": KpiStatus.PUBLISHED.value,
        "metric_id": None,
        "created_by": "seed_phase4_2",
    },
    {
        "kpi_code": "KPI_PURCHASE_CYCLE_TIME",
        "kpi_name": "采购周期",
        "business_definition": "从请购申请创建到货物入库的平均天数。",
        "formula": "AVG(GOODS_RECEIPT_DATE - PR_CREATED_DATE)",
        "numerator": "GOODS_RECEIPT_DATE - PR_CREATED_DATE 之差",
        "denominator": "N/A（聚合 AVG）",
        "grain": "供应商+工厂+月",
        "unit": "天",
        "data_source": "DWD_PURCHASE_REQUISITION + DWD_GOODS_RECEIPT",
        "owner": "采购部门",
        "version": "v1.0",
        "status": KpiStatus.PUBLISHED.value,
        "metric_id": None,
        "created_by": "seed_phase4_2",
    },
    {
        "kpi_code": "KPI_SUPPLIER_DELAY_DAYS",
        "kpi_name": "供应商平均延误天数",
        "business_definition": "逾期订单的延误天数平均值（仅统计实际逾期的订单）。",
        "formula": "AVG(ACTUAL_RECEIPT_DATE - PROMISED_DATE) WHERE 延误 > 0",
        "numerator": "ACTUAL_RECEIPT_DATE - PROMISED_DATE 之差",
        "denominator": "N/A（聚合 AVG，仅逾期）",
        "grain": "供应商+工厂+月",
        "unit": "天",
        "data_source": "DWD_PURCHASE_ORDER + DWD_GOODS_RECEIPT",
        "owner": "采购部门",
        "version": "v1.0",
        "status": KpiStatus.PUBLISHED.value,
        "metric_id": None,
        "created_by": "seed_phase4_2",
    },
    # ── 质量类（3）───────────────────────────────────────────────────────
    {
        "kpi_code": "KPI_SUPPLIER_DEFECT_RATE",
        "kpi_name": "供应商来料不良率",
        "business_definition": "来料检验中不合格数量占检验总数量的比例。",
        "formula": "不合格数量 / 检验总数量",
        "numerator": "IQCRESULT = 'FAIL' 的检验数量",
        "denominator": "IQC 检验总数量",
        "grain": "供应商+物料+工厂+月",
        "unit": "%",
        "data_source": "DWD_INCOMING_INSPECTION",
        "owner": "质量部门",
        "version": "v1.0",
        "status": KpiStatus.PUBLISHED.value,
        "metric_id": None,
        "created_by": "seed_phase4_2",
    },
    {
        "kpi_code": "KPI_SUPPLIER_FPY",
        "kpi_name": "供应商一次合格率",
        "business_definition": "首次检验即通过的检验批数占总检验批数的比例（First Pass Yield）。",
        "formula": "一次通过批数 / 总检验批数",
        "numerator": "INSPECTION_RETRY_COUNT = 0 且 IQCRESULT = 'PASS' 的批数",
        "denominator": "总检验批数",
        "grain": "供应商+物料+工厂+月",
        "unit": "%",
        "data_source": "DWD_INCOMING_INSPECTION",
        "owner": "质量部门",
        "version": "v1.0",
        "status": KpiStatus.PUBLISHED.value,
        "metric_id": None,
        "created_by": "seed_phase4_2",
    },
    {
        "kpi_code": "KPI_SUPPLIER_NCR_RATE",
        "kpi_name": "供应商 NCR 率",
        "business_definition": "来料检验触发不合格报告（Non-Conformance Report）的频次比例。",
        "formula": "NCR 数 / 检验批数",
        "numerator": "触发了 NCR 的检验批数",
        "denominator": "总检验批数",
        "grain": "供应商+物料+工厂+月",
        "unit": "%",
        "data_source": "DWD_INCOMING_INSPECTION + DWD_NCR",
        "owner": "质量部门",
        "version": "v1.0",
        "status": KpiStatus.PUBLISHED.value,
        "metric_id": None,
        "created_by": "seed_phase4_2",
    },
    # ── 价格类（2）───────────────────────────────────────────────────────
    {
        "kpi_code": "KPI_PURCHASE_PRICE_VARIANCE",
        "kpi_name": "采购价格偏差率",
        "business_definition": "实际采购价格相对标准采购价格的偏差比例（统一口径用采购合同价为基准）。",
        "formula": "(实际采购价格 - 标准采购价格) / 标准采购价格",
        "numerator": "ACTUAL_PRICE - STANDARD_PRICE",
        "denominator": "STANDARD_PRICE",
        "grain": "物料+工厂+月",
        "unit": "%",
        "data_source": "DWD_PURCHASE_ORDER + DWD_PURCHASE_CONTRACT",
        "owner": "采购部门",
        "version": "v1.0",
        "status": KpiStatus.PUBLISHED.value,
        "metric_id": None,
        "created_by": "seed_phase4_2",
    },
    {
        "kpi_code": "KPI_COST_SAVING",
        "kpi_name": "采购成本节约",
        "business_definition": "相对标准/历史基准价产生的实际成本节约金额（正数为节约，负数为超支）。",
        "formula": "SUM((STANDARD_PRICE - ACTUAL_PRICE) * QTY)",
        "numerator": "STANDARD_PRICE - ACTUAL_PRICE 之差",
        "denominator": "N/A（聚合 SUM）",
        "grain": "供应商+物料+月",
        "unit": "元",
        "data_source": "DWD_PURCHASE_ORDER + DWD_PURCHASE_CONTRACT",
        "owner": "采购部门",
        "version": "v1.0",
        "status": KpiStatus.PUBLISHED.value,
        "metric_id": None,
        "created_by": "seed_phase4_2",
    },
    # ── 财务/合规类（3）───────────────────────────────────────────────────
    {
        "kpi_code": "KPI_INVOICE_MATCH_RATE",
        "kpi_name": "发票三单匹配率",
        "business_definition": "采购订单、收货单、发票三单匹配的发票金额占总发票金额的比例。",
        "formula": "三单匹配发票金额 / 总发票金额",
        "numerator": "PO+QTY+PRICE 三单匹配的发票金额",
        "denominator": "总发票金额",
        "grain": "供应商+月",
        "unit": "%",
        "data_source": "DWD_PURCHASE_INVOICE + DWD_GOODS_RECEIPT + DWD_PURCHASE_ORDER",
        "owner": "财务部门",
        "version": "v1.0",
        "status": KpiStatus.PUBLISHED.value,
        "metric_id": None,
        "created_by": "seed_phase4_2",
    },
    {
        "kpi_code": "KPI_PAYMENT_ON_TIME",
        "kpi_name": "付款及时率",
        "business_definition": "在约定账期内完成付款的笔数占应付款笔数的比例。",
        "formula": "按时付款笔数 / 应付款笔数",
        "numerator": "PAYMENT_DATE <= DUE_DATE 的付款笔数",
        "denominator": "已到约定账期的付款笔数",
        "grain": "供应商+月",
        "unit": "%",
        "data_source": "DWD_PAYMENT",
        "owner": "财务部门",
        "version": "v1.0",
        "status": KpiStatus.PUBLISHED.value,
        "metric_id": None,
        "created_by": "seed_phase4_2",
    },
    {
        "kpi_code": "KPI_SUPPLIER_MAINTENANCE_RATIO",
        "kpi_name": "供应商资质合规率",
        "business_definition": "资质证书（ISO/质量体系/营业执照）有效期内且合规的供应商数占总供应商数的比例。",
        "formula": "资质有效供应商数 / 总供应商数",
        "numerator": "所有资质证书均在有效期且未触发的供应商数",
        "denominator": "活跃供应商总数",
        "grain": "供应商（快照）",
        "unit": "%",
        "data_source": "DIM_SUPPLIER + DWD_SUPPLIER_CERTIFICATION",
        "owner": "采购部门",
        "version": "v1.0",
        "status": KpiStatus.PUBLISHED.value,
        "metric_id": None,
        "created_by": "seed_phase4_2",
    },
]


async def seedKpiCatalog(session: Any) -> int:
    """幂等写入全部 12 条 KPI 种子；返回本次新增条数。

    用 PG `INSERT ... ON CONFLICT (kpi_code) DO NOTHING`：唯一约束
    `uq_kpi_catalog_code` 冲突的行由 DB 直接跳过，并发运行安全，无需预查重（无 TOCTOU race）。
    """
    inserted = 0
    for kpi in KPI_SEEDS:
        result = await session.execute(
            pg_insert(KpiCatalog).values(**kpi).on_conflict_do_nothing(
                index_elements=["kpi_code"]
            )
        )
        inserted += result.rowcount or 0
    await session.commit()
    return inserted


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        inserted = await seedKpiCatalog(session)
        total = (
            await session.execute(select(func.count()).select_from(KpiCatalog))
        ).scalar()
    print(
        f"[seed_kpi_catalog] 本次新增 {inserted} 条，库内共 {total} 条"
    )
    print(
        f"✅ KPI 业务目录种子完成（12 条核心 KPI：4 交付 + 3 质量 + 2 价格 + 3 财务/合规，"
        f"Phase 4.2 验收 ≥12 条已满足）"
    )


if __name__ == "__main__":
    asyncio.run(main())