"""KPI 诊断接口（admin，P0 D2）。

``GET /api/v1/admin/kpi/diagnose?question=...``

把「问句 ⇄ KPI ⇄ 技术形态 ⇄ 注入门禁」整条链路摊开给管理员：

- 匹配：v2 的 ``matchRoute``（唯一命中 / 歧义候选 / 无命中）或 v1 的
  ``matchAll`` 的 code / confidence / 命中路径（exact_code / keyword / containment）
- 对账：``reconcileMetric`` 的 verdict / degraded / Tier / findings（原因码 + 明细）
- 注入：总闸状态 + 每条是否注入 + 注入块是否为空

**不另写判定**：开关读 ``isKpiContractInjectionEnabled``，块读
``buildKpiContractBlock``，对账读 ``reconcileMetric`` —— 与 chat 注入路径同源，
诊断结果与真实注入行为不会漂移。

异常不吞：匹配 / 契约块构建故障直接 500（走 builder 的 strict 路径）。诊断接口
把错误伪装成空结果，等于让它骗人。唯一例外是注入总闸读取：与 chat 同源
（``isKpiContractInjectionEnabled``），读失败按 fail-closed 记为关闭 —— 那是
既有设计，不在本接口改动。

**同步健康段（``syncHealth``）是第二个有意例外**：它是巡检信息，不是判定信息 ——
Milvus/Neo4j 不可用时应仍能返回 PG 口径（取数失败进 ``errors``，对应计数置
None），而不是让整个诊断 500。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getDb, requireAdmin
from app.domain.models import AuditLog, KpiCatalog, OntologyMetric
from app.domain.schemas import (
    CamelModel,
    KpiGovernanceGates,
    KpiGovernanceKpiCounts,
    KpiGovernanceMetricCounts,
    KpiGovernanceReconcile,
    KpiGovernanceSummary,
    KpiGovernanceSyncHealth,
)
from app.infrastructure import neo4j_client as neo4j
from app.infrastructure.milvus_client import queryKpiEmbeddings
from app.services.kpi_alignment_resolver import ResolutionOutcome
from app.services.kpi_contract_builder import (
    KPI_CONTRACT_MAX_KPIS,
    buildKpiContractBlockWithCodes,
)
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_metric_audit import RECONCILE_ENTITY_TYPE
from app.services.kpi_metric_reconcile import (
    ReconcileResult,
    reconcileMetric,
)
from app.services.kpi_metric_spec import (
    governanceSpecOf,
    resolutionForMetric,
    techniqueOf,
)
from app.services.kpi_metric_switches import (
    SWITCH_GATE,
    isKpiContractInjectionEnabled,
    isKpiMatchContainmentEnabled,
    isKpiMatchVectorEnabled,
    isKpiMetricTierAEnabled,
    isKpiSyncEnabled,
    readKpiMetricSwitch,
)
from app.services.kpi_semantic_match_service import (
    KpiMatchResult,
    getKpiSemanticMatchService,
)

router = APIRouter(prefix="/api/v1/admin/kpi", tags=["admin-kpi"])

logger = logging.getLogger(__name__)

VERDICT_NO_METRIC = "no_metric"
EXECUTION_SOURCE_NONE = "NONE"
EXECUTION_SOURCE_COMPILED = "COMPILED"
EXECUTION_SOURCE_ESCAPE_HATCH = "ESCAPE_HATCH"

TIER_A = "A"
TIER_B = "B"
TIER_C = "C"

# 治理看板：对账统计窗口（天）与错误串截断长度。
GOVERNANCE_RECONCILE_WINDOW_DAYS = 7
GOVERNANCE_ERROR_MAX_CHARS = 200

VERDICT_MISMATCH = "mismatch"
VERDICT_PASS = "pass"
VERDICT_UNKNOWN = "unknown"


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
    # L1 语义匹配目前不打分距离（L1 只出 confidence）；字段先占位，
    # 供后续向量召回路径填真值。
    distance: float | None = None


class KpiSyncHealth(CamelModel):
    """KPI 同步健康快照（best-effort：取数失败进 errors，不 500）。

    有意偏离模块头「异常不吞」承诺：diagnose 的匹配段是故障即 500，
    而同步健康是巡检信息——Milvus/Neo4j 不可用时应仍能返回 PG 口径。
    """

    kpi_published_pg_count: int
    kpi_vector_count: int | None = None
    kpi_graph_node_count: int | None = None
    errors: list[str] = Field(default_factory=list)


class KpiDiagnoseRead(CamelModel):
    """诊断响应：问句 + 总闸状态 + 命中明细。"""

    question: str
    injection_enabled: bool
    injection_block_empty: bool
    # v2 等长跨 KPI 并列 ⇒ True：matches 是候选（不替用户选），块必为空。
    ambiguous: bool = False
    matches: list[KpiDiagnoseMatch] = Field(default_factory=list)
    sync_health: KpiSyncHealth | None = None


def _isNoneSource(metric: OntologyMetric | None) -> bool:
    if metric is None:
        return False
    return (metric.execution_source or "").strip().upper() == EXECUTION_SOURCE_NONE


def _isCompiledSource(metric: OntologyMetric | None) -> bool:
    if metric is None:
        return False
    return (
        (metric.execution_source or "").strip().upper()
        == EXECUTION_SOURCE_COMPILED
    )


def _isEscapeHatchSource(metric: OntologyMetric | None) -> bool:
    if metric is None:
        return False
    return (
        (metric.execution_source or "").strip().upper()
        == EXECUTION_SOURCE_ESCAPE_HATCH
    )


def _isExecutableSource(metric: OntologyMetric | None) -> bool:
    """COMPILED / ESCAPE_HATCH：可执行源（Tier A 直通候选）。"""
    return _isCompiledSource(metric) or _isEscapeHatchSource(metric)


def _tierOf(
    result: ReconcileResult | None, metric: OntologyMetric | None
) -> str:
    """Tier 判定（与运行时 ``planKpiExecution`` 同源）。

    无 metric、NONE 源、空或未知 source ⇒ C；COMPILED 与 ESCAPE_HATCH 同属
    可执行源：对账 ``isTierAEligible`` ⇒ A，否则 B。
    """
    if metric is None or _isNoneSource(metric):
        return TIER_C
    if not _isExecutableSource(metric):
        return TIER_C
    if result is not None and result.isTierAEligible:
        return TIER_A
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
    injected: bool,
    resolution: ResolutionOutcome | None = None,
    distance: float | None = None,
) -> KpiDiagnoseMatch:
    """单条命中：对账结果 + 是否真的进了注入块（由调用方按块成员关系传入）。"""
    result: ReconcileResult | None = None
    if metric is not None and kpi is not None:
        result = reconcileMetric(
            governanceSpecOf(kpi), techniqueOf(metric), resolution=resolution
        )
    verdict = result.verdict if result is not None else VERDICT_NO_METRIC
    # 可执行源（COMPILED / ESCAPE_HATCH）才可能直通；NONE / 空 / 未知一律 False。
    tierAEligible = (
        result.isTierAEligible if result is not None else False
    ) and _isExecutableSource(metric)
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
        tier_a_eligible=tierAEligible,
        tier=_tierOf(result, metric),
        reasons=list(result.reasons()) if result is not None else [],
        findings=_findingsOf(result),
        injected=injected,
        distance=distance,
    )


async def _kpiSyncHealth(session: AsyncSession) -> KpiSyncHealth:
    """KPI 同步健康快照（PG 口径 + Milvus 向量数 + Neo4j 节点数）。

    Milvus / Neo4j 取数失败**只记进 errors**（对应计数置 None），不抛 —— 巡检
    信息不应让诊断主流程 500。`queryKpiEmbeddings` 是**同步**函数（pymilvus
    同步 SDK）；Neo4j client 同样是同步 API ⇒ 两者都在线程里跑，不占事件循环。
    """
    errors: list[str] = []
    pgCount = int(
        (
            await session.execute(
                select(func.count(KpiCatalog.id)).where(
                    KpiCatalog.status == "PUBLISHED"
                )
            )
        ).scalar_one()
    )
    vectorCount: int | None = None
    graphCount: int | None = None
    try:
        vectorCount = len(await asyncio.to_thread(queryKpiEmbeddings))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"milvus: {exc}")
    try:
        graphCount = len(await asyncio.to_thread(neo4j.getKpiCatalogIds))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"neo4j: {exc}")
    return KpiSyncHealth(
        kpi_published_pg_count=pgCount,
        kpi_vector_count=vectorCount,
        kpi_graph_node_count=graphCount,
        errors=errors,
    )


async def _resolveKpiMatches(
    db: AsyncSession, question: str
) -> tuple[list[KpiMatchResult], bool, list[str]]:
    """匹配 + 路由判定：返回 (命中列表, 是否歧义, 建块用的 code 列表)。"""
    matcher = getKpiSemanticMatchService()
    if await isKpiMatchContainmentEnabled(db):
        route = await matcher.matchRoute(question)
        if route.kind == "none" and await isKpiMatchVectorEnabled(db):
            route = await matcher.matchRouteVector(question)
        matches = list(route.matches)
        ambiguous = route.kind == "ambiguous"
        # 歧义时不建块：matches 仍全量返回（管理员要看候选），但不据此注入。
        codes = [m.code for m in matches] if route.kind == "hit" else []
        return matches, ambiguous, codes
    matches = await matcher.matchAll(question, limit=KPI_CONTRACT_MAX_KPIS)
    return matches, False, [m.code for m in matches]


async def _buildDiagnosticMatches(
    db: AsyncSession, matches: list[KpiMatchResult], injectedCodes: set[str]
) -> list[KpiDiagnoseMatch]:
    """逐条命中展开成诊断明细。

    别名解析失败只降级为 resolution=None（恒等回退），不把诊断变成 500。
    """
    diagnosticMatches: list[KpiDiagnoseMatch] = []
    for match in matches:
        kpi = await _loadKpi(db, match.code)
        metric = await _loadMetric(db, kpi.metric_id if kpi is not None else None)
        governance = governanceSpecOf(kpi) if kpi is not None else None
        resolution = (
            await resolutionForMetric(db, metric.id, governance)
            if metric is not None and governance is not None
            else None
        )
        diagnosticMatches.append(
            _matchOf(
                kpi=kpi,
                metric=metric,
                code=match.code,
                kpiName=match.kpi_name,
                confidence=match.confidence,
                method=match.method,
                # 以**块成员关系**为准：通过逐条门禁但被字符上限丢掉的条目不算注入。
                injected=match.code in injectedCodes,
                resolution=resolution,
                # 文本命中恒为 None；向量候选带原始 Milvus L2 距离。
                distance=match.distance,
            )
        )
    return diagnosticMatches


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
    matches, ambiguous, codes = await _resolveKpiMatches(db, question)

    # 用同一份匹配结果建块：不重复匹配，也不在只读 GET 里写审计
    # （getDb 不提交 ⇒ 审计只会被回滚并留下误导性 INFO 日志）。
    block, includedCodes = await buildKpiContractBlockWithCodes(
        db,
        question,
        codes=codes,
        strict=True,
        recordAudit=False,
    )
    diagnosticMatches = await _buildDiagnosticMatches(
        db, matches, set(includedCodes)
    )

    # 同步健康是**巡检信息**：失败不阻塞诊断主流程（整体异常 ⇒ sync_health=None）。
    syncHealth: KpiSyncHealth | None = None
    try:
        syncHealth = await _kpiSyncHealth(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("KPI 同步健康快照失败: %s", exc)

    return KpiDiagnoseRead(
        question=question,
        injection_enabled=injectionEnabled,
        injection_block_empty=not block.strip(),
        ambiguous=ambiguous,
        matches=diagnosticMatches,
        sync_health=syncHealth,
    )


# ---------------------------------------------------------------------------
# 治理看板聚合（P4b/P5 Task 5）
#
# 只读、admin only。所有「外部」计数（audit_log / Milvus / Neo4j）均 best-effort：
# 失败降级成 0 / None + 错误串，绝不让看板整体 500（设计 §2.3、§4 风险表）。
# ---------------------------------------------------------------------------


def _truncateError(text: str) -> str:
    """错误串截断：响应里只带短错误，避免把堆栈/超长消息塞给前端。"""
    return text[:GOVERNANCE_ERROR_MAX_CHARS]


def _governanceError(exc: Exception) -> str:
    return _truncateError(f"{type(exc).__name__}: {exc}")


async def _countBy(
    session: AsyncSession, column: object
) -> dict[str, int]:
    """按单个列分组计数；NULL 值以空串键返回（由调用方决定桶名）。"""
    rows = (
        await session.execute(select(column, func.count()).group_by(column))
    ).all()
    buckets: dict[str, int] = {}
    for value, count in rows:
        key = "" if value is None else str(value)
        # 累加而非覆盖：NULL 组与显式空串组都映射到 ""，二者是同源语义，
        # 覆盖会静默丢掉其中一组的计数。
        buckets[key] = buckets.get(key, 0) + int(count)
    return buckets


async def _governanceKpiCounts(
    session: AsyncSession,
) -> KpiGovernanceKpiCounts:
    byStatus = await _countBy(session, KpiCatalog.status)
    return KpiGovernanceKpiCounts(
        total=sum(byStatus.values()), by_status=byStatus
    )


async def _governanceMetricCounts(
    session: AsyncSession,
) -> KpiGovernanceMetricCounts:
    byStatus = await _countBy(session, OntologyMetric.status)
    rawSources = await _countBy(session, OntologyMetric.execution_source)
    bySource: dict[str, int] = {}
    for source, count in rawSources.items():
        # 看板口径刻意比运行时**窄**：运行时用
        # `(execution_source or "").strip().upper()` 归一后判缺失
        # （kpi_execution_service），看板只把 NULL / 空串归入 NONE。
        # 非 API 写入的变体（如 " none "、"None"）各落各桶 —— 让数据面
        # 异常暴露出来，而不是在看板上悄悄合并。
        bucket = EXECUTION_SOURCE_NONE if source in (None, "") else source
        bySource[bucket] = bySource.get(bucket, 0) + count
    return KpiGovernanceMetricCounts(
        total=sum(byStatus.values()),
        by_execution_source=bySource,
        by_status=byStatus,
    )


async def _governanceReconcile(
    session: AsyncSession,
) -> KpiGovernanceReconcile:
    """近 7 天对账统计（audit_log，verdict 走 JSONB ->> 取值）。

    ``pass_count`` **不代表通过率，且会严重低估真实 pass**：P1 起
    ``recordReconcileResult`` 只在 findings 非空时写审计（避免噪声）⇒
    **干净 pass 不落库**；但 ``reconcileMetric`` 的 verdict 只取层 2，
    层 1/3 mismatch 会并入 findings 并置 ``degraded=True``，因此
    **degraded pass 会以 verdict='pass', degraded=true 落库**。本计数只是
    「窗口内实际落库的（多为 degraded）pass 行数」，干净 pass 不在其中。
    """
    windowStart = datetime.now(UTC) - timedelta(
        days=GOVERNANCE_RECONCILE_WINDOW_DAYS
    )
    try:
        # savepoint：本段失败不得把调用方事务打成 failed 态（后续还要读开关）。
        async with session.begin_nested():
            rows = (
                await session.execute(
                    select(
                        AuditLog.after_json["verdict"].astext,
                        AuditLog.created_at,
                    ).where(
                        AuditLog.entity_type == RECONCILE_ENTITY_TYPE,
                        AuditLog.created_at >= windowStart,
                    )
                )
            ).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("KPI 治理看板对账统计失败: %s", exc)
        return KpiGovernanceReconcile(
            window_days=GOVERNANCE_RECONCILE_WINDOW_DAYS,
            reconcile_error=_governanceError(exc),
        )

    mismatchCount = 0
    passCount = 0
    unknownCount = 0
    mismatchTimes: list[datetime] = []
    for verdict, createdAt in rows:
        if verdict == VERDICT_MISMATCH:
            mismatchCount += 1
            mismatchTimes.append(createdAt)
        elif verdict == VERDICT_PASS:
            passCount += 1
        elif verdict == VERDICT_UNKNOWN:
            unknownCount += 1
    return KpiGovernanceReconcile(
        window_days=GOVERNANCE_RECONCILE_WINDOW_DAYS,
        mismatch_count=mismatchCount,
        pass_count=passCount,
        unknown_count=unknownCount,
        last_mismatch_at=max(mismatchTimes) if mismatchTimes else None,
    )


async def _governanceSyncHealth(
    session: AsyncSession, kpiTotal: int
) -> KpiGovernanceSyncHealth:
    """三库同步健康；整段失败 ⇒ 三计数 None + ``syncHealthError``（不 500）。"""
    try:
        # savepoint：PG 计数失败同样不能污染调用方事务。
        async with session.begin_nested():
            health = await _kpiSyncHealth(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("KPI 治理看板同步健康快照失败: %s", exc)
        return KpiGovernanceSyncHealth(
            kpi_total=kpiTotal,
            sync_health_error=_governanceError(exc),
        )
    return KpiGovernanceSyncHealth(
        published_kpi_count=health.kpi_published_pg_count,
        milvus_kpi_count=health.kpi_vector_count,
        neo4j_kpi_count=health.kpi_graph_node_count,
        kpi_total=kpiTotal,
        # Milvus / Neo4j 单点失败由 `_kpiSyncHealth` 收集进 errors，这里透出；
        # 与整段失败同口径截断（schema 承诺「短错误串」）。
        sync_health_error=_truncateError("; ".join(health.errors)) or None,
    )


@router.get("/governance-summary", response_model=KpiGovernanceSummary)
async def governanceSummary(
    _admin: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> KpiGovernanceSummary:
    """KPI 治理看板聚合（admin only，只读，best-effort）。

    形状契约见设计稿 §2.3：

    - ``kpis`` / ``metrics``：单表 GROUP BY 计数；``metrics.byExecutionSource``
      把 NULL / 空串统一归入 ``NONE``；
    - ``reconcile``：近 7 天 ``audit_log``（entity_type 见
      ``RECONCILE_ENTITY_TYPE``）按 ``after_json->>'verdict'`` 计数，查询失败
      ⇒ 全 0 + ``reconcileError``，不 500。``passCount`` 只是「实际落库的
      （多为 degraded）pass 行数」：干净 pass 不落库，degraded pass 会落库，
      见 ``_governanceReconcile`` docstring，**不是通过率**；
    - ``syncHealth``：复用 ``_kpiSyncHealth``（PG + Milvus + Neo4j），外部计数
      失败保持 null + ``syncHealthError``；
    - ``gates``：与运行时同源的三个开关（publish gate / Tier A / sync）。
    """
    kpis = await _governanceKpiCounts(db)
    metrics = await _governanceMetricCounts(db)
    reconcile = await _governanceReconcile(db)
    syncHealth = await _governanceSyncHealth(db, kpis.total)
    gates = KpiGovernanceGates(
        publish_gate_enabled=await readKpiMetricSwitch(
            db, SWITCH_GATE, False
        ),
        tier_a_enabled=await isKpiMetricTierAEnabled(db),
        sync_enabled=await isKpiSyncEnabled(db),
    )
    return KpiGovernanceSummary(
        kpis=kpis,
        metrics=metrics,
        reconcile=reconcile,
        sync_health=syncHealth,
        gates=gates,
        generated_at=datetime.now(UTC),
    )
