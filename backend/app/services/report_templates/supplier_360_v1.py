"""供应商 360° 报告模板（supplier-360-v1，A8）。

声明式结构（brief §实勘 2）：4 节——
- 3 节走 Supplier360Service.get360（profile / entityCodes / kpis 三个 part）
- 1 节走 KpiCatalogService.get_by_code（该供应商相关 KPI 的目录口径）
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 供应商风险评估相关的 KPI 目录编码（与 supplier-360 特征面呼应）
_SUPPLIER_KPI_CODES = (
    "KPI_SUPPLIER_OTD",
    "KPI_SUPPLIER_DELAY_DAYS",
    "KPI_SUPPLIER_NCR_RATE",
    "KPI_SUPPLIER_OVERDUE_RATIO",
)


class Supplier360Params(BaseModel):
    """供应商 360° 报告生成参数。"""

    supplierKey: str = Field(
        min_length=1,
        max_length=64,
        description="供应商编码（业务码或 MDM 代理键）",
    )


TEMPLATE: dict = {
    "code": "supplier-360-v1",
    "title": "供应商 360° 报告",
    "description": "单供应商档案、编码映射、特征值与相关 KPI 口径（4 节，≤2 页）",
    "paramsModel": Supplier360Params,
    "sections": [
        {
            "sectionId": "supplier-profile",
            "title": "供应商档案",
            "kind": "table",
            "source": {
                "type": "supplier360",
                "supplierKey": "{{params.supplierKey}}",
                "part": "profile",
            },
        },
        {
            "sectionId": "supplier-entity-codes",
            "title": "跨系统编码映射",
            "kind": "table",
            "source": {
                "type": "supplier360",
                "supplierKey": "{{params.supplierKey}}",
                "part": "entityCodes",
            },
        },
        {
            "sectionId": "supplier-kpi-values",
            "title": "特征值最新结果",
            "kind": "kpi_cards",
            "source": {
                "type": "supplier360",
                "supplierKey": "{{params.supplierKey}}",
                "part": "kpis",
            },
        },
        {
            "sectionId": "supplier-kpi-catalog",
            "title": "相关 KPI 口径",
            "kind": "kpi_cards",
            "source": {"type": "kpi", "kpiCodes": list(_SUPPLIER_KPI_CODES)},
        },
    ],
}
