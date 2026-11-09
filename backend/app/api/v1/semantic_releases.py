"""语义发布 REST 端点（P1b / D9 只读 + D10 写路径）。

路由只在 `ENABLE_SEMANTIC_RELEASE=true` 时注册（见 `main.py` / `_testapp.py`），
并整组挂在 `requireAdmin` 下：只读的是业务数据，但草稿 / 归档版本与
`created_by` / `note` 属于治理元数据，不对普通用户开放。

写端点（POST）走 D10 状态机：DRAFT → VALIDATED → ACTIVE → ARCHIVED，
非法迁移 409、发布闸门阻断 409、并发激活撞唯一索引 409。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.exceptions import ValidationError
from app.domain.semantic_release_schemas import (
    SemanticReleaseCreateRequest,
    SemanticReleaseOperationRead,
    SemanticReleasePage,
    SemanticReleaseSnapshotRead,
    SemanticReleaseSummaryRead,
)
from app.services.messages_zh import MSG_SEMANTIC_RELEASE_SCOPE_CONFLICT
from app.services.semantic_registry_service import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    MAX_RELEASE_ID,
    SemanticReleaseSnapshot,
    semanticRegistry,
)
from app.services.semantic_release_service import (
    SemanticReleaseOperation,
    semanticReleaseService,
)

_MAX_DOMAIN_CODE_LENGTH = 20  # 与 semantic_release.domain_code 列宽一致

router = APIRouter(tags=["semantic-release"])


def _noStore(response: Response) -> None:
    """治理快照不进浏览器/代理缓存（含列表页的 created_by / note）。"""
    response.headers["Cache-Control"] = "no-store"


def _toSnapshotRead(snapshot: SemanticReleaseSnapshot) -> SemanticReleaseSnapshotRead:
    return SemanticReleaseSnapshotRead(
        id=snapshot.id,
        version=snapshot.version,
        domain_code=snapshot.domain_code,
        status=snapshot.status,
        schema_version=snapshot.schema_version,
        manifest_hash=snapshot.manifest_hash,
        activated_time=snapshot.activated_time,
        manifest=snapshot.manifest,
    )


@router.get("", response_model=SemanticReleasePage)
async def listSemanticReleases(
    response: Response,
    status: str | None = Query(default=None, description="按发布状态筛选"),
    domainCode: str | None = Query(
        default=None, max_length=_MAX_DOMAIN_CODE_LENGTH, description="按数据域筛选"
    ),
    globalOnly: bool = Query(default=False, description="只看全局快照（domain_code IS NULL）"),
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(getDb),
) -> SemanticReleasePage:
    """发布列表（新→旧）。域范围三态：不传 = 全量；`globalOnly` = 全局；`domainCode` = 该域。"""
    _noStore(response)
    if globalOnly and domainCode is not None:
        raise ValidationError(MSG_SEMANTIC_RELEASE_SCOPE_CONFLICT)

    filters: dict[str, Any] = {"status": status}
    if globalOnly:
        filters["domainCode"] = None
    elif domainCode is not None:
        filters["domainCode"] = domainCode

    rows = await semanticRegistry.listReleases(session, **filters, limit=limit, offset=offset)
    total = await semanticRegistry.countReleases(session, **filters)
    return SemanticReleasePage(
        rows=[SemanticReleaseSummaryRead.model_validate(row) for row in rows],
        total=total,
    )


# `/active` 必须声明在 `/{releaseId}` 之前，否则会被当成 id 解析。
@router.get("/active", response_model=SemanticReleaseSnapshotRead | None)
async def getActiveSemanticRelease(
    response: Response,
    domainCode: str | None = Query(
        default=None, max_length=_MAX_DOMAIN_CODE_LENGTH, description="数据域；不传 = 全局快照"
    ),
    session: AsyncSession = Depends(getDb),
) -> SemanticReleaseSnapshotRead | None:
    """当前激活快照；未发布是正常状态，返回 200 + null。"""
    _noStore(response)
    snapshot = await semanticRegistry.getActive(session, domainCode=domainCode)
    return None if snapshot is None else _toSnapshotRead(snapshot)


@router.get("/{releaseId}", response_model=SemanticReleaseSnapshotRead)
async def getSemanticRelease(
    response: Response,
    releaseId: int = Path(..., ge=1, le=MAX_RELEASE_ID),
    session: AsyncSession = Depends(getDb),
) -> SemanticReleaseSnapshotRead:
    """按主键取发布详情（含完整快照）。"""
    _noStore(response)
    snapshot = await semanticRegistry.getById(session, releaseId)
    return _toSnapshotRead(snapshot)


# ---------------------------------------------------------------------------
# 写路径（D10）：状态机 DRAFT → VALIDATED → ACTIVE → ARCHIVED
# ---------------------------------------------------------------------------


def _toOperationRead(operation: SemanticReleaseOperation) -> SemanticReleaseOperationRead:
    return SemanticReleaseOperationRead(
        release=SemanticReleaseSummaryRead.model_validate(operation.release),
        blockers=list(operation.blockers),
        warnings=list(operation.warnings),
    )


@router.post("", response_model=SemanticReleaseOperationRead, status_code=201)
async def createSemanticReleaseDraft(
    response: Response,
    payload: SemanticReleaseCreateRequest,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> SemanticReleaseOperationRead:
    """把当前治理表编译成一份 DRAFT 快照（允许带发布闸门阻断项落库）。

    阻断项在这里是「预览」：DRAFT 必须如实记录被拦下的内容，评审才看得到治理债。
    """
    _noStore(response)
    operation = await semanticReleaseService.createDraft(
        session,
        version=payload.version,
        domainCode=payload.domain_code,
        actor=user.userId,
        departments=user.departments,
        note=payload.note,
    )
    return _toOperationRead(operation)


@router.post("/{releaseId}/validate", response_model=SemanticReleaseOperationRead)
async def validateSemanticRelease(
    response: Response,
    releaseId: int = Path(..., ge=1, le=MAX_RELEASE_ID),
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> SemanticReleaseOperationRead:
    """DRAFT → VALIDATED：完整性 / Schema / 发布闸门全部通过才放行。"""
    _noStore(response)
    operation = await semanticReleaseService.markValidated(
        session, releaseId, actor=user.userId, departments=user.departments
    )
    return _toOperationRead(operation)


@router.post("/{releaseId}/activate", response_model=SemanticReleaseOperationRead)
async def activateSemanticRelease(
    response: Response,
    releaseId: int = Path(..., ge=1, le=MAX_RELEASE_ID),
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> SemanticReleaseOperationRead:
    """VALIDATED → ACTIVE：同一作用域原有的激活快照在同一事务里归档。"""
    _noStore(response)
    operation = await semanticReleaseService.activate(
        session, releaseId, actor=user.userId, departments=user.departments
    )
    return _toOperationRead(operation)


@router.post("/{releaseId}/archive", response_model=SemanticReleaseOperationRead)
async def archiveSemanticRelease(
    response: Response,
    releaseId: int = Path(..., ge=1, le=MAX_RELEASE_ID),
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> SemanticReleaseOperationRead:
    """ACTIVE → ARCHIVED：人工下线（只读终态）。"""
    _noStore(response)
    operation = await semanticReleaseService.archive(
        session, releaseId, actor=user.userId, departments=user.departments
    )
    return _toOperationRead(operation)
