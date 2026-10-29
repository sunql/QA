"""S4 发布闸门（开关 ON 拦截 / OFF 观察），含开关行的可还原写。

**为什么单列一个模块**：入口有 800 行上限；S4 是唯一会临时改灰度开关的
步骤，单独成文件便于审阅「运行前后开关值一致」这条安全边界。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError
from app.domain.models import KpiCatalog
from app.services.kpi_metric_publish_gate import (
    checkKpiPublishGate,
    enforceKpiPublishGate,
)
from app.services.kpi_metric_switches import SWITCH_GATE
from scripts.kpi_metric_realdata_db import (
    fetchKpis,
    readSwitchValue,
    restoreSwitchValue,
    writeSwitchValue,
)
from scripts.kpi_metric_realdata_lib import check, info, skipStep


async def _runWithSwitch(
    session: AsyncSession,
    key: str,
    value: str,
    action: Callable[[], Awaitable[None]],
    label: str,
) -> None:
    """临时写开关 → 执行 action → **无条件还原**并核对前后值。"""
    original = await readSwitchValue(session, key)
    await writeSwitchValue(session, key, value)
    try:
        await action()
    finally:
        await restoreSwitchValue(session, key, original)
        after = await readSwitchValue(session, key)
        check(
            f"{label} 开关已还原",
            after == original,
            f"原值 {original!r} → 还原后 {after!r}",
        )
        info(f"开关对照 {key}：运行前={original!r} 临时={value!r} 还原后={after!r}")


async def _gateOn(session: AsyncSession, target: KpiCatalog) -> None:
    checkResult = await checkKpiPublishGate(
        session,
        target,
        metricId=target.metric_id,
        datasourceId=target.datasource_id,
    )
    failedMessages = [item.message for item in checkResult.failedItems]
    info(f"S4 闸门未通过项（{target.kpi_code}）：{[i.code for i in checkResult.failedItems]}")
    if not failedMessages:
        skipStep(
            "S4 发布闸门（拦截路径）",
            f"{target.kpi_code} 已满足全部闸门条件 —— 没有可拦截的样本",
        )
        return

    blocked = False
    message = ""
    try:
        await enforceKpiPublishGate(
            session,
            target,
            metricId=target.metric_id,  # type: ignore[attr-defined]
            datasourceId=target.datasource_id,  # type: ignore[attr-defined]
        )
    except ConflictError as exc:
        blocked = True
        message = str(exc)
    check(
        "S4.1 开关 ON ⇒ 服务层抛 ConflictError（被拦下）",
        blocked,
        "enforceKpiPublishGate 未抛异常" if not blocked else "",
    )
    check(
        "S4.2 错误信息带出缺项明细",
        blocked and all(item in message for item in failedMessages),
        f"failedItems={[i.code for i in checkResult.failedItems]}",
    )


async def _gateOff(session: AsyncSession, target: KpiCatalog) -> None:
    blocked = False
    try:
        await enforceKpiPublishGate(
            session,
            target,
            metricId=target.metric_id,
            datasourceId=target.datasource_id,
        )
    except ConflictError:
        blocked = True
    check(
        "S4.3 开关 OFF ⇒ 只观察不拦截",
        not blocked,
        "开关关闭时抛了 ConflictError（观察模式不应阻断）" if blocked else "",
    )


async def step4_publishGate(session: AsyncSession) -> None:
    kpis = await fetchKpis(session)
    if not kpis:
        skipStep("S4 发布闸门", "库里没有 kpi_catalog 行")
        return
    target = kpis[0]
    await _runWithSwitch(
        session,
        SWITCH_GATE,
        "true",
        lambda: _gateOn(session, target),
        "S4.4",
    )
    await _runWithSwitch(
        session,
        SWITCH_GATE,
        "false",
        lambda: _gateOff(session, target),
        "S4.5",
    )


__all__ = ["step4_publishGate"]
