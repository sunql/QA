"""菜单权限解析服务（feat-rbac-identity, 2026-09-07）。

语义：某用户的有效菜单权限 = 直接授权（subject USER=该用户）∪
其所有角色授权（subject ROLE=角色）∪ 其所有组织授权（subject ORGANIZATION=
组织）。三者合集；`admin` 角色为超管，旁路合集直接获得全部 grantable 菜单。

解析逻辑集中在纯函数 `_composeEffective`（无 DB 依赖，可独立单测）；DB 加载
是一层薄封装（`computeEffective`）。

数据权限扩展预留：资源解析骨架（subject 模型 + 合集语义）与资源类型解耦——
未来把「menu_code」换成本体「类/属性/指标」资源码、并新增 data_permission_grant
即可复用本模块与 PermissionResourceType（domain/enums.py）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import GrantSubjectType
from app.domain.models import (
    Organization,
    PermissionGrant,
    Role,
    UserOrganization,
    UserRole,
)
from app.models.rbac import ADMIN_ROLE_CODE
from app.services.outbox_service import OutboxService

_GRANT_SUBJECT_VALUES: frozenset[str] = frozenset(t.value for t in GrantSubjectType)


@dataclass(frozen=True)
class SubjectGrantView:
    """有效权限拆分的单一来源（一个角色 / 一个组织）。"""

    subject_id: int
    code: str
    name: str
    menu_codes: tuple[str, ...]


@dataclass(frozen=True)
class EffectivePermissions:
    """某用户的有效菜单权限（合集）+ 逐来源拆分（需求 #4 视图的数据底座）。"""

    user_id: int
    is_superuser: bool
    role_codes: tuple[str, ...]
    organization_codes: tuple[str, ...]
    # 有效权限合集；is_superuser=True 时为调用方提供的全部 grantable 菜单
    menu_codes: tuple[str, ...]
    direct_menu_codes: tuple[str, ...]
    role_grants: tuple[SubjectGrantView, ...]
    organization_grants: tuple[SubjectGrantView, ...]


def _sorted(codes: frozenset[str]) -> tuple[str, ...]:
    return tuple(sorted(codes))


def _composeEffective(
    user_id: int,
    *,
    role_meta: dict[int, tuple[str, str]],
    organization_meta: dict[int, tuple[str, str]],
    role_grants: dict[int, frozenset[str]],
    organization_grants: dict[int, frozenset[str]],
    direct_codes: frozenset[str],
    all_menu_codes: frozenset[str],
) -> EffectivePermissions:
    """纯函数：由预先加载的数据计算有效权限（无 IO，便于单测）。

    参数：
        role_meta：role_id -> (code, name)，仅含用户持有角色
        organization_meta：organization_id -> (code, name)，仅含用户所属组织
        role_grants / organization_grants：role_id/org_id -> 该主体授权菜单码集
        direct_codes：用户直接授权码集
        all_menu_codes：grantable 全量菜单码（admin 旁路时作为合集）

    返回集合均按码排序（确定性输出）。
    """
    is_superuser = any(code == ADMIN_ROLE_CODE for code, _ in role_meta.values())
    role_views = tuple(
        SubjectGrantView(
            subject_id=sid,
            code=role_meta[sid][0],
            name=role_meta[sid][1],
            menu_codes=_sorted(role_grants.get(sid, frozenset())),
        )
        for sid in sorted(role_meta)
    )
    org_views = tuple(
        SubjectGrantView(
            subject_id=sid,
            code=organization_meta[sid][0],
            name=organization_meta[sid][1],
            menu_codes=_sorted(organization_grants.get(sid, frozenset())),
        )
        for sid in sorted(organization_meta)
    )

    # 合集只统计用户「持有」的主体授权（meta 即持有关系；grants 传入的
    # 额外主体一并忽略，防止数据错位把非本人授权并入合集）。
    if is_superuser:
        union: frozenset[str] = frozenset(all_menu_codes)
    else:
        union = frozenset(direct_codes)
        for sid, codes in role_grants.items():
            if sid in role_meta:
                union |= codes
        for sid, codes in organization_grants.items():
            if sid in organization_meta:
                union |= codes

    return EffectivePermissions(
        user_id=user_id,
        is_superuser=is_superuser,
        role_codes=_sorted(frozenset(code for code, _ in role_meta.values())),
        organization_codes=_sorted(frozenset(code for code, _ in organization_meta.values())),
        menu_codes=_sorted(union),
        direct_menu_codes=_sorted(direct_codes),
        role_grants=role_views,
        organization_grants=org_views,
    )


