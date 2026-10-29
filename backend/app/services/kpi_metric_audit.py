"""KPI 口径对账审计落库（P1）。

仅在有 finding 时追加 audit_log；pass 不写，避免噪声。AuditService.record
不自行 commit，审计与调用方业务操作同事务。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audit_service import AuditService
from app.services.kpi_metric_reconcile import ReconcileFinding, ReconcileResult

logger = logging.getLogger(__name__)

RECONCILE_ENTITY_TYPE = "kpi_metric_reconcile"
RECONCILE_ACTOR = "system:kpi-contract"

_audit = AuditService()


def _findingToDict(finding: ReconcileFinding) -> dict[str, Any]:
    return {
        "layer": finding.layer,
        "reason": finding.reason,
        "detail": finding.detail,
        "verdict": finding.verdict,
    }


async def recordReconcileResult(
    session: AsyncSession,
    *,
    kpiId: int,
    kpiCode: str,
    result: ReconcileResult,
    actor: str = RECONCILE_ACTOR,
) -> bool:
    """追加一条对账审计；无 finding 时返回 False。"""
    if not result.findings:
        return False
    payload: dict[str, Any] = {
        "kpi_code": kpiCode,
        "verdict": result.verdict,
        "degraded": result.degraded,
        "findings": [_findingToDict(f) for f in result.findings],
    }
    await _audit.record(
        session,
        entity_type=RECONCILE_ENTITY_TYPE,
        entity_id=kpiId,
        action="CREATE",
        actor=actor,
        after=payload,
    )
    logger.info(
        "KPI 口径对账落审计 code=%s verdict=%s findings=%d",
        kpiCode,
        result.verdict,
        len(result.findings),
    )
    return True
