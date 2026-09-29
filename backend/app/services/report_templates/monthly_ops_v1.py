"""月度经营分析报告模板（monthly-ops-v1，A8）。

声明式结构（brief §实勘 2）：4 节，数据绑定零 LLM——
- 2 节走 KpiCatalogService.get_by_code（既有接口，SQL Guard 在服务内部生效）
- 2 节走 Supplier360Service.get360（既有接口）
- supplierKey 占位符整串 ``{{params.supplierKey}}``，渲染期白名单替换
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# KPI 卡片/口径说明引用的目录编码（seed_kpi_catalog.py 幂等种子集）
_MONTHLY_KPI_CODES = (
    "KPI_SUPPLIER_OTD",
    "KPI_SUPPLIER_DEFECT_RATE",
    "KPI_SUPPLIER_FPY",
    "KPI_PURCHASE_CYCLE_TIME",
)


class MonthlyOpsParams(BaseModel):
    """月度经营分析生成参数（值经 Pydantic 校验后才允许进入绑定器）。"""

    month: str = Field(
        pattern=r"^\d{4}-\d{2}$",
        description="统计月份（YYYY-MM）",
    )
    supplierKey: str = Field(
        min_length=1,
        max_length=64,
        description="供应商编码（业务码或 MDM 代理键）",
    )


TEMPLATE: dict = {
    "code": "monthly-ops-v1",
    "title": "月度经营分析报告",
    "description": "按月汇总 KPI 目录指标与指定供应商的 360° 特征值（4 节，≤2 页）",
    "paramsModel": MonthlyOpsParams,
    "sections": [
        {
            "sectionId": "kpi-overview",
            "title": "月度 KPI 总览",
            "kind": "kpi_cards",
            "source": {"type": "kpi", "kpiCodes": list(_MONTHLY_KPI_CODES)},
        },
        {
            "sectionId": "kpi-definitions",
            "title": "KPI 口径说明",
            "kind": "table",
            "source": {"type": "kpi", "kpiCodes": list(_MONTHLY_KPI_CODES)},
        },
        {
            "sectionId": "supplier-kpis",
            "title": "供应商特征值",
            "kind": "table",
            "source": {
                "type": "supplier360",
                "supplierKey": "{{params.supplierKey}}",
                "part": "kpis",
            },
        },
        {
            "sectionId": "supplier-identity",
            "title": "供应商编码映射",
            "kind": "table",
            "source": {
                "type": "supplier360",
                "supplierKey": "{{params.supplierKey}}",
                "part": "entityCodes",
            },
        },
    ],
}