class PermissionService:
    """权限解析与授权读取。无状态；每次调用传入 session。"""

    def __init__(self, outbox: OutboxService | None = None) -> None:
        self._outbox = outbox or OutboxService()

    async def listSubjectMenuCodes(
        self, session: AsyncSession, subject_type: str, subject_id: int
    ) -> list[str]:
        """某主体（用户/角色/组织）当前直接授权的菜单码（需求 #4 的角色/组织视图）。"""
        if subject_type not in _GRANT_SUBJECT_VALUES:
            raise ValueError(f"subject_type 非法：{subject_type!r}")
        rows = (
            await session.execute(
                select(PermissionGrant.menu_code).where(
                    PermissionGrant.subject_type == subject_type,
                    PermissionGrant.subject_id == subject_id,
                )
            )
        ).scalars().all()
        return sorted(set(rows))

    async def computeEffective(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        all_menu_codes: frozenset[str],
    ) -> EffectivePermissions:
        """加载用户主体与授权数据，计算有效菜单权限（合集 + 拆分）。"""
        role_rows = (
            await session.execute(
                select(UserRole.role_id, Role.code, Role.name)
                .join(Role, Role.id == UserRole.role_id)
                .where(UserRole.user_id == user_id)
            )
        ).all()
        org_rows = (
            await session.execute(
                select(UserOrganization.organization_id, Organization.code, Organization.name)
                .join(
                    Organization,
                    Organization.id == UserOrganization.organization_id,
                )
                .where(UserOrganization.user_id == user_id)
            )
        ).all()

        role_meta = {rid: (code, name) for rid, code, name in role_rows}
        org_meta = {oid: (code, name) for oid, code, name in org_rows}

        role_grants = await self._grantsBySubject(
            session, GrantSubjectType.ROLE, role_meta
        )
        org_grants = await self._grantsBySubject(
            session, GrantSubjectType.ORGANIZATION, org_meta
        )
        direct_codes = frozenset(
            await self.listSubjectMenuCodes(session, GrantSubjectType.USER.value, user_id)
        )

        return _composeEffective(
            user_id,
            role_meta=role_meta,
            organization_meta=org_meta,
            role_grants=role_grants,
            organization_grants=org_grants,
            direct_codes=direct_codes,
            all_menu_codes=all_menu_codes,
        )

    async def _grantsBySubject(
        self,
        session: AsyncSession,
        subject_type: GrantSubjectType,
        subject_meta: dict[int, tuple[str, str]],
    ) -> dict[int, frozenset[str]]:
        """按主体分组加载授权码集（仅含 subject_meta 中的主体）。"""
        if not subject_meta:
            return {}
        rows = (
            await session.execute(
                select(PermissionGrant.subject_id, PermissionGrant.menu_code).where(
                    PermissionGrant.subject_type == subject_type.value,
                    PermissionGrant.subject_id.in_(list(subject_meta)),
                )
            )
        ).all()
        grouped: dict[int, set[str]] = {}
        for sid, code in rows:
            grouped.setdefault(sid, set()).add(code)
        return {sid: frozenset(codes) for sid, codes in grouped.items()}

    async def replaceSubjectGrants(
        self,
        session: AsyncSession,
        subject_type: str,
        subject_id: int,
        menu_codes: list[str],
        *,
        valid_menu_codes: frozenset[str],
        actor: str,
    ) -> None:
        """set-replace：整体替换某主体的直接授权。

        非法 menu_code（不在 valid_menu_codes 中）→ ValueError（fail fast，
        由路由层转 422）。旧授权整组删除后插入新集，幂等。写审计由本方法
        在调用方事务内入队（caller commit）。
        """
        if subject_type not in _GRANT_SUBJECT_VALUES:
            raise ValueError(f"subject_type 非法：{subject_type!r}")
        unknown = [c for c in menu_codes if c not in valid_menu_codes]
        if unknown:
            raise ValueError(f"未知菜单 code: {', '.join(sorted(unknown))}")

        await session.execute(
            delete(PermissionGrant).where(
                PermissionGrant.subject_type == subject_type,
                PermissionGrant.subject_id == subject_id,
            )
        )
        for code in sorted(set(menu_codes)):
            session.add(
                PermissionGrant(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    menu_code=code,
                )
            )
        await self._outbox.enqueue(
            session,
            event_type="permission_grant_updated",
            entity_type="permission_grant",
            entity_id=subject_id,
            actor=actor,
            payload={
                "before": None,
                "after": {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "menu_codes": sorted(set(menu_codes)),
                },
            },
        )
