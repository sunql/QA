"""用户 / 角色 / 组织 CRUD + 关联 set-replace（feat-rbac-identity, 2026-09-07）。

职责边界：
- 用户 / 角色 / 组织增删改查 + 用户↔角色、用户↔组织 set-replace
- 删除侧的多态授权清理（permission_grant.subject_id 无物理 FK，需手动删）
- 内置 admin 角色与「最后一个 admin 用户」防护
菜单授权（set-replace）与有效权限解析见 PermissionService。
所有写操作经 OutboxService.enqueue 写审计，caller commit（同事务原子性）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import GrantSubjectType
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import (
    Organization,
    PermissionGrant,
    Role,
    User,
    UserOrganization,
    UserRole,
)
from app.models.rbac import ADMIN_ROLE_CODE
from app.schemas.rbac import (
    OrganizationCreate,
    OrganizationUpdate,
    RoleCreate,
    RoleUpdate,
    UserCreate,
    UserUpdate,
)
from app.services.outbox_service import OutboxService

logger = logging.getLogger(__name__)


def _user_payload(row: User) -> dict:
    return {
        "id": row.id,
        "username": row.username,
        "display_name": row.display_name,
        "email": row.email,
        "enabled": row.enabled,
    }


def _role_payload(row: Role) -> dict:
    return {"id": row.id, "code": row.code, "name": row.name,
            "description": row.description}


def _org_payload(row: Organization) -> dict:
    return {"id": row.id, "code": row.code, "name": row.name,
            "parent_id": row.parent_id, "description": row.description,
            "sort_order": row.sort_order}


class IdentityService:
    def __init__(
        self,
        outbox: OutboxService | None = None,
    ) -> None:
        self._outbox = outbox or OutboxService()

    # ------------------------------------------------------------------ users

    async def list_users(self, session: AsyncSession) -> list[User]:
        rows = (await session.execute(select(User).order_by(User.id))).scalars().all()
        return list(rows)

    async def get_user(self, session: AsyncSession, user_id: int) -> User:
        row = await session.get(User, user_id)
        if row is None:
            raise NotFoundError(f"用户不存在: id={user_id}")
        return row

    async def create_user(
        self, session: AsyncSession, dto: UserCreate, actor: CurrentUser
    ) -> User:
        row = User(
            username=dto.username,
            display_name=dto.display_name,
            email=dto.email,
            enabled=dto.enabled,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as e:
            await session.rollback()
            if "uq_users_username" in str(e.orig):
                raise ConflictError(f"用户名已存在: {dto.username}")
            raise
        await self._outbox.enqueue(
            session,
            event_type="user_created",
            entity_type="user",
            entity_id=row.id,
            actor=actor.userId,
            payload={"before": None, "after": _user_payload(row)},
        )
        return row

    async def update_user(
        self, session: AsyncSession, user_id: int, dto: UserUpdate, actor: CurrentUser
    ) -> User:
        row = await self.get_user(session, user_id)
        before = _user_payload(row)
        # exclude_unset 语义：只更新显式提供的字段；email 空串视为清空
        provided = dto.model_fields_set
        # 停用守卫：把持 admin 角色且是最后一个 admin 的用户不可被停用
        # （与 delete_user / set_user_roles 同一条红线：系统需保留超管入口）。
        if (
            "enabled" in provided
            and dto.enabled is False
            and row.enabled is True
            and await self._user_has_admin_role(session, user_id)
            and await self._count_admin_users(session) <= 1
        ):
            raise ConflictError("该用户是唯一 admin，不能停用（系统需保留超管入口）")
        if "display_name" in provided and dto.display_name is not None:
            row.display_name = dto.display_name
        if "email" in provided:
            row.email = dto.email or None
        if "enabled" in provided and dto.enabled is not None:
            row.enabled = dto.enabled
        row.updated_time = datetime.now(timezone.utc)
        await session.flush()
        await self._outbox.enqueue(
            session,
            event_type="user_updated",
            entity_type="user",
            entity_id=row.id,
            actor=actor.userId,
            payload={"before": before, "after": _user_payload(row)},
        )
        return row

    async def delete_user(
        self, session: AsyncSession, user_id: int, actor: CurrentUser
    ) -> None:
        row = await self.get_user(session, user_id)
        is_admin = await self._user_has_admin_role(session, user_id)
        if is_admin and await self._count_admin_users(session) <= 1:
            raise ConflictError("不能删除最后一个 admin 用户（系统需保留超管入口）")

        before = _user_payload(row)
        # 多态授权无物理 FK：手动清理该用户的直接授权
        await self._delete_subject_grants(session, GrantSubjectType.USER, user_id)
        await session.delete(row)
        await session.flush()
        await self._outbox.enqueue(
            session,
            event_type="user_deleted",
            entity_type="user",
            entity_id=user_id,
            actor=actor.userId,
            payload={"before": before, "after": None},
        )

    async def set_user_roles(
        self,
        session: AsyncSession,
        user_id: int,
        role_ids: list[int],
        actor: CurrentUser,
    ) -> None:
        """set-replace 用户角色。内置 admin 用户不能因此失去唯一超管。"""
        await self.get_user(session, user_id)
        distinct = sorted(set(role_ids))
        if distinct:
            found = (
                await session.execute(
                    select(Role.id, Role.code).where(Role.id.in_(distinct))
                )
            ).all()
            if len({rid for rid, _ in found}) != len(distinct):
                raise ValidationError("role_ids 中存在不存在的角色")
            new_codes = [code for _, code in found]
        else:
            new_codes = []
        new_has_admin = ADMIN_ROLE_CODE in new_codes
        currently_admin = await self._user_has_admin_role(session, user_id)
        if (
            currently_admin
            and not new_has_admin
            and await self._count_admin_users(session) <= 1
        ):
            raise ConflictError("该用户是唯一 admin，不能移除其 admin 角色")

        # 先删后插（幂等 set-replace）
        await session.execute(delete(UserRole).where(UserRole.user_id == user_id))
        for rid in distinct:
            session.add(UserRole(user_id=user_id, role_id=rid))
        await self._outbox.enqueue(
            session,
            event_type="user_role_updated",
            entity_type="user_role",
            entity_id=user_id,
            actor=actor.userId,
            payload={"before": None, "after": {"user_id": user_id, "role_ids": distinct}},
        )

    async def set_user_organizations(
        self,
        session: AsyncSession,
        user_id: int,
        org_ids: list[int],
        actor: CurrentUser,
    ) -> None:
        """set-replace 用户所属组织。"""
        await self.get_user(session, user_id)
        distinct = sorted(set(org_ids))
        if distinct:
            found = (
                await session.execute(
                    select(Organization.id).where(Organization.id.in_(distinct))
                )
            ).scalars().all()
            if len(set(found)) != len(distinct):
                raise ValidationError("organization_ids 中存在不存在的组织")
        await session.execute(
            delete(UserOrganization).where(UserOrganization.user_id == user_id)
        )
        for oid in distinct:
            session.add(UserOrganization(user_id=user_id, organization_id=oid))
        await self._outbox.enqueue(
            session,
            event_type="user_org_updated",
            entity_type="user_organization",
            entity_id=user_id,
            actor=actor.userId,
            payload={"before": None, "after": {"user_id": user_id, "org_ids": distinct}},
        )

    # ------------------------------------------------------------------ roles

    async def list_roles(self, session: AsyncSession) -> list[Role]:
        rows = (await session.execute(select(Role).order_by(Role.id))).scalars().all()
        return list(rows)

    async def get_role(self, session: AsyncSession, role_id: int) -> Role:
        row = await session.get(Role, role_id)
        if row is None:
            raise NotFoundError(f"角色不存在: id={role_id}")
        return row

    async def create_role(
        self, session: AsyncSession, dto: RoleCreate, actor: CurrentUser
    ) -> Role:
        if dto.code == ADMIN_ROLE_CODE:
            raise ValidationError("admin 为内置角色，请勿手动创建")
        row = Role(
            code=dto.code,
            name=dto.name,
            description=dto.description,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as e:
            await session.rollback()
            if "uq_roles_code" in str(e.orig):
                raise ConflictError(f"角色 code 已存在: {dto.code}")
            raise
        await self._outbox.enqueue(
            session,
            event_type="role_created",
            entity_type="role",
            entity_id=row.id,
            actor=actor.userId,
            payload={"before": None, "after": _role_payload(row)},
        )
        return row

    async def update_role(
        self, session: AsyncSession, role_id: int, dto: RoleUpdate, actor: CurrentUser
    ) -> Role:
        row = await self.get_role(session, role_id)
        before = _role_payload(row)
        provided = dto.model_fields_set
        if "name" in provided and dto.name is not None:
            row.name = dto.name
        if "description" in provided:
            row.description = dto.description
        row.updated_time = datetime.now(timezone.utc)
        await session.flush()
        await self._outbox.enqueue(
            session,
            event_type="role_updated",
            entity_type="role",
            entity_id=row.id,
            actor=actor.userId,
            payload={"before": before, "after": _role_payload(row)},
        )
        return row

    async def delete_role(
        self, session: AsyncSession, role_id: int, actor: CurrentUser
    ) -> None:
        row = await self.get_role(session, role_id)
        if row.code == ADMIN_ROLE_CODE:
            raise ConflictError("admin 为内置角色，禁止删除")
        before = _role_payload(row)
        await self._delete_subject_grants(session, GrantSubjectType.ROLE, role_id)
        await session.delete(row)
        await session.flush()
        await self._outbox.enqueue(
            session,
            event_type="role_deleted",
            entity_type="role",
            entity_id=role_id,
            actor=actor.userId,
            payload={"before": before, "after": None},
        )

    # ---------------------------------------------------------- organizations

    async def list_organizations(self, session: AsyncSession) -> list[Organization]:
        rows = (
            await session.execute(select(Organization).order_by(Organization.id))
        ).scalars().all()
        return list(rows)

    async def list_organizations_tree(
        self, session: AsyncSession
    ) -> list[dict]:
        """返回组织嵌套树（多根）。一次性按 (parent_id NULLS FIRST, sort_order) 排序
        后 Python 侧 O(n) 构建，n≤几百行足够。返回字典列表，每项含 children，
        由路由层映射到 OrganizationTreeNode。
        """
        rows = (
            await session.execute(
                select(Organization).order_by(
                    Organization.parent_id.nulls_first(),
                    Organization.sort_order,
                    Organization.id,
                )
            )
        ).scalars().all()
        # id -> dict（含 children 列表），先建后挂
        nodes: dict[int, dict] = {}
        for r in rows:
            nodes[r.id] = {
                "id": r.id,
                "code": r.code,
                "name": r.name,
                "sort_order": r.sort_order,
                "description": r.description,
                "children": [],
            }
        roots: list[dict] = []
        for r in rows:
            node = nodes[r.id]
            if r.parent_id is None or r.parent_id not in nodes:
                roots.append(node)
            else:
                nodes[r.parent_id]["children"].append(node)
        return roots

    async def get_organization(self, session: AsyncSession, org_id: int) -> Organization:
        row = await session.get(Organization, org_id)
        if row is None:
            raise NotFoundError(f"组织不存在: id={org_id}")
        return row

    async def create_organization(
        self, session: AsyncSession, dto: OrganizationCreate, actor: CurrentUser
    ) -> Organization:
        if dto.parent_id is not None:
            await self.get_organization(session, dto.parent_id)
        row = Organization(
            code=dto.code,
            name=dto.name,
            parent_id=dto.parent_id,
            description=dto.description,
            sort_order=dto.sort_order,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as e:
            await session.rollback()
            if "uq_organizations_code" in str(e.orig):
                raise ConflictError(f"组织 code 已存在: {dto.code}")
            raise
        await self._outbox.enqueue(
            session,
            event_type="org_created",
            entity_type="organization",
            entity_id=row.id,
            actor=actor.userId,
            payload={"before": None, "after": _org_payload(row)},
        )
        return row

    async def update_organization(
        self, session: AsyncSession, org_id: int, dto: OrganizationUpdate, actor: CurrentUser
    ) -> Organization:
        row = await self.get_organization(session, org_id)
        if dto.parent_id is not None:
            if dto.parent_id == row.id:
                raise ValidationError("组织不能以自身作为上级组织")
            # 循环依赖检测：new_parent_id 不能是 org_id 的后代（否则 A→B→A）
            if await self._wouldCreateOrgCycle(session, org_id, dto.parent_id):
                raise ValidationError("不能将组织移动到自身下级（会导致循环引用）")
            await self.get_organization(session, dto.parent_id)
        before = _org_payload(row)
        provided = dto.model_fields_set
        if "name" in provided and dto.name is not None:
            row.name = dto.name
        if "parent_id" in provided:
            row.parent_id = dto.parent_id
        if "description" in provided:
            row.description = dto.description
        if "sort_order" in provided and dto.sort_order is not None:
            row.sort_order = dto.sort_order
        row.updated_time = datetime.now(timezone.utc)
        await session.flush()
        await self._outbox.enqueue(
            session,
            event_type="org_updated",
            entity_type="organization",
            entity_id=row.id,
            actor=actor.userId,
            payload={"before": before, "after": _org_payload(row)},
        )
        return row

    async def _wouldCreateOrgCycle(
        self, session: AsyncSession, org_id: int, new_parent_id: int
    ) -> bool:
        """若 new_parent_id 是 org_id 的后代，把 org 的 parent 改成它会成环。

        用 SQL 递归 CTE：从 new_parent_id 向上追溯到根，看是否能遇到 org_id。
        仅依赖 parent_id 自引用 FK，O(深度) 一步查询，n≤几百行足够快。
        """
        cte = text(
            """
            WITH RECURSIVE ancestors AS (
                SELECT id, parent_id
                FROM organizations
                WHERE id = :new_parent_id
                UNION ALL
                SELECT o.id, o.parent_id
                FROM organizations o
                JOIN ancestors a ON o.id = a.parent_id
            )
            SELECT EXISTS (
                SELECT 1 FROM ancestors WHERE id = :org_id
            )
            """
        )
        result = await session.execute(
            cte, {"org_id": org_id, "new_parent_id": new_parent_id}
        )
        return bool(result.scalar_one())

    async def delete_organization(
        self, session: AsyncSession, org_id: int, actor: CurrentUser
    ) -> None:
        row = await self.get_organization(session, org_id)
        child = (
            await session.execute(
                select(Organization.id).where(Organization.parent_id == org_id)
            )
        ).first()
        if child is not None:
            raise ConflictError("该组织存在下级组织，请先处理下级")
        before = _org_payload(row)
        await self._delete_subject_grants(
            session, GrantSubjectType.ORGANIZATION, org_id
        )
        await session.delete(row)
        await session.flush()
        await self._outbox.enqueue(
            session,
            event_type="org_deleted",
            entity_type="organization",
            entity_id=org_id,
            actor=actor.userId,
            payload={"before": before, "after": None},
        )

    # ------------------------------------------------------------- 查询/工具

    async def load_user_roles(
        self, session: AsyncSession, user_id: int
    ) -> list[tuple[int, str, str]]:
        """返回 (role_id, code, name)，按 role_id 排序。"""
        rows = (
            await session.execute(
                select(UserRole.role_id, Role.code, Role.name)
                .join(Role, Role.id == UserRole.role_id)
                .where(UserRole.user_id == user_id)
                .order_by(Role.id)
            )
        ).all()
        return [(rid, code, name) for rid, code, name in rows]

    async def load_user_orgs(
        self, session: AsyncSession, user_id: int
    ) -> list[tuple[int, str, str]]:
        """返回 (org_id, code, name)，按 org_id 排序。"""
        rows = (
            await session.execute(
                select(UserOrganization.organization_id, Organization.code, Organization.name)
                .join(Organization, Organization.id == UserOrganization.organization_id)
                .where(UserOrganization.user_id == user_id)
                .order_by(Organization.id)
            )
        ).all()
        return [(oid, code, name) for oid, code, name in rows]

    async def _user_has_admin_role(self, session: AsyncSession, user_id: int) -> bool:
        code = (
            await session.execute(
                select(Role.code)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == user_id, Role.code == ADMIN_ROLE_CODE)
            )
        ).scalar_one_or_none()
        return code is not None

    async def _count_admin_users(self, session: AsyncSession) -> int:
        return (
            await session.execute(
                select(func.count(func.distinct(UserRole.user_id)))
                .join(Role, Role.id == UserRole.role_id)
                .where(Role.code == ADMIN_ROLE_CODE)
            )
        ).scalar_one()

    async def _delete_subject_grants(
        self, session: AsyncSession, subject_type: GrantSubjectType, subject_id: int
    ) -> None:
        await session.execute(
            delete(PermissionGrant).where(
                PermissionGrant.subject_type == subject_type.value,
                PermissionGrant.subject_id == subject_id,
            )
        )
