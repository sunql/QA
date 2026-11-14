"""KPI 运行时 Tier 事件审计（M4 看板：Tier 分布 / 降级原因 / 路由占比）。

复用 ``audit_log`` 的 append-only 通道（与 ``kpi_metric_audit`` 同一持久化方式）：
- ``entity_type = "kpi_runtime_event"``，``action = "CREATE"``；
- ``entity_id = kpi_catalog.id``（与对账审计同口径，便于按 KPI 追溯）；
- ``after_json = {"kpi_code", "tier", "reason", "match_method"}``。

为什么落审计而不是新表：``audit_log`` 已是「事件流」的既有载体（对账结果走同一
通道），且按 ``created_at`` 有索引；新增事件类型不需要 DDL，也不改变消息持久化
契约。运行时每次 KPI 路由（A/B/C/LEGACY）写一行 —— 量级与 KPI 问句数同阶。

为什么用**独立会话**而不是请求会话：流式入口的终结分支（Tier A/C/LEGACY）只
yield SSE 帧就 ``return``，从不 commit 请求会话（``getDb`` 收尾只关闭/回滚），
请求会话里的审计行会被静默丢弃 —— 实测 Tier A 流式问句 0 行、非流式 1 行。
事件是观测数据，不该依附于业务事务的提交时机；与 H4 断连兜底同理由，多一条
连接换「必达」。写入 **best-effort**：失败只记 warning，绝不让 chat 请求失败。
"""

from __future__ import annotations

import logging
from typing import Any

from app.infrastructure import database as databaseModule
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

RUNTIME_ENTITY_TYPE = "kpi_runtime_event"
RUNTIME_ACTOR = "system:kpi-runtime"

_audit = AuditService()


def buildRuntimePayload(
    *,
    kpiCode: str,
    tier: str,
    reason: str | None,
    matchMethod: str | None,
) -> dict[str, Any]:
    """构造审计负载（纯函数，便于单测钉形状）。"""
    return {
        "kpi_code": kpiCode,
        "tier": tier,
        "reason": reason,
        "match_method": matchMethod,
    }


async def recordKpiRuntimeEvent(
    *,
    kpiId: int,
    kpiCode: str,
    tier: str,
    reason: str | None = None,
    matchMethod: str | None = None,
) -> bool:
    """追加一条运行时 Tier 事件并立即提交；失败返回 False（调用方继续回答）。"""
    try:
        # 经模块属性取工厂（而不是 import 时绑定的名字）：测试把
        # ``database._sessionFactory`` 换成测试库时，这里随之解析到测试库。
        async with databaseModule.getSessionFactory()() as ownSession:
            await _audit.record(
                ownSession,
                entity_type=RUNTIME_ENTITY_TYPE,
                entity_id=kpiId,
                action="CREATE",
                actor=RUNTIME_ACTOR,
                after=buildRuntimePayload(
                    kpiCode=kpiCode,
                    tier=tier,
                    reason=reason,
                    matchMethod=matchMethod,
                ),
            )
            # 独立会话：提交本行即落库，不依赖请求事务是否 commit。
            await ownSession.commit()
        return True
    except Exception:  # noqa: BLE001 — 观测路径，绝不上抛
        logger.warning(
            "KPI 运行时事件写入失败 code=%s tier=%s reason=%s",
            kpiCode,
            tier,
            reason,
            exc_info=True,
        )
        return False
