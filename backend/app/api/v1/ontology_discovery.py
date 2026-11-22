"""本体发现 REST 端点（WP-ONT-AI A1-4）。

前缀 `/api/v1/ontology-discovery`（设计稿草案是 `/api/ontology-discovery`，
本仓所有 v1 路由都在 `/api/v1` 下 ⇒ 对齐本仓约定，偏离已在 `plan.md` D-8 登记）。

门禁分层（与 `plan.md` D-8 一致）：

| 方法 | 路径 | 门禁 |
|---|---|---|
| POST | `/sources` | `requireAdmin`（登记授权目录） |
| PATCH | `/sources/{sourceId}` | `requireAdmin`（停用 / 改策略 = 改授权） |
| GET | `/sources` | 登录 |
| GET | `/snapshots` | 登录 |
| GET | `/snapshots/{snapshotId}` | 登录 |
| POST | `/snapshots` | `requireAdmin`（采集快照） |
| GET | `/diff` | 登录 |

写入口只写**发现域自己的表**（`discovery_source` / `metadata_snapshot`），
**不写治理表**（红线：AI / 发现域只产出候选，人工裁决，机器发布）。

所有写动作落 `audit_log`（治理动作必须留痕）。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query, Request, Response
from fastapi import status as http_status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb, requireAdmin
from app.domain.discovery_pipeline_models import (
    DiscoveryCandidate,
    DiscoveryFinding,
    DiscoveryProposal,
)
from app.domain.discovery_schemas import (
    DiscoveryCandidatePage,
    DiscoveryCandidateRead,
    DiscoveryFindingPage,
    DiscoveryFindingRead,
    DiscoveryProposalCreateRequest,
    DiscoveryProposalPage,
    DiscoveryProposalRead,
    DiscoveryPublishRead,
    DiscoveryPublishRequest,
    DiscoveryReviewRead,
    DiscoveryReviewRequest,
    DiscoveryRunRead,
    DiscoveryRunRequest,
    DiscoverySnapshotPage,
    DiscoverySnapshotRead,
    DiscoverySnapshotSummaryRead,
    DiscoverySourceCreateRequest,
    DiscoverySourcePage,
    DiscoverySourceRead,
    DiscoverySourceUpdateRequest,
    SemanticChangeRead,
    SemanticDiffRead,
    SnapshotCaptureRead,
)
from app.infrastructure.rate_limit import enforceRouteLimit
from app.services.audit_service import AuditService
from app.services.discovery_diff_service import computeOntologyDiff
from app.services.discovery_proposal_service import DiscoveryProposalService
from app.services.discovery_run_service import DiscoveryRunService
from app.services.discovery_snapshot_service import DiscoverySnapshotService
from app.services.semantic_diff_service import selectChangesForResponse
from app.services.semantic_release_service import semanticReleaseService

router = APIRouter(tags=["ontology-discovery"])

_MAX_LIMIT = 200
_MAX_OFFSET = 1_000_000
# 快照 id 是 BigIntPk（bigserial）⇒ 上界取 2**63-1。
_MAX_SNAPSHOT_ID = 2**63 - 1
# `/diff` 单次返回的变更条数上限：Manifest 允许 5000 类 + 20000 join ⇒ 全量返回
# 可达上万条（每条带 before/after）⇒ 截断 + 显式 `truncated` 标记。
_MAX_DIFF_CHANGES = 500
# 采集快照 = 连接业务库读数据字典（高成本运维动作）⇒ 收紧独立限额，
# 与 `datasource.py` 的 introspect（10/minute）对齐。
_CAPTURE_LIMIT = "10/minute"
# 限流桶的作用域键（**不用 URL 路径**：挂载前缀会变 ⇒ 路径做键会静默失效）。
_CAPTURE_LIMIT_SCOPE = "ontology-discovery:capture"

_service = DiscoverySnapshotService()
_runService = DiscoveryRunService()
_proposalService = DiscoveryProposalService()
_audit = AuditService()


def _noStore(response: Response) -> None:
    """治理元数据（授权目录 / 快照 / 差异）不进浏览器与代理缓存。"""

    response.headers["Cache-Control"] = "no-store"


def _sourcePayload(source, *, snapshotCount: int = 0) -> DiscoverySourceRead:
    return DiscoverySourceRead(
        id=source.id,
        name=source.name,
        datasource_id=source.datasource_id,
        owner=source.owner,
        access_policy=dict(source.access_policy or {}),
        status=source.status,
        note=source.note,
        created_by=source.created_by,
        created_time=source.created_time,
        snapshot_count=snapshotCount,
    )


@router.post("/sources", response_model=DiscoverySourceRead, status_code=201)
async def createDiscoverySource(
    response: Response,
    payload: DiscoverySourceCreateRequest,
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> DiscoverySourceRead:
    """登记发现源（引用既有数据源，**不新建连接**）。"""

    _noStore(response)
    source = await _service.registerSource(
        session,
        name=payload.name,
        datasourceId=payload.datasource_id,
        owner=payload.owner,
        accessPolicy=payload.access_policy,
        note=payload.note,
        createdBy=admin.userId,
    )
    await _audit.record(
        session,
        entity_type="discovery_source",
        entity_id=source.id,
        action="CREATE",
        actor=admin.userId,
        actor_departments=admin.departments or None,
        after={
            "name": source.name,
            "datasourceId": source.datasource_id,
            "owner": source.owner,
            "status": source.status,
            "accessPolicy": dict(source.access_policy or {}),
        },
    )
    await session.commit()
    return _sourcePayload(source)


@router.patch("/sources/{sourceId}", response_model=DiscoverySourceRead)
async def updateDiscoverySource(
    response: Response,
    sourceId: int,
    payload: DiscoverySourceUpdateRequest,
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> DiscoverySourceRead:
    """更新发现源（停用 / 改责任人 / 改策略 / 改备注）。

    `datasource_id` 与 `name` **不可改**：前者是授权根（改它 = 换一条被授权的
    连接）。DTO 侧 `extra="forbid"` ⇒ 传了就 422，不会被静默忽略。
    """

    _noStore(response)
    before = await _service.getSource(session, sourceId)
    beforePayload = {
        "owner": before.owner,
        "status": before.status,
        "accessPolicy": dict(before.access_policy or {}),
        "note": before.note,
    }
    changes = payload.model_dump(exclude_unset=True)
    source = await _service.updateSource(session, sourceId=sourceId, changes=changes)
    await _audit.record(
        session,
        entity_type="discovery_source",
        entity_id=source.id,
        action="UPDATE",
        actor=admin.userId,
        actor_departments=admin.departments or None,
        before=beforePayload,
        after={
            "owner": source.owner,
            "status": source.status,
            "accessPolicy": dict(source.access_policy or {}),
            "note": source.note,
        },
    )
    await session.commit()
    snapshotCount = await _service.countSnapshots(session, sourceId=source.id)
    return _sourcePayload(source, snapshotCount=snapshotCount)


@router.get("/sources", response_model=DiscoverySourcePage)
async def listDiscoverySources(
    response: Response,
    limit: int = Query(default=_MAX_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=_MAX_OFFSET),
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> DiscoverySourcePage:
    """发现源列表（附各自的快照数）。"""

    _noStore(response)
    rows = await _service.listSources(session, limit=limit, offset=offset)
    # 一次 GROUP BY 拿本页各源的计数（不做 N+1；只聚合当前页 ⇒ 不扫全表）
    counts = await _service.countSnapshotsBySource(
        session, sourceIds=[row.id for row in rows]
    )
    reads = [_sourcePayload(row, snapshotCount=counts.get(row.id, 0)) for row in rows]
    # `total` = **全量条数**（不是本页长度）⇒ 前端分页器才知道还有多少页
    total = await _service.countSources(session)
    return DiscoverySourcePage(rows=reads, total=total)


@router.get("/snapshots", response_model=DiscoverySnapshotPage)
async def listDiscoverySnapshots(
    response: Response,
    sourceId: int | None = Query(default=None, ge=1, description="按发现源过滤"),
    limit: int = Query(default=50, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=_MAX_OFFSET),
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> DiscoverySnapshotPage:
    """快照列表（新→旧）。快照**只增不改** ⇒ 列表即历史。"""

    _noStore(response)
    rows = await _service.listSnapshots(session, sourceId=sourceId, limit=limit, offset=offset)
    total = await _service.countSnapshots(session, sourceId=sourceId)
    return DiscoverySnapshotPage(
        rows=[
            DiscoverySnapshotSummaryRead.model_validate(row, from_attributes=True)
            for row in rows
        ],
        total=total,
    )


@router.get("/snapshots/{snapshotId}", response_model=DiscoverySnapshotRead)
async def getDiscoverySnapshot(
    response: Response,
    snapshotId: int = Path(..., ge=1, le=_MAX_SNAPSHOT_ID),
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> DiscoverySnapshotRead:
    """快照详情（含内容）。"""

    _noStore(response)
    row = await _service.getSnapshot(session, snapshotId)
    return DiscoverySnapshotRead(
        id=row.id,
        source_id=row.source_id,
        schema_hash=row.schema_hash,
        table_count=row.table_count,
        column_count=row.column_count,
        captured_at=row.captured_at,
        created_by=row.created_by,
        snapshot=row.snapshot_json or {},
    )


@router.post("/snapshots", response_model=SnapshotCaptureRead)
async def captureDiscoverySnapshot(
    request: Request,
    response: Response,
    sourceId: int = Query(..., ge=1, description="要采集的发现源 id"),
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> SnapshotCaptureRead:
    """采集一次快照（**内容寻址、幂等**：内容未变则复用既有行）。

    限流 10/minute：采集要连业务库读数据字典，是代价高昂的运维动作。
    """

    # 限流在**端点内**显式执行（不用 @limiter.limit 装饰器 —— 本仓装饰器登记的
    # 路由级限额不会被执行，详见 `rate_limit.enforceRouteLimit` 的说明）。
    enforceRouteLimit(request, _CAPTURE_LIMIT, _CAPTURE_LIMIT_SCOPE)

    _noStore(response)
    result = await _service.captureSnapshot(
        session, sourceId=sourceId, createdBy=admin.userId
    )
    await _audit.record(
        session,
        entity_type="metadata_snapshot",
        entity_id=result.snapshotId,
        # 复用既有行时**没有新建** ⇒ 审计动作记 REUSE（记 CREATE 会让
        # 「新增了多少行」的台账虚高）。
        action="REUSE" if result.reused else "CREATE",
        actor=admin.userId,
        actor_departments=admin.departments or None,
        after={
            "sourceId": result.sourceId,
            "schemaHash": result.schemaHash,
            "tableCount": result.tableCount,
            "columnCount": result.columnCount,
            "reused": result.reused,
        },
    )
    await session.commit()
    return SnapshotCaptureRead(
        snapshot_id=result.snapshotId,
        source_id=result.sourceId,
        schema_hash=result.schemaHash,
        table_count=result.tableCount,
        column_count=result.columnCount,
        captured_at=result.capturedAt,
        reused=result.reused,
    )


@router.get("/diff", response_model=SemanticDiffRead)
async def getOntologyDiff(
    response: Response,
    domainCode: str | None = Query(default=None, description="数据域；不传 = 全局"),
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> SemanticDiffRead:
    """基线（已发布 ACTIVE）vs 当前治理表的差异 + 破坏性分类 + 风险分级。

    覆盖度随结果披露：`covered_sections` / `uncovered_sections` / `partial`
    ⇒ 「无破坏性变更」只在该披露范围内成立。
    """

    _noStore(response)
    result = await computeOntologyDiff(session, domainCode=domainCode)
    diff = result.diff
    allChanges = diff.changes
    # 截断时**优先保留破坏性变更**（MEDIUM-2）：按原序截断会把排在后面的
    # BREAKING 丢掉 ⇒ 调用方看到「变更列表里没有破坏性变更」却 hasBreakingChanges=true。
    shown, droppedBreaking = selectChangesForResponse(allChanges, _MAX_DIFF_CHANGES)
    return SemanticDiffRead(
        baseline_release_id=result.baselineReleaseId,
        baseline_version=result.baselineVersion,
        baseline_hash=result.baselineHash,
        scope=result.scope,
        baseline_domain_code=result.baselineDomainCode,
        change_count=len(shown),
        total_change_count=len(allChanges),
        truncated=len(allChanges) > len(shown),
        dropped_breaking_count=droppedBreaking,
        breaking_count=diff.breakingCount,
        additive_count=diff.additiveCount,
        cosmetic_count=diff.cosmeticCount,
        has_breaking_changes=diff.hasBreakingChanges,
        overall_risk_level=diff.overallRiskLevel,
        covered_sections=list(diff.coveredSections),
        uncovered_sections=list(diff.uncoveredSections),
        covered_fields={
            section: list(fields)
            for section, fields in diff.coveredFields.items()
        },
        partial=diff.isPartial,
        changes=[
            SemanticChangeRead(
                kind=change.kind,
                severity=change.severity,
                risk_level=change.riskLevel,
                section=change.section,
                target=change.target,
                field=change.field,
                before=change.before,
                after=change.after,
            )
            for change in shown
        ],
    )


# =============================================================================
# A2（G3-4）：运行 / 候选 / 问题 / 提案 / 审核
# =============================================================================
#
# 门禁（追加到模块 docstring 的同一张表）：
#
# | POST `/runs` | `requireAdmin`（连业务库剖析 + 调 LLM，成本与授权动作） |
# | GET  `/runs/{runId}` | `requireAdmin`（含 error_message / 模型信息） |
# | GET  `/candidates`、`/findings` | `requireAdmin`（AI 未确认结论，普通用户不得当事实） |
# | POST `/proposals`、`/proposals/{id}/submit`、`/proposals/{id}/review` | `requireAdmin` |
# | GET  `/proposals`、`/proposals/{id}` | `requireAdmin`（未发布的治理在途件） |
#
# 后台执行：`POST /runs` 只受理（建 RUNNING 行）并返回 202；剖析与候选在
# `BackgroundTasks` 里用**自有会话**跑（见 `discovery_run_service` 的会话隔离说明）。

_MAX_RUN_LIMIT = "2/minute"
_RUN_LIMIT_SCOPE = "ontology-discovery:run"
_MAX_CANDIDATE_PAGE = 200


def _runRead(run) -> DiscoveryRunRead:
    return DiscoveryRunRead(
        id=run.id,
        source_id=run.source_id,
        snapshot_id=run.snapshot_id,
        trigger=run.trigger,
        status=run.status,
        model_version=run.model_version,
        prompt_version=run.prompt_version,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
        cost=float(run.cost or 0),
        error_message=run.error_message,
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_by=run.created_by,
    )


def _proposalRead(row) -> DiscoveryProposalRead:
    return DiscoveryProposalRead(
        id=row.id,
        title=row.title,
        source_id=row.source_id,
        base_release_id=row.base_release_id,
        release_id=row.release_id,
        diff_json=dict(row.diff_json or {}),
        impact_json=dict(row.impact_json) if row.impact_json is not None else None,
        status=row.status,
        proposer=row.proposer,
        created_time=row.created_time,
        updated_time=row.updated_time,
    )


def _reviewRead(row) -> DiscoveryReviewRead:
    return DiscoveryReviewRead(
        id=row.id,
        proposal_id=row.proposal_id,
        reviewer=row.reviewer,
        decision=row.decision,
        comment=row.comment,
        approval_task_id=row.approval_task_id,
        created_time=row.created_time,
    )


@router.post("/runs", response_model=DiscoveryRunRead, status_code=http_status.HTTP_202_ACCEPTED)
async def startDiscoveryRun(
    request: Request,
    response: Response,
    background: BackgroundTasks,
    payload: DiscoveryRunRequest,
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> DiscoveryRunRead:
    """受理一次发现运行（202）：剖析 + （默认）候选生成在后台执行。

    限流 2/minute：剖析要连业务库跑聚合、候选要调 LLM，都是高成本动作。
    """
    enforceRouteLimit(request, _MAX_RUN_LIMIT, _RUN_LIMIT_SCOPE)
    _noStore(response)
    run = await _runService.startRun(
        session,
        sourceId=payload.source_id,
        snapshotId=payload.snapshot_id,
        createdBy=admin.userId,
    )
    await _audit.record(
        session,
        entity_type="discovery_run",
        entity_id=run.id,
        action="CREATE",
        actor=admin.userId,
        actor_departments=admin.departments or None,
        after={
            "sourceId": run.source_id,
            "snapshotId": run.snapshot_id,
            "trigger": run.trigger,
            "withCandidates": payload.with_candidates,
            "tables": payload.tables,
        },
    )
    await session.commit()
    background.add_task(
        _runService.runPipeline,
        run.id,
        tables=payload.tables,
        withCandidates=payload.with_candidates,
    )
    return _runRead(run)


@router.get("/runs/{runId}", response_model=DiscoveryRunRead)
async def getDiscoveryRun(
    response: Response,
    runId: int = Path(..., ge=1),
    _admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> DiscoveryRunRead:
    _noStore(response)
    return _runRead(await _runService.getRun(session, runId))


@router.get("/candidates", response_model=DiscoveryCandidatePage)
async def listDiscoveryCandidates(
    response: Response,
    runId: int | None = Query(default=None, ge=1),
    status: str | None = Query(default=None, description="CANDIDATE / NEEDS_EVIDENCE / …"),
    limit: int = Query(default=50, ge=1, le=_MAX_CANDIDATE_PAGE),
    offset: int = Query(default=0, ge=0),
    _admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> DiscoveryCandidatePage:
    """AI 候选（**未确认结论**）⇒ 限 admin，普通用户不得当事实消费。"""
    _noStore(response)
    conditions = []
    if runId is not None:
        conditions.append(DiscoveryCandidate.run_id == runId)
    if status:
        conditions.append(DiscoveryCandidate.status == status)
    base = select(DiscoveryCandidate).where(*conditions)
    total = int(
        await session.scalar(
            select(func.count()).select_from(DiscoveryCandidate).where(*conditions)
        )
        or 0
    )
    rows = (
        await session.execute(
            base.order_by(DiscoveryCandidate.id.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return DiscoveryCandidatePage(
        rows=[
            DiscoveryCandidateRead(
                id=row.id,
                run_id=row.run_id,
                source_id=row.source_id,
                snapshot_id=row.snapshot_id,
                candidate_type=row.candidate_type,
                table_name=row.table_name,
                column_name=row.column_name,
                target_code=row.target_code,
                payload=dict(row.payload or {}),
                evidence_refs=list(row.evidence_refs or []),
                confidence=float(row.confidence) if row.confidence is not None else None,
                model_version=row.model_version,
                prompt_version=row.prompt_version,
                status=row.status,
                decided_by=row.decided_by,
                decided_time=row.decided_time,
                created_time=row.created_time,
            )
            for row in rows
        ],
        total=total,
    )


@router.get("/findings", response_model=DiscoveryFindingPage)
async def listDiscoveryFindings(
    response: Response,
    runId: int | None = Query(default=None, ge=1),
    status: str | None = Query(default=None, description="OPEN / ACKNOWLEDGED / …"),
    limit: int = Query(default=50, ge=1, le=_MAX_CANDIDATE_PAGE),
    offset: int = Query(default=0, ge=0),
    _admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> DiscoveryFindingPage:
    _noStore(response)
    conditions = []
    if runId is not None:
        conditions.append(DiscoveryFinding.run_id == runId)
    if status:
        conditions.append(DiscoveryFinding.status == status)
    total = int(
        await session.scalar(
            select(func.count()).select_from(DiscoveryFinding).where(*conditions)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(DiscoveryFinding)
            .where(*conditions)
            .order_by(DiscoveryFinding.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return DiscoveryFindingPage(
        rows=[
            DiscoveryFindingRead(
                id=row.id,
                run_id=row.run_id,
                source_id=row.source_id,
                snapshot_id=row.snapshot_id,
                finding_type=row.finding_type,
                table_name=row.table_name,
                column_name=row.column_name,
                severity=row.severity,
                status=row.status,
                detail=row.detail,
                evidence_refs=list(row.evidence_refs or []),
                created_time=row.created_time,
            )
            for row in rows
        ],
        total=total,
    )


@router.post("/proposals", response_model=DiscoveryProposalRead, status_code=201)
async def createDiscoveryProposal(
    response: Response,
    payload: DiscoveryProposalCreateRequest,
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> DiscoveryProposalRead:
    """建提案草稿（manifest 必须过契约；有基线则同时算影响面）。"""
    _noStore(response)
    proposal = await _proposalService.createProposal(
        session,
        sourceId=payload.source_id,
        title=payload.title,
        proposalManifest=payload.manifest,
        proposer=admin.userId,
        baseReleaseId=payload.base_release_id,
        note=payload.note,
    )
    await _audit.record(
        session,
        entity_type="discovery_proposal",
        entity_id=proposal.id,
        action="CREATE",
        actor=admin.userId,
        actor_departments=admin.departments or None,
        after={
            "title": proposal.title,
            "sourceId": proposal.source_id,
            "baseReleaseId": proposal.base_release_id,
            "status": proposal.status,
            "impact": proposal.impact_json,
        },
    )
    await session.commit()
    await session.refresh(proposal)
    return _proposalRead(proposal)


@router.get("/proposals", response_model=DiscoveryProposalPage)
async def listDiscoveryProposals(
    response: Response,
    status: str | None = Query(default=None),
    sourceId: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=_MAX_CANDIDATE_PAGE),
    offset: int = Query(default=0, ge=0),
    _admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> DiscoveryProposalPage:
    _noStore(response)
    conditions = []
    if status:
        conditions.append(DiscoveryProposal.status == status)
    if sourceId is not None:
        conditions.append(DiscoveryProposal.source_id == sourceId)
    total = int(
        await session.scalar(
            select(func.count()).select_from(DiscoveryProposal).where(*conditions)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(DiscoveryProposal)
            .where(*conditions)
            .order_by(DiscoveryProposal.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return DiscoveryProposalPage(
        rows=[_proposalRead(row) for row in rows], total=total
    )


@router.get("/proposals/{proposalId}", response_model=DiscoveryProposalRead)
async def getDiscoveryProposal(
    response: Response,
    proposalId: int = Path(..., ge=1),
    _admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> DiscoveryProposalRead:
    _noStore(response)
    return _proposalRead(
        await _proposalService.getProposal(session, proposalId)
    )


@router.post("/proposals/{proposalId}/submit", response_model=DiscoveryProposalRead)
async def submitDiscoveryProposal(
    response: Response,
    proposalId: int = Path(..., ge=1),
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> DiscoveryProposalRead:
    """DRAFT → IN_REVIEW（提交后只能由**别人**审核）。"""
    _noStore(response)
    proposal = await _proposalService.submitForReview(
        session, proposalId, actor=admin.userId
    )
    await _audit.record(
        session,
        entity_type="discovery_proposal",
        entity_id=proposal.id,
        action="UPDATE",
        actor=admin.userId,
        actor_departments=admin.departments or None,
        before={"status": "DRAFT"},
        after={"status": proposal.status},
    )
    await session.commit()
    await session.refresh(proposal)
    return _proposalRead(proposal)


@router.post("/proposals/{proposalId}/review", response_model=DiscoveryReviewRead, status_code=201)
async def reviewDiscoveryProposal(
    response: Response,
    payload: DiscoveryReviewRequest,
    proposalId: int = Path(..., ge=1),
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> DiscoveryReviewRead:
    """四眼审核：`reviewer != proposer`（否则 409）；结论驱动状态机。"""
    _noStore(response)
    review = await _proposalService.review(
        session,
        proposalId,
        reviewer=admin.userId,
        decision=payload.decision,
        comment=payload.comment,
        approvalTaskId=payload.approval_task_id,
    )
    proposal = await _proposalService.getProposal(session, proposalId)
    await _audit.record(
        session,
        entity_type="discovery_proposal",
        entity_id=proposal.id,
        action="CREATE",
        actor=admin.userId,
        actor_departments=admin.departments or None,
        after={
            "reviewId": review.id,
            "decision": review.decision,
            "status": proposal.status,
            "approvalTaskId": review.approval_task_id,
        },
    )
    await session.commit()
    await session.refresh(review)
    return _reviewRead(review)


@router.post(
    "/proposals/{proposalId}/publish",
    response_model=DiscoveryPublishRead,
    status_code=201,
)
async def publishDiscoveryProposal(
    response: Response,
    payload: DiscoveryPublishRequest,
    proposalId: int = Path(..., ge=1),
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> DiscoveryPublishRead:
    """发布已批准（APPROVED）的提案：DRAFT → VALIDATED → ACTIVE（**唯一写口**）。

    本端点只调 `semanticReleaseService.publishProposal` —— 发现域**不写**
    `semantic_release` 行（F1 红线）；发布后提案置 `PUBLISHED` 并回填 `release_id`。
    """
    _noStore(response)
    operation = await semanticReleaseService.publishProposal(
        session,
        proposalId=proposalId,
        version=payload.version,
        actor=admin.userId,
        domainCode=payload.domain_code,
        departments=admin.departments or None,
        note=payload.note,
    )
    proposal = await _proposalService.getProposal(session, proposalId)
    await _audit.record(
        session,
        entity_type="discovery_proposal",
        entity_id=proposal.id,
        action="UPDATE",
        actor=admin.userId,
        actor_departments=admin.departments or None,
        before={"status": "APPROVED"},
        after={
            "status": proposal.status,
            "releaseId": operation.release.id,
            "version": operation.release.version,
        },
    )
    await session.commit()
    return DiscoveryPublishRead(
        proposal_id=proposal.id,
        proposal_status=proposal.status,
        release_id=operation.release.id,
        version=operation.release.version,
        status=operation.release.status,
        blockers=list(operation.blockers),
        warnings=list(operation.warnings),
    )
