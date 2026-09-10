"""Task 1.5 KPI semantic index 种子脚本（幂等）。

12 个核心采购域 KPI 的 L1 语义匹配关键词：

交付类（4）：OTD / Overdue Ratio / Cycle Time / Delay Days
质量类（3）：Defect Rate / FPY / NCR Rate
价格类（2）：Price Variance / Cost Saving
财务/合规（3）：Invoice Match / Payment On-Time / Maintenance Ratio

幂等：UPDATE 限定 status=PUBLISHED，重复运行覆盖旧值。
DRAFT/DEPRECATED 不被触碰。

语义关键词设计原则：
- 中文 2-4 字 ngram + 英文短语
- 不填停用词
- 单 KPI 适量（4-7 个），防止退化
- 跨 KPI 有适度区分度

依赖 scripts/seed_kpi_catalog.py 先运行（12 条 PUBLISHED KPI 须先存在）。
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import update  # noqa: E402

from app.domain.models import KpiCatalog  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402

# 12 个 KPI 的语义关键词（字段与 KpiCatalog ORM 列对齐）
# 仅更新 status=PUBLISHED 的 KPI；DRAFT/DEPRECATED 不在范围内
KPI_SEMANTIC_INDEX: list[dict[str, Any]] = [
    # ── 交付类（4）───────────────────────────────────────────────────────
    {
        "kpi_code": "KPI_SUPPLIER_OTD",
        "semantic_keywords": [
            "准时交付",
            "OTD",
            "按时交付",
            "准时率",
            "交付率",
            "on time delivery",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_SUPPLIER_OVERDUE_RATIO",
        "semantic_keywords": [
            "逾期率",
            "逾期",
            "逾期订单",
            "overdue",
            "delayed",
            "延迟交付",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_PURCHASE_CYCLE_TIME",
        "semantic_keywords": [
            "采购周期",
            "交期",
            "交货周期",
            "lead time",
            "采购交期",
            "delivery cycle",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_SUPPLIER_DELAY_DAYS",
        "semantic_keywords": [
            "延误天数",
            "平均延误",
            "delay days",
            "逾期天数",
            "延误",
            "delivery delay",
        ],
        "match_threshold": Decimal("0.75"),
    },
    # ── 质量类（3）───────────────────────────────────────────────────────
    {
        "kpi_code": "KPI_SUPPLIER_DEFECT_RATE",
        "semantic_keywords": [
            "不良率",
            "来料不良",
            "缺陷率",
            "defect rate",
            "quality defect",
            "iqc",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_SUPPLIER_FPY",
        "semantic_keywords": [
            "一次合格率",
            "FPY",
            "first pass yield",
            "首检通过",
            "一次通过率",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_SUPPLIER_NCR_RATE",
        "semantic_keywords": [
            "NCR",
            "不合格报告",
            "non-conformance",
            "来料异常",
            "质量异常",
            "NCR rate",
        ],
        "match_threshold": Decimal("0.75"),
    },
    # ── 价格类（2）───────────────────────────────────────────────────────
    {
        "kpi_code": "KPI_PURCHASE_PRICE_VARIANCE",
        "semantic_keywords": [
            "价格偏差",
            "价格差异",
            "price variance",
            "采购价格",
            "价格波动",
            "price deviation",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_COST_SAVING",
        "semantic_keywords": [
            "成本节约",
            "cost saving",
            "节支",
            "成本节省",
            "采购节支",
            "cost reduction",
        ],
        "match_threshold": Decimal("0.75"),
    },
    # ── 财务/合规类（3）───────────────────────────────────────────────────
    {
        "kpi_code": "KPI_INVOICE_MATCH_RATE",
        "semantic_keywords": [
            "三单匹配",
            "发票匹配",
            "invoice match",
            "票货核对",
            "invoice reconciliation",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_PAYMENT_ON_TIME",
        "semantic_keywords": [
            "付款及时率",
            "按时付款",
            "payment on time",
            "付款率",
            "账期",
            "准时付款",
        ],
        "match_threshold": Decimal("0.75"),
    },
    {
        "kpi_code": "KPI_SUPPLIER_MAINTENANCE_RATIO",
        "semantic_keywords": [
            "资质合规",
            "合规率",
            "certificate",
            "资质证书",
            "供应商资质",
            "compliance",
        ],
        "match_threshold": Decimal("0.75"),
    },
]


async def seedKpiSemanticIndex(session: Any) -> int:
    """幂等写入 12 条 KPI 的 semantic_keywords；返回 affected 行数。

    使用 UPDATE 限定 status=PUBLISHED：
    - 仅更新已发布的 KPI（DRAFT 不被触碰）
    - 重复运行覆盖旧值（幂等）
    - 不依赖 INSERT 故无需处理 NOT NULL 列问题
    """
    affected = 0
    for spec in KPI_SEMANTIC_INDEX:
        stmt = (
            update(KpiCatalog)
            .where(KpiCatalog.kpi_code == spec["kpi_code"])
            .where(KpiCatalog.status == "PUBLISHED")
            .values(
                semantic_keywords=spec["semantic_keywords"],
                match_threshold=spec["match_threshold"],
            )
        )
        result = await session.execute(stmt)
        affected += result.rowcount or 0
    await session.commit()
    return affected


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        affected = await seedKpiSemanticIndex(session)
    print(
        f"[seed_kpi_semantic_index] affected={affected} 条 "
        f"（12 KPI semantic index，Task 1.5 验收）"
    )
    print("✅ KPI semantic index 种子完成（12 条：4 交付 + 3 质量 + 2 价格 + 3 财务/合规）")


if __name__ == "__main__":
    asyncio.run(main())
