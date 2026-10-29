"""KPI 指标技术形态灰度开关读取。

开关存于 system_config。支持 0/false/off/no 与 1/true/on/yes（大小写不敏感）；
其他非空值按 int 解析，大于 0 为真。缺失、非法或读取异常均返回调用方默认值，
避免配置面故障阻断 chat。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_config import SystemConfig

logger = logging.getLogger(__name__)

SWITCH_CONTRACT_INJECTION = "KPI_CONTRACT_INJECTION_ENABLED"
SWITCH_GATE = "KPI_METRIC_GATE_ENABLED"
SWITCH_TIER_A = "KPI_METRIC_TIER_A_ENABLED"

_FALSY: frozenset[str] = frozenset({"0", "false", "off", "no"})
_TRUTHY: frozenset[str] = frozenset({"1", "true", "on", "yes"})


async def readKpiMetricSwitch(
    session: AsyncSession, key: str, default: bool
) -> bool:
    """读 system_config 布尔开关；缺席/非法/异常返 default。"""
    try:
        row = await session.execute(
            select(SystemConfig).where(SystemConfig.key == key)
        )
        cfg = row.scalar_one_or_none()
    except Exception:
        logger.warning("读取开关 %s 失败，返默认值 %s", key, default, exc_info=True)
        return default
    if cfg is None:
        return default
    raw = (cfg.value or "").strip().lower()
    if not raw:
        return default
    if raw in _FALSY:
        return False
    if raw in _TRUTHY:
        return True
    try:
        return int(raw) > 0
    except ValueError:
        logger.warning("开关 %s 取值非法：%r，返默认值 %s", key, raw, default)
        return default
