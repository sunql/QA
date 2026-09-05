"""DB-backed Feature Rule 注册表（spec §6.1）。

- warmUp: lifespan 调用，bulk-load enabled=True + 阈值档
- getEnabledRules(scope_tuple): sync 快路径；未 warmed → RuntimeError
- invalidate(rule_id|None): 写时失效（sync，不查 DB）
- reloadOne(session, rule_id): async，asyncio.Lock 防并发 reloadOne 竞态
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import FeatureRule

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeatureRuleReady:
    """Registry 内部就绪结构（避免 ORM 持有 session）。"""
    id: int
    code: str
    data_object: str
    data_layer: str
    target_level: str
    feature_name: str
    enabled: bool
    priority: int
    thresholds: tuple  # tuple[FeatureThresholdReady, ...]


@dataclass(frozen=True)
class FeatureThresholdReady:
    severity: str
    operator: str
    threshold_value: object  # Decimal
    unit: str | None
    threshold_order: int


class FeatureRuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[tuple[str, str, str], list[FeatureRuleReady]] = {}
        self._loaded: bool = False
        self._lock = asyncio.Lock()

    async def warmUp(self, session: AsyncSession) -> None:
        rows = (
            await session.execute(
                select(FeatureRule).where(FeatureRule.enabled.is_(True))
            )
        ).scalars().all()
        # 防御：mock 测试可能绕过 SQL filter；client-side 再 filter 一遍
        rows = [r for r in rows if r.enabled]
        rule_ids = [r.id for r in rows]
        thresholds_by_rule: dict[int, list[FeatureThresholdReady]] = {}
        if rule_ids:
            from app.domain.models import FeatureRuleThreshold
            t_rows = (
                await session.execute(
                    select(FeatureRuleThreshold).where(
                        FeatureRuleThreshold.rule_id.in_(rule_ids)
                    )
                )
            ).scalars().all()
            for t in t_rows:
                thresholds_by_rule.setdefault(t.rule_id, []).append(FeatureThresholdReady(
                    severity=t.severity,
                    operator=t.operator,
                    threshold_value=t.threshold_value,
                    unit=t.unit,
                    threshold_order=t.threshold_order,
                ))

        async with self._lock:
            self._rules = {}
            for r in rows:
                key = (r.data_object, r.data_layer, r.target_level)
                ready = FeatureRuleReady(
                    id=r.id, code=r.code, data_object=r.data_object,
                    data_layer=r.data_layer, target_level=r.target_level,
                    feature_name=r.feature_name, enabled=r.enabled,
                    priority=r.priority,
                    thresholds=tuple(thresholds_by_rule.get(r.id, [])),
                )
                self._rules.setdefault(key, []).append(ready)
            self._loaded = True
        logger.info("FeatureRuleRegistry warmed up: %d rules", sum(len(v) for v in self._rules.values()))

    def getEnabledRules(
        self, data_object: str, data_layer: str, target_level: str
    ) -> list[FeatureRuleReady]:
        if not self._loaded:
            raise RuntimeError("FeatureRuleRegistry 未 warmUp（lifespan bug）")
        return list(self._rules.get((data_object, data_layer, target_level), []))

    def invalidate(self, rule_id: int | None = None) -> None:
        """写时失效（同步快路径）。未 warmed 时为 no-op。
        v1：简化策略——任何失效清空整个索引，下次查询触发 reloadOne。
        """
        if not self._loaded:
            return
        self._rules.clear()

    async def reloadOne(self, session: AsyncSession, rule_id: int) -> None:
        async with self._lock:
            row = (
                await session.execute(
                    select(FeatureRule).where(FeatureRule.id == rule_id)
                )
            ).scalar_one_or_none()
            if row is None or not row.enabled:
                self._rules.clear()
                return
            # Fetch thresholds for this one rule only; inline the single-rule merge.
            from app.domain.models import FeatureRuleThreshold
            t_rows = (
                await session.execute(
                    select(FeatureRuleThreshold).where(
                        FeatureRuleThreshold.rule_id == rule_id
                    )
                )
            ).scalars().all()
            thresholds = tuple(FeatureThresholdReady(
                severity=t.severity, operator=t.operator,
                threshold_value=t.threshold_value, unit=t.unit,
                threshold_order=t.threshold_order,
            ) for t in t_rows)
            ready = FeatureRuleReady(
                id=row.id, code=row.code, data_object=row.data_object,
                data_layer=row.data_layer, target_level=row.target_level,
                feature_name=row.feature_name, enabled=row.enabled,
                priority=row.priority, thresholds=thresholds,
            )
            key = (row.data_object, row.data_layer, row.target_level)
            # Remove the old version of this rule from the bucket (by id), then add new.
            bucket = self._rules.get(key, [])
            self._rules[key] = [r for r in bucket if r.id != rule_id] + [ready]


feature_rule_registry = FeatureRuleRegistry()
