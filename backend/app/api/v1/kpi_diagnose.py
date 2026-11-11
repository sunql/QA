"""KPI 诊断接口（admin，P0 D2）。

``GET /api/v1/admin/kpi/diagnose?question=...``

把「问句 ⇄ KPI ⇄ 技术形态 ⇄ 注入门禁」整条链路摊开给管理员：

- 匹配：``matchAll`` 的 code / confidence / 命中路径（exact_code / keyword）
- 对账：``reconcileMetric`` 的 verdict / degraded / Tier / findings（原因码 + 明细）
- 注入：总闸状态 + 每条是否注入 + 注入块是否为空

**不另写判定**：开关读 ``isKpiContractInjectionEnabled``，块读
``buildKpiContractBlock``，对账读 ``reconcileMetric`` —— 与 chat 注入路径同源，
诊断结果与真实注入行为不会漂移。

异常不吞：DB/缓存故障直接 500。诊断接口把错误伪装成空结果，等于让它骗人。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getDb, requireAdmin
from app.domain.models import KpiCatalog, OntologyMetric
from app.domain.schemas import CamelModel
from app.services.kpi_contract_builder import (
    KPI_CONTRACT_MAX_KPIS,
    buildKpiContractBlock,
)
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_metric_reconcile import (
    VERDICT_MISMATCH,
    ReconcileResult,
    reconcileMetric,
)
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf
from app.services.kpi_metric_switches import isKpiContractInjectionEnabled
from app.services.kpi_semantic_match_service import getKpiSemanticMatchService

router = APIRouter(prefix="/api/v1/admin/kpi", tags=["admin-kpi"])

VERDICT_NO_METRIC = "no_metric"
EXECUTION_SOURCE_NONE = "NONE"

TIER_A = "A"
TIER_B = "B"
TIER_C = "C"


class KpiDiagnoseFinding(CamelModel):
    """单条对账发现（原因码 + 人类可读明细）。"""

    layer: str
    reason: str
    detail: str
    verdict: str


class KpiDiagnoseMatch(CamelModel):
    """单个命中 KPI 的诊断结果。"""

    kpi_code: str
    kpi_name: str | None = None
    confidence: float
    match_method: str
    metric_id: int | None = None
    metric_name: str | None = None
    metric_type: str | None = None
    execution_source: str | None = None
    verdict: str
    degraded: bool = False
    tier_a_eligible: bool = False
    tier: str
    reasons: list[str] = Field(default_factory=list)
    findings: list[KpiDiagnoseFinding] = Field(default_factory=list)
    injected: bool = False


class KpiDiagnoseRead(CamelModel):
    """诊断响应：问句 + 总闸状态 + 命中明细。"""

    question: str
    injection_enabled: bool
    injection_block_empty: bool
    matches: list[KpiDiagnoseMatch] = Field(default_factory=list)


def _isNoneSource(metric: OntologyMetric | None) -> bool:
    if metric is None:
        return False
    return (metric.execution_source or "").strip().upper() == EXECUTION_SOURCE_NONE


def _tierOf(
    result: ReconcileResult | None, metric: OntologyMetric | None
) -> str:
    """Tier A = pass 且层 1/3 无降级；无 metric 或 NONE ⇒ C；其余 B。"""
    if result is not None and result.isTierAEligible:
        return TIER_A
    if metric is None or _isNoneSource(metric):
        return TIER_C
    return TIER_B


def _findingsOf(result: ReconcileResult | None) -> list[KpiDiagnoseFinding]:
    if result is None:
        return []
    return [
        KpiDiagnoseFinding(
            layer=finding.layer,
            reason=finding.reason,
            detail=finding.detail,
            verdict=finding.verdict,
        )
        for finding in result.findings
    ]


async def _loadKpi(session: AsyncSession, kpiCode: str) -> KpiCatalog | None:
    row = await session.execute(
        select(KpiCatalog).where(KpiCatalog.kpi_code == kpiCode)
    )
    return row.scalar_one_or_none()


async def _loadMetric(
    session: AsyncSession, metricId: int | None
) -> OntologyMetric | None:
    if metricId is None:
        return None
    return await session.get(OntologyMetric, metricId)


def _matchOf(
    *,
    kpi: KpiCatalog | None,
    metric: OntologyMetric | None,
    code: str,
    kpiName: str | None,
    confidence: float,
    method: str,
    injectionEnabled: bool,
) -> KpiDiagnoseMatch:
    """单条命中：对账 + 注入门禁（与 ``_buildEntry`` 同口径）。"""
    result: ReconcileResult | None = None
    if metric is not None and kpi is not None:
        result = reconcileMetric(governanceSpecOf(kpi), techniqueOf(metric))
    verdict = result.verdict if result is not None else VERDICT_NO_METRIC
    injected = (
        injectionEnabled and metric is not None and verdict != VERDICT_MISMATCH
    )
    return KpiDiagnoseMatch(
        kpi_code=code,
        kpi_name=kpi.kpi_name if kpi is not None else kpiName,
        confidence=confidence,
        match_method=method,
        metric_id=metric.id if metric is not None else None,
        metric_name=metric.metric_name if metric is not None else None,
        metric_type=metric.metric_type if metric is not None else None,
        execution_source=metric.execution_source if metric is not None else None,
        verdict=verdict,
        degraded=result.degraded if result is not None else False,
        tier_a_eligible=result.isTierAEligible if result is not None else False,
        tier=_tierOf(result, metric),
        reasons=list(result.reasons()) if result is not None else [],
        findings=_findingsOf(result),
        injected=injected,
    )


@router.get("/diagnose", response_model=KpiDiagnoseRead)
async def diagnoseKpi(
    question: str = Query(..., min_length=1, max_length=500),
    _admin: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> KpiDiagnoseRead:
    """诊断一个问句的 KPI 匹配与注入结果（admin only）。"""
    injectionEnabled = await isKpiContractInjectionEnabled(db)

    # 与 chat 注入路径一致：匹配前必须确保缓存新鲜，否则语义匹配静默失效。
    # 这里**不做 savepoint 兜底**：诊断接口要让缓存/DB 故障暴露（500），
    # 不能像 chat 主流程那样降级成「无命中」。
    await get_kpi_match_cache().ensureFresh(db)
    matches = await getKpiSemanticMatchService().matchAll(
        question, limit=KPI_CONTRACT_MAX_KPIS
    )

    diagnosticMatches: list[KpiDiagnoseMatch] = []
    for match in matches:
        kpi = await _loadKpi(db, match.code)
        metric = await _loadMetric(db, kpi.metric_id if kpi is not None else None)
        diagnosticMatches.append(
            _matchOf(
                kpi=kpi,
                metric=metric,
                code=match.code,
                kpiName=match.kpi_name,
                confidence=match.confidence,
                method=match.method,
                injectionEnabled=injectionEnabled,
            )
        )

    block = await buildKpiContractBlock(db, question)
    return KpiDiagnoseRead(
        question=question,
        injection_enabled=injectionEnabled,
        injection_block_empty=not block.strip(),
        matches=diagnosticMatches,
    )
