"""组织管理 API（feat-rbac-identity + 树形层级扩展）。

挂在 /api/v1/organizations（全部 admin only —— 管理面）：
  GET    ""                       列表（扁平）
  GET    /tree                    树形层级（多根，按 sort_order 排序）
  POST   ""                       创建（parent_id 可选，sort_order 可选）
  GET    /{id}                    详情
  PUT    /{id}                    编辑（含循环依赖检测）
  DELETE /{id}                    删除（含下级 → 409）
  PUT    /{id}/permissions        organization 维度授权菜单 set-replace
  GET    /{id}/permissions        查看组织当前授权（需求 #4）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.rbac_grant import replaceSubjectMenuGrants
from app.dependencies import CurrentUser, getAdminOnlyActor, getDb
from app.domain.enums import GrantSubjectType
from app.domain.models import Organization
from app.schemas.rbac import (
    MenuCodesUpdate,
    OrganizationCreate,
    OrganizationRead,
    OrganizationTreeNode,
    OrganizationUpdate,
    SubjectPermissionsRead,
)
from app.services.identity_service import IdentityService
from app.services.permission_service import PermissionService

router = APIRouter(prefix="/api/v1/organizations", tags=["organizations"])


def _idsvc() -> IdentityService:
    return IdentityService()


def _to_org_read(row: Organization) -> OrganizationRead:
    return OrganizationRead(
        id=row.id,
        code=row.code,
        name=row.name,
        parent_id=row.parent_id,
        description=row.description,
        sort_order=row.sort_order,
        created_time=row.created_time,
        updated_time=row.updated_time,
    )


@router.get("", response_model=list[OrganizationRead])
async def listOrganizations(
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> list[OrganizationRead]:
    """列出全部组织（扁平）。"""
    rows = await _idsvc().list_organizations(session)
    return [_to_org_read(r) for r in rows]


@router.get("/tree", response_model=list[OrganizationTreeNode])
async def listOrganizationTree(
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> list[OrganizationTreeNode]:
    """嵌套组织树（多根；同 parent 下按 sort_order 排序）。"""
    return await _idsvc().list_organizations_tree(session)


@router.post(
    "", response_model=OrganizationRead, status_code=status.HTTP_201_CREATED
)
async def createOrganization(
    payload: OrganizationCreate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> OrganizationRead:
    """创建组织（code 冲突 → 409）。"""
    row = await _idsvc().create_organization(session, payload, _admin)
    await session.commit()
    return _to_org_read(row)


@router.get("/{org_id}", response_model=OrganizationRead)
async def getOrganization(
    org_id: int,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> OrganizationRead:
    """组织详情。"""
    row = await _idsvc().get_organization(session, org_id)
    return _to_org_read(row)


@router.put("/{org_id}", response_model=OrganizationRead)
async def updateOrganization(
    org_id: int,
    payload: OrganizationUpdate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> OrganizationRead:
    """编辑组织（name/parent_id/description）。"""
    row = await _idsvc().update_organization(session, org_id, payload, _admin)
    await session.commit()
    return _to_org_read(row)


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteOrganization(
    org_id: int,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> None:
    """删除组织（含下级 → 409；级联清理授权与用户绑定）。"""
    await _idsvc().delete_organization(session, org_id, _admin)
    await session.commit()


@router.put("/{org_id}/permissions", status_code=status.HTTP_204_NO_CONTENT)
async def setOrganizationMenuPermissions(
    org_id: int,
    payload: MenuCodesUpdate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> None:
    """组织维度授权菜单（set-replace）：属于该组织的用户都会获得这些菜单。"""
    await _idsvc().get_organization(session, org_id)
    await replaceSubjectMenuGrants(
        session,
        GrantSubjectType.ORGANIZATION,
        org_id,
        payload.menu_codes,
        actor=_admin.userId,
    )
    await session.commit()


@router.get("/{org_id}/permissions", response_model=SubjectPermissionsRead)
async def getOrganizationMenuPermissions(
    org_id: int,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> SubjectPermissionsRead:
    """查看组织当前直接授权的菜单（需求 #4）。"""
    await _idsvc().get_organization(session, org_id)
    codes = await PermissionService().listSubjectMenuCodes(
        session, GrantSubjectType.ORGANIZATION.value, org_id
    )
    return SubjectPermissionsRead(subject_id=org_id, menu_codes=codes)
