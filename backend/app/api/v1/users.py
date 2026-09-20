"""用户管理 API（feat-rbac-identity）。

挂在 /api/v1/users（全部 admin only —— 管理面）：
  GET    ""                        列表（含角色/组织 code）
  POST   ""                        创建
  GET    /{id}                     详情
  PUT    /{id}                     编辑
  DELETE /{id}                     删除（最后一个 admin 用户 → 409）
  PUT    /{id}/roles               set-replace 用户角色
  PUT    /{id}/organizations       set-replace 用户组织
  PUT    /{id}/permissions         直接授权菜单 set-replace
  GET    /{id}/permissions         有效权限（需求 #4：合集 + 来源拆分）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.rbac_grant import replaceSubjectMenuGrants
from app.dependencies import CurrentUser, getAdminOnlyActor, getCurrentUser, getDb
from app.domain.enums import GrantSubjectType
from app.domain.models import Organization, Role, User, UserOrganization, UserRole
from app.schemas.auth import AdminResetPasswordRequest
from app.schemas.rbac import (
    GrantSource,
    MenuCodesUpdate,
    OrganizationIdsUpdate,
    RoleIdsUpdate,
    UserCreate,
    UserEffectivePermissionsRead,
    UserMeRead,
    UserRead,
    UserUpdate,
)
from app.services.auth_service import AuthService
from app.services.identity_service import IdentityService
from app.services.menu_config_service import MenuConfigService
from app.services.permission_service import EffectivePermissions, PermissionService

router = APIRouter(prefix="/api/v1/users", tags=["users"])


def _idsvc() -> IdentityService:
    return IdentityService()


async def _assoc_maps(
    session: AsyncSession, user_ids: list[int]
) -> tuple[dict[int, tuple[list[int], list[str]]], dict[int, tuple[list[int], list[str]]]]:
    """批量加载 user → (role_ids, role_codes) / (org_ids, org_codes)，避免 N+1。"""
    if not user_ids:
        return {}, {}
    role_rows = (
        await session.execute(
            select(UserRole.user_id, UserRole.role_id, Role.code)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id.in_(user_ids))
        )
    ).all()
    org_rows = (
        await session.execute(
            select(
                UserOrganization.user_id,
                UserOrganization.organization_id,
                Organization.code,
            )
            .join(Organization, Organization.id == UserOrganization.organization_id)
            .where(UserOrganization.user_id.in_(user_ids))
        )
    ).all()
    roles: dict[int, tuple[list[int], list[str]]] = {u: ([], []) for u in user_ids}
    orgs: dict[int, tuple[list[int], list[str]]] = {u: ([], []) for u in user_ids}
    for uid, rid, code in role_rows:
        ids, codes = roles[uid]
        ids.append(rid)
        codes.append(code)
    for uid, oid, code in org_rows:
        ids, codes = orgs[uid]
        ids.append(oid)
        codes.append(code)
    return roles, orgs


def _to_user_read(
    row, roles: tuple[list[int], list[str]], orgs: tuple[list[int], list[str]]
) -> UserRead:
    return UserRead(
        id=row.id,
        username=row.username,
        display_name=row.display_name,
        email=row.email,
        enabled=row.enabled,
        role_ids=roles[0],
        role_codes=roles[1],
        organization_ids=orgs[0],
        organization_codes=orgs[1],
        created_time=row.created_time,
        updated_time=row.updated_time,
    )


@router.get("", response_model=list[UserRead])
async def listUsers(
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> list[UserRead]:
    """列出全部用户（含角色/组织）。"""
    rows = await _idsvc().list_users(session)
    user_ids = [r.id for r in rows]
    roles, orgs = await _assoc_maps(session, user_ids)
    return [_to_user_read(r, roles[r.id], orgs[r.id]) for r in rows]


# 注意：必须注册在 /{user_id} 之前。个人中心页（/profile）用。
@router.get("/me", response_model=UserMeRead)
async def getCurrentUserInfo(
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> UserMeRead:
    """当前调用方身份（非 admin-only）。

    - X-User-Id 命中 DB 用户 → displayName/email 取 users 行，
      roles/departments 已由 getCurrentUser 以 DB 为准富化；
    - 桩回退 → dbUserId=null，displayName 回退 userId。
    """
    display_name = user.userId
    email: str | None = None
    if user.dbUserId is not None:
        row = await session.get(User, user.dbUserId)
        if row is not None:
            display_name = row.display_name
            email = row.email
    return UserMeRead(
        user_id=user.userId,
        display_name=display_name,
        email=email,
        role_codes=list(user.roles or ()),
        department_codes=list(user.departments or ()),
        db_user_id=user.dbUserId,
    )


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def createUser(
    payload: UserCreate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> UserRead:
    """创建用户（用户名冲突 → 409）。"""
    row = await _idsvc().create_user(session, payload, _admin)
    await session.commit()
    return _to_user_read(row, ([], []), ([], []))


@router.get("/{user_id}", response_model=UserRead)
async def getUser(
    user_id: int,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> UserRead:
    """用户详情（含角色/组织）。"""
    row = await _idsvc().get_user(session, user_id)
    svc = _idsvc()
    roles = await svc.load_user_roles(session, row.id)
    orgs = await svc.load_user_orgs(session, row.id)
    return _to_user_read(
        row,
        ([rid for rid, _, _ in roles], [code for _, code, _ in roles]),
        ([oid for oid, _, _ in orgs], [code for _, code, _ in orgs]),
    )


@router.put("/{user_id}", response_model=UserRead)
async def updateUser(
    user_id: int,
    payload: UserUpdate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> UserRead:
    """编辑用户（displayName/email/enabled）。"""
    row = await _idsvc().update_user(session, user_id, payload, _admin)
    await session.commit()
    svc = _idsvc()
    roles = await svc.load_user_roles(session, row.id)
    orgs = await svc.load_user_orgs(session, row.id)
    return _to_user_read(
        row,
        ([rid for rid, _, _ in roles], [code for _, code, _ in roles]),
        ([oid for oid, _, _ in orgs], [code for _, code, _ in orgs]),
    )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteUser(
    user_id: int,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> None:
    """删除用户（最后一个 admin → 409）。"""
    await _idsvc().delete_user(session, user_id, _admin)
    await session.commit()


@router.put("/{user_id}/roles", status_code=status.HTTP_204_NO_CONTENT)
async def setUserRoles(
    user_id: int,
    payload: RoleIdsUpdate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> None:
    """授予角色（set-replace）。"""
    await _idsvc().set_user_roles(session, user_id, payload.role_ids, _admin)
    await session.commit()


@router.put("/{user_id}/organizations", status_code=status.HTTP_204_NO_CONTENT)
async def setUserOrganizations(
    user_id: int,
    payload: OrganizationIdsUpdate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> None:
    """分配到组织（set-replace）。"""
    await _idsvc().set_user_organizations(
        session, user_id, payload.organization_ids, _admin
    )
    await session.commit()


@router.put("/{user_id}/permissions", status_code=status.HTTP_204_NO_CONTENT)
async def setUserMenuPermissions(
    user_id: int,
    payload: MenuCodesUpdate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> None:
    """直接授权菜单（user 维度，set-replace）。"""
    await _idsvc().get_user(session, user_id)
    await replaceSubjectMenuGrants(
        session,
        GrantSubjectType.USER,
        user_id,
        payload.menu_codes,
        actor=_admin.userId,
    )
    await session.commit()


def _to_effective_read(eff: EffectivePermissions) -> UserEffectivePermissionsRead:
    return UserEffectivePermissionsRead(
        user_id=eff.user_id,
        is_superuser=eff.is_superuser,
        role_codes=list(eff.role_codes),
        organization_codes=list(eff.organization_codes),
        menu_codes=list(eff.menu_codes),
        direct_grants=list(eff.direct_menu_codes),
        role_grants=[
            GrantSource(
                subject_id=g.subject_id,
                code=g.code,
                name=g.name,
                menu_codes=list(g.menu_codes),
            )
            for g in eff.role_grants
        ],
        organization_grants=[
            GrantSource(
                subject_id=g.subject_id,
                code=g.code,
                name=g.name,
                menu_codes=list(g.menu_codes),
            )
            for g in eff.organization_grants
        ],
    )


@router.get("/{user_id}/permissions", response_model=UserEffectivePermissionsRead)
async def getUserEffectivePermissions(
    user_id: int,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> UserEffectivePermissionsRead:
    """查看用户当前有效权限（需求 #4）：三维来源合集 + 逐来源拆分。"""
    await _idsvc().get_user(session, user_id)
    visible = await MenuConfigService(session).leaf_codes(only_visible=True)
    eff = await PermissionService().computeEffective(
        session, user_id, all_menu_codes=visible
    )
    return _to_effective_read(eff)


# 注意：必须注册在 /{user_id} 之前。admin 重置密码端点（feat-user-auth）。
@router.put("/{user_id}/password", status_code=204)
async def adminResetPassword(
    user_id: int,
    payload: AdminResetPasswordRequest,
    actor: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> Response:
    """admin 重置用户密码（feat-user-auth）。

    - admin only（``getAdminOnlyActor`` 强制 roles 含 admin）
    - 自动吊销目标用户所有未过期 session（即时踢出，无需等 JWT TTL 过期）
    - ``force_change_on_next_login`` 控制是否下次登录强制改密
    """
    await AuthService.admin_reset_password(
        target_user_id=user_id,
        payload=payload,
        actor=actor,
        session=session,
    )
    return Response(status_code=204)
