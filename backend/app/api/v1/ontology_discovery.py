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

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb, requireAdmin
from app.domain.discovery_schemas import (
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
from app.services.discovery_snapshot_service import DiscoverySnapshotService
from app.services.semantic_diff_service import selectChangesForResponse

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
