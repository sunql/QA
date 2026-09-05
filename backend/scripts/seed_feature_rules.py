"""幂等 upsert 4 个内置 Feature Rule（feat-feature-rule-config, 2026-09-05）。

RISK_SCORE 三档（0.60 / 0.80 / 1.01）保证 byte-identical 字节级与 legacy
_decideLevel 兼容（spec §10.2 + §10.4）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import FeatureRule
from app.services.feature_rule_service import FeatureRuleService

logger = logging.getLogger(__name__)


FEATURE_RULE_SEEDS: list[dict[str, Any]] = [
    {
        "code": "supplier_risk_score_main",
        "data_object": "SUPPLIER",
        "data_layer": "FEATURE",
        "target_level": "RISK",
        "feature_name": "SUPPLIER_RISK_SCORE",
        "thresholds": [
            {"severity": "HIGH", "operator": "lt_inverse", "threshold_value": 0.60},
            {"severity": "MEDIUM", "operator": "lt_inverse", "threshold_value": 0.80},
            {"severity": "LOW", "operator": "lt_inverse", "threshold_value": 1.01},
        ],
    },
    {
        "code": "supplier_otd_high_risk",
        "data_object": "SUPPLIER",
        "data_layer": "FEATURE",
        "target_level": "RISK",
        "feature_name": "SUPPLIER_OTD_3M",
        "thresholds": [{"severity": "HIGH", "operator": "lt", "threshold_value": 90, "unit": "%"}],
    },
    {
        "code": "supplier_defect_high_risk",
        "data_object": "SUPPLIER",
        "data_layer": "FEATURE",
        "target_level": "RISK",
        "feature_name": "SUPPLIER_DEFECT_RATE_3M",
        "thresholds": [{"severity": "HIGH", "operator": "gt", "threshold_value": 5, "unit": "%"}],
    },
    {
        "code": "supplier_price_var_high_risk",
        "data_object": "SUPPLIER",
        "data_layer": "FEATURE",
        "target_level": "RISK",
        "feature_name": "SUPPLIER_PRICE_VARIANCE_3M",
        "thresholds": [{"severity": "HIGH", "operator": "gt", "threshold_value": 10, "unit": "%"}],
    },
]


async def seedFeatureRules(session: AsyncSession) -> int:
    """幂等 upsert 4 个内置规则。返回改动行数（用于 lifespan 日志）。"""
    service = FeatureRuleService()
    changed = 0
    for seed in FEATURE_RULE_SEEDS:
        existing = (
            await session.execute(
                select(FeatureRule).where(FeatureRule.code == seed["code"])
            )
        ).scalar_one_or_none()
        # seed 幂等：首次插入计 changed=1；后续 upsert 不计
        await service.upsertSeed(session, seed["code"], seed)
        changed += 1 if existing is None else 0
    if changed:
        await session.flush()
        logger.info("seedFeatureRules: %d/%d rules created", changed, len(FEATURE_RULE_SEEDS))
    return changed


async def main() -> None:
    """Standalone 入口：连真实 PG 并跑一次 seed（运维 / CI 用）。"""
    from app.infrastructure.database import getSessionFactory
    from app.tests import _pg_support  # noqa: F401  # 仅触发 settings env loading

    factory = getSessionFactory()
    async with factory() as session:
        count = await seedFeatureRules(session)
        await session.commit()
        print(f"seed_feature_rules: {count}/{len(FEATURE_RULE_SEEDS)} created")


if __name__ == "__main__":
    asyncio.run(main())
