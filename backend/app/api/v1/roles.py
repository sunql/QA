"""角色管理 API（feat-rbac-identity）。

挂在 /api/v1/roles（全部 admin only —— 管理面）：
  GET    ""                       列表
  POST   ""                       创建（code='admin' 内置，禁止手动创建）
  GET    /{id}                    详情
  PUT    /{id}                    编辑（code 不可变）
  DELETE /{id}                    删除（内置 admin → 409）
  PUT    /{id}/permissions        role 维度授权菜单 set-replace
  GET    /{id}/permissions        查看角色当前授权（需求 #4）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.rbac_grant import replaceSubjectMenuGrants
from app.dependencies import CurrentUser, getAdminOnlyActor, getDb
from app.domain.enums import GrantSubjectType
from app.domain.models import Role
from app.models.rbac import ADMIN_ROLE_CODE
from app.schemas.rbac import (
    MenuCodesUpdate,
    RoleCreate,
    RoleRead,
    RoleUpdate,
    SubjectPermissionsRead,
)
from app.services.identity_service import IdentityService
from app.services.permission_service import PermissionService

router = APIRouter(prefix="/api/v1/roles", tags=["roles"])


def _idsvc() -> IdentityService:
    return IdentityService()


def _to_role_read(row: Role) -> RoleRead:
    return RoleRead(
        id=row.id,
        code=row.code,
        name=row.name,
        description=row.description,
        is_builtin=row.code == ADMIN_ROLE_CODE,
        created_time=row.created_time,
        updated_time=row.updated_time,
    )


@router.get("", response_model=list[RoleRead])
async def listRoles(
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> list[RoleRead]:
    """列出全部角色。"""
    rows = await _idsvc().list_roles(session)
    return [_to_role_read(r) for r in rows]


@router.post("", response_model=RoleRead, status_code=status.HTTP_201_CREATED)
async def createRole(
    payload: RoleCreate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> RoleRead:
    """创建角色（code 冲突 → 409；code='admin' → 422）。"""
    row = await _idsvc().create_role(session, payload, _admin)
    await session.commit()
    return _to_role_read(row)


@router.get("/{role_id}", response_model=RoleRead)
async def getRole(
    role_id: int,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> RoleRead:
    """角色详情。"""
    row = await _idsvc().get_role(session, role_id)
    return _to_role_read(row)


@router.put("/{role_id}", response_model=RoleRead)
async def updateRole(
    role_id: int,
    payload: RoleUpdate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> RoleRead:
    """编辑角色（name/description）。"""
    row = await _idsvc().update_role(session, role_id, payload, _admin)
    await session.commit()
    return _to_role_read(row)


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteRole(
    role_id: int,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> None:
    """删除角色（内置 admin → 409；级联清理授权与用户绑定）。"""
    await _idsvc().delete_role(session, role_id, _admin)
    await session.commit()


@router.put("/{role_id}/permissions", status_code=status.HTTP_204_NO_CONTENT)
async def setRoleMenuPermissions(
    role_id: int,
    payload: MenuCodesUpdate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> None:
    """角色维度授权菜单（set-replace）：持有该角色的用户都会获得这些菜单。"""
    await _idsvc().get_role(session, role_id)
    await replaceSubjectMenuGrants(
        session,
        GrantSubjectType.ROLE,
        role_id,
        payload.menu_codes,
        actor=_admin.userId,
    )
    await session.commit()


@router.get("/{role_id}/permissions", response_model=SubjectPermissionsRead)
async def getRoleMenuPermissions(
    role_id: int,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> SubjectPermissionsRead:
    """查看角色当前直接授权的菜单（需求 #4）。"""
    await _idsvc().get_role(session, role_id)
    codes = await PermissionService().listSubjectMenuCodes(
        session, GrantSubjectType.ROLE.value, role_id
    )
    return SubjectPermissionsRead(subject_id=role_id, menu_codes=codes)
