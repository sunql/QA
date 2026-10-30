"""KPI 指标技术形态灰度开关读取。

开关存于 system_config。支持 0/false/off/no 与 1/true/on/yes（大小写不敏感）；
其他非空值按 int 解析，大于 0 为真。缺失、非法或读取异常均返回调用方默认值，
避免配置面故障阻断 chat。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
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
        # savepoint：开关读取失败不得把调用方事务打成 failed 态（chat 主流程仍在用同一 session）。
        async with session.begin_nested():
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


async def isKpiContractInjectionEnabled(session: AsyncSession) -> bool:
    """KPI 契约注入总闸：**两条注入面共用一个决策**（fail-closed）。

    2026-10-30 修复的历史缺陷：同一个字符串 ``KPI_CONTRACT_INJECTION_ENABLED``
    此前被两套互不相干的机制读取 ——

    - env（``Settings.kpiContractInjectionEnabled``，默认 true）管 nl2sql 侧的
      B3 治理契约块（``kpi_contract_reconcile.buildKpiContractBlock``）；
    - system_config 行管 chat 侧的 Tier B 签字口径块
      （``kpi_contract_builder.buildKpiContractBlock``）。

    于是「先关后开」把 system_config 置 false 只关掉了 Tier B 那扇门，B3 那扇
    仍默认开着 ⇒ 灰度前置状态（全部关闭）从未真正生效。

    现在的语义（两条面都必须通过才注入）：

    1. env 紧急杀闸：``KPI_CONTRACT_INJECTION_ENABLED=false`` ⇒ 一律不注入
       （进程级、需重启生效；默认 true ⇒ 平时不干预，只作「一键止血」）。
    2. system_config 灰度开关：行值为真才开。**行缺失 / 非法 / 读取异常 ⇒ 关**
       （fail-closed：注入会改变 LLM prompt，默认关更安全；显式置 true 即开启）。

    注意 env 与 system_config **同名但不同源**：env 只作杀闸，置 true 不会打开
    灰度开关；system_config 才是灰度旋钮。

    开关**恒定读同一个 key**（``SWITCH_CONTRACT_INJECTION``）——不开放换 key
    的参数，否则「两条面共用一个决策」的不变量可被调用方悄悄打破。
    """
    if not getSettings().kpiContractInjectionEnabled:
        # DEBUG 而非 INFO：这是每请求都会走到的路径，INFO 会刷屏
        logger.debug("KPI 契约注入 env 紧急杀闸关闭：两条注入面一律不注入")
        return False
    return await readKpiMetricSwitch(session, SWITCH_CONTRACT_INJECTION, False)
