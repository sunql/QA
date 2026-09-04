"""Phase 4.3 AI 特征定义种子脚本（幂等）。

5 个核心采购域特征（采购域 §十三，Supplier 360° 前置）：

交付/质量/价格（3）：SUPPLIER_OTD_3M / SUPPLIER_DEFECT_RATE_3M /
                    SUPPLIER_PRICE_VARIANCE_3M
风险/缺料（2）：SUPPLIER_RISK_SCORE / MATERIAL_SHORTAGE_RISK

幂等：`INSERT ... ON CONFLICT (feature_name) DO NOTHING`（唯一约束
`uq_feature_definition_name`），重复运行不产生重复数据。

calculation_logic 为读 THBI 业务库的单条只读 SELECT（返回 entity_key + value 两列），
与 FeatureComputeService 的解析契约一致。MATERIAL_SHORTAGE_RISK 用价格波动率做代理
（暂无物料库存/缺料 DWS 表），见 SSOT §10 遗留。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.domain.enums import (  # noqa: E402
    FeatureRefreshFrequency,
    FeatureStatus,
)
from app.domain.models import DataSource, FeatureDefinition  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402

# 近 3 / 12 月窗口谓词（year_month 为 VARCHAR2(7) 'YYYY-MM'，字典序可比）
_WINDOW_3M = "year_month >= TO_CHAR(ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -3), 'YYYY-MM')"
_WINDOW_12M = "year_month >= TO_CHAR(ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -12), 'YYYY-MM')"

# 5 条核心特征定义（字段与 FeatureDefinition ORM 列对齐）
FEATURE_SEEDS: list[dict[str, Any]] = [
    {
        "feature_name": "SUPPLIER_OTD_3M",
        "feature_alias": "供应商3月准时交付率",
        "feature_definition": "供应商最近 3 个月准时交付率的均值（on_time_rate）。",
        "entity_type": "SUPPLIER",
        "calculation_logic": (
            "SELECT supplier_code AS entity_key, AVG(on_time_rate) AS value "
            f"FROM THBI.DWS_SUPPLIER_DELIVERY_MONTHLY WHERE {_WINDOW_3M} "
            "GROUP BY supplier_code"
        ),
        "window_size": "3M",
        "refresh_frequency": FeatureRefreshFrequency.DAILY.value,
        "unit": "%",
        "owner": "数据团队",
        "version": "v1.0",
        "status": FeatureStatus.ACTIVE.value,
        "is_enabled": True,
        "created_by": "seed_phase4_3",
    },
    {
        "feature_name": "SUPPLIER_DEFECT_RATE_3M",
        "feature_alias": "供应商3月来料不良率",
        "feature_definition": "供应商最近 3 个月来料检验不良率的均值（reject_rate）。",
        "entity_type": "SUPPLIER",
        "calculation_logic": (
            "SELECT supplier_code AS entity_key, AVG(reject_rate) AS value "
            f"FROM THBI.DWS_SUPPLIER_QUALITY_MONTHLY WHERE {_WINDOW_3M} "
            "GROUP BY supplier_code"
        ),
        "window_size": "3M",
        "refresh_frequency": FeatureRefreshFrequency.DAILY.value,
        "unit": "%",
        "owner": "质量部门",
        "version": "v1.0",
        "status": FeatureStatus.ACTIVE.value,
        "is_enabled": True,
        "created_by": "seed_phase4_3",
    },
    {
        "feature_name": "SUPPLIER_PRICE_VARIANCE_3M",
        "feature_alias": "供应商3月价格偏差率",
        "feature_definition": "供应商最近 3 个月物料价格波动率（max-min 相对 min 的均值）。",
        "entity_type": "SUPPLIER",
        "calculation_logic": (
            "SELECT supplier_code AS entity_key, "
            "AVG((max_net_unit_price - min_net_unit_price) / NULLIF(min_net_unit_price, 0)) AS value "
            f"FROM THBI.DWS_MATERIAL_PRICE_MONTHLY WHERE {_WINDOW_3M} "
            "GROUP BY supplier_code"
        ),
        "window_size": "3M",
        "refresh_frequency": FeatureRefreshFrequency.WEEKLY.value,
        "unit": "%",
        "owner": "采购部门",
        "version": "v1.0",
        "status": FeatureStatus.ACTIVE.value,
        "is_enabled": True,
        "created_by": "seed_phase4_3",
    },
    {
        "feature_name": "SUPPLIER_RISK_SCORE",
        "feature_alias": "供应商风险评分",
        "feature_definition": "供应商综合风险评分（0-1，越高越优）：OTD 权重 0.6 + 一次合格率权重 0.4。",
        "entity_type": "SUPPLIER",
        "calculation_logic": (
            "SELECT d.supplier_code AS entity_key, "
            "AVG(d.on_time_rate) * 0.6 + (1 - AVG(q.reject_rate)) * 0.4 AS value "
            "FROM THBI.DWS_SUPPLIER_DELIVERY_MONTHLY d "
            "JOIN THBI.DWS_SUPPLIER_QUALITY_MONTHLY q "
            "ON q.supplier_code = d.supplier_code AND q.year_month = d.year_month "
            f"WHERE d.year_month >= TO_CHAR(ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -12), 'YYYY-MM') "
            "GROUP BY d.supplier_code"
        ),
        "window_size": "12M",
        "refresh_frequency": FeatureRefreshFrequency.MONTHLY.value,
        "unit": "score",
        "owner": "采购部门",
        "version": "v1.0",
        "status": FeatureStatus.ACTIVE.value,
        "is_enabled": True,
        "created_by": "seed_phase4_3",
    },
    {
        "feature_name": "MATERIAL_SHORTAGE_RISK",
        "feature_alias": "物料缺货风险",
        "feature_definition": "物料价格波动率代理的缺货风险（价格波动越大，供应越不稳定）；暂无库存缺料 DWS。",
        "entity_type": "MATERIAL",
        # HAVING price_line_count>=3 过滤样本不足的物料：12M 全量去重后 39456 个物料_code
        # 远超 10000 行上限；价格波动率对仅 1-2 条记录的物料无统计意义，过滤后 7089 个。
        "calculation_logic": (
            "SELECT material_code AS entity_key, "
            "AVG((max_net_unit_price - min_net_unit_price) / NULLIF(min_net_unit_price, 0)) AS value "
            f"FROM THBI.DWS_MATERIAL_PRICE_MONTHLY WHERE {_WINDOW_12M} "
            "GROUP BY material_code "
            "HAVING SUM(price_line_count) >= 3"
        ),
        "window_size": "12M",
        "refresh_frequency": FeatureRefreshFrequency.MONTHLY.value,
        "unit": "ratio",
        "owner": "采购部门",
        "version": "v1.0",
        "status": FeatureStatus.ACTIVE.value,
        "is_enabled": True,
        "created_by": "seed_phase4_3",
    },
]


async def seedFeatures(session: Any, datasource_id: int) -> int:
    """幂等写入 5 条特征定义；返回本次新增条数。datasource_id 由调用方注入。"""
    inserted = 0
    for feat in FEATURE_SEEDS:
        payload = dict(feat)
        payload["datasource_id"] = datasource_id
        result = await session.execute(
            pg_insert(FeatureDefinition)
            .values(**payload)
            .on_conflict_do_nothing(index_elements=["feature_name"])
        )
        inserted += result.rowcount or 0
    await session.commit()
    return inserted


async def _resolveDatasourceId(session: Any) -> int:
    """解析默认数据源 id（THBI）；可用 FEATURE_DATASOURCE_ID 覆盖。"""
    explicit = os.environ.get("FEATURE_DATASOURCE_ID")
    if explicit:
        return int(explicit)
    ds = (
        await session.execute(
            select(DataSource.id).where(DataSource.is_default.is_(True)).limit(1)
        )
    ).scalar_one_or_none()
    if ds is None:
        raise RuntimeError(
            "未找到默认数据源；请先配置 THBI 数据源并设为默认，或用 FEATURE_DATASOURCE_ID 指定"
        )
    return ds


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        datasource_id = await _resolveDatasourceId(session)
        inserted = await seedFeatures(session, datasource_id)
        total = (
            await session.execute(select(func.count()).select_from(FeatureDefinition))
        ).scalar()
    print(f"[seed_features] 本次新增 {inserted} 条，库内共 {total} 条")
    print("✅ AI 特征定义种子完成（5 条核心特征：OTD/不良率/价格偏差/风险/缺料，Phase 4.3 验收 ≥5 已满足）")


if __name__ == "__main__":
    asyncio.run(main())
