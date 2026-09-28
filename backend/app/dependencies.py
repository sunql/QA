"""FastAPI 共享依赖。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, getSettings
from app.domain.exceptions import PermissionDeniedError
from app.domain.models import Organization, Role, User, UserOrganization, UserRole
from app.domain.error_messages import (
    MSG_AUTH_REQUIRED,
    MSG_TOKEN_INVALID,
    MSG_TOKEN_REVOKED,
)
from app.infrastructure.database import getDb
from app.services.jwt_codec import decode_jwt

logger = logging.getLogger(__name__)


DEFAULT_STUB_ROLES: tuple[str, ...] = ("user", "admin")
"""stub auth 默认角色（dev/test 兜底）。

dev/test 下默认给 admin 角色，让未登录/未配 header 的前端调用也能通过
Phase 4.5 的 owner-based ACL，便于本地体验。生产部署必须关闭 stub auth
（AUTH_STUB_ENABLED=0），否则任意客户端可直接伪造 admin。
"""

DEFAULT_STUB_USER_ID = "anonymous"


@dataclass(frozen=True)
class CurrentUser:
    """当前用户（feat-user-auth，2026-09-20）。

    **三种解析路径**（按优先级）：
    1) Bearer JWT（``Authorization: Bearer <token>``）：decode 后查
       ``user_sessions`` 表验证 jti 未吊销 → DB 加载用户 + roles + orgs。
       填 ``jti`` / ``tokenExp`` 字段供后续登出 / 改密吊销用。
    2) Stub header（``X-User-Id`` + ``X-User-Roles`` + ``X-User-Departments``）：
       X-User-Id 命中 DB enabled 用户 → DB roles/departments；查无此人 →
       DEFAULT_STUB_ROLES 默认（dev 兜底）。
    3) ``anonymous`` 兜底：仅 stub 模式生效。

    ``dbUserId`` 字段：Bearer 路径恒为 DB 主键；stub 路径命中时也是 DB 主键，
    查无此人时为 ``None``。ACL / 审计都用 ``dbUserId`` 而非 ``userId`` 字符串
    （防「X-User-Id 任意字符串」伪造）。

    AUTH_MODE 开关（``app.config.Settings.authMode``）：
    - ``stub``（默认）：三种路径都可走
    - ``real``：仅 Bearer 路径有效；stub 头默认拒绝
      （``ALLOW_STUB_WHEN_REAL=true`` 允许过渡期并存）
    """

    userId: str = DEFAULT_STUB_USER_ID
    tenantId: str = "default"
    roles: tuple[str, ...] = DEFAULT_STUB_ROLES
    departments: tuple[str, ...] = ()
    dbUserId: int | None = None
    jti: str | None = None
    tokenExp: int | None = None


async def getCurrentUser(
    authorization: str | None = Header(default=None, alias="Authorization"),
    xUserId: str | None = Header(default=None, alias="X-User-Id"),
    xTenantId: str | None = Header(default=None, alias="X-Tenant-Id"),
    xUserRoles: str | None = Header(default=None, alias="X-User-Roles"),
    xUserDepartments: str | None = Header(default=None, alias="X-User-Departments"),
    session: AsyncSession = Depends(getDb),
) -> CurrentUser:
    """从请求头解析当前用户（feat-user-auth 改造后）。

    Headers:
        Authorization: ``Bearer <jwt>`` 优先解析（real 模式强制）
        X-User-Id / X-User-Roles / X-User-Departments：stub 模式兜底
        X-Tenant-Id：租户 ID（默认 'default'）

    解析顺序（feat-user-auth，2026-09-20）：

    1) ``Authorization: Bearer <jwt>`` 解析：
       - decode JWT（验签+exp+iss+aud）
       - 同步查 ``user_sessions`` 表：``jti`` 命中 + ``revoked_at IS NULL``
         + ``expires_at > now()`` → 命中才放行（fail-closed）
       - 查 DB users 表（``enabled=True`` 才放行）
       - 加载 roles / organizations 列表

    2) AUTH_MODE=real 时，无 Bearer → 403（拒绝 stub 头）
       AUTH_MODE=stub 时，回退 stub 头解析（保持原有 dev 体验）

    3) ``anonymous`` 兜底：仅 stub 模式且 AUTH_STUB_ENABLED=1 时。

    安全护栏：
        - 启动期 main.py 校验：APP_ENV=production 时 AUTH_MODE=real 且
          JWT_SECRET ≥ 32 字节，否则 ERROR 日志告警（fail-soft，不阻塞启动）
        - 反向代理（nginx）剥离客户端传来的 ``Authorization`` / ``X-User-*`` 头
    """
    settings = getSettings()

    # 优先级 1: Bearer JWT（两种模式都尝试解析）
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        try:
            payload = decode_jwt(token)
        except InvalidTokenError as exc:
            logger.warning("JWT 解析失败: %s", exc)
            raise PermissionDeniedError(MSG_TOKEN_INVALID) from exc

        jti = payload.get("jti", "")
        sub = payload.get("sub", "")
        if not jti or not sub:
            raise PermissionDeniedError(MSG_TOKEN_INVALID)

        # 同步查 user_sessions 验证 session 未吊销（fail-closed）
        from app.models.rbac import UserSession  # 避免循环依赖

        session_row = (
            await session.execute(
                select(UserSession).where(
                    UserSession.jti == jti,
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ),
                )
            )
        ).scalar_one_or_none()
        if session_row is None:
            raise PermissionDeniedError(MSG_TOKEN_REVOKED)

        # 查 DB user
        try:
            user_id = int(sub)
        except ValueError as exc:
            raise PermissionDeniedError(MSG_TOKEN_INVALID) from exc
        user = await session.get(User, user_id)
        if user is None or not user.enabled:
            raise PermissionDeniedError(MSG_AUTH_REQUIRED)

        roles, departments = await _loadDbRolesAndOrgs(session, user_id)
        return CurrentUser(
            userId=user.username,
            tenantId=xTenantId or "default",
            roles=roles,
            departments=departments,
            dbUserId=user_id,
            jti=jti,
            tokenExp=payload.get("exp"),
        )

    # 优先级 2: stub 头解析
    if settings.authMode == "real" and not settings.allowStubWhenReal:
        # real 模式强制要求 Bearer
        raise PermissionDeniedError(MSG_AUTH_REQUIRED)

    if not settings.authStubEnabled:
        raise PermissionDeniedError(
            "Stub auth 未启用：生产环境必须由 JWT/IdP 解析用户身份，"
            "或设置 AUTH_STUB_ENABLED=1（仅 dev/test）"
        )

    base = _buildCurrentUser(
        userId=xUserId,
        tenantId=xTenantId,
        rolesHeader=xUserRoles,
        departmentsHeader=xUserDepartments,
    )
    dbUser = await _resolveDbUser(session, base.userId)
    if dbUser is None:
        return base
    roles, departments = await _loadDbRolesAndOrgs(session, dbUser.id)
    return CurrentUser(
        userId=base.userId,
        tenantId=base.tenantId,
        roles=roles,
        departments=departments,
        dbUserId=dbUser.id,
    )


async def _resolveDbUser(session: AsyncSession, username: str) -> User | None:
    """按 users.username 精确匹配；查无此人 / 未启用 / anonymous → None（走桩回退）。"""
    if not username or username == DEFAULT_STUB_USER_ID:
        return None
    row = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if row is None or not row.enabled:
        return None
    return row


async def _loadDbRolesAndOrgs(
    session: AsyncSession, user_id: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """加载 DB 用户的角色 code 与组织 code（owner-based ACL 的 departments 语义）。"""
    roles = tuple(
        (
            await session.execute(
                select(Role.code)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == user_id)
                .order_by(Role.id)
            )
        ).scalars().all()
    )
    orgs = tuple(
        (
            await session.execute(
                select(Organization.code)
                .join(
                    UserOrganization,
                    UserOrganization.organization_id == Organization.id,
                )
                .where(UserOrganization.user_id == user_id)
                .order_by(Organization.id)
            )
        ).scalars().all()
    )
    return roles, orgs


def _splitCsv(headerValue: str | None) -> tuple[str, ...]:
    """逗号分隔字符串 → 去空白 + 跳空段 → tuple。"""
    if not headerValue:
        return ()
    return tuple(p.strip() for p in headerValue.split(",") if p.strip())


def _buildCurrentUser(
    *,
    userId: str | None,
    tenantId: str | None,
    rolesHeader: str | None,
    departmentsHeader: str | None,
) -> CurrentUser:
    """从 header 原始值组装 CurrentUser。

    拆成纯函数：(1) 不依赖 FastAPI Header 对象，方便单测；
    (2) 解析逻辑可独立验证（不去重写 `getCurrentUser` 全文）。
    """
    roles = _splitCsv(rolesHeader) or DEFAULT_STUB_ROLES
    departments = _splitCsv(departmentsHeader)
    return CurrentUser(
        userId=userId or DEFAULT_STUB_USER_ID,
        tenantId=tenantId or "default",
        roles=roles,
        departments=departments,
    )


def getSettingsDep() -> Settings:
    """FastAPI 依赖：注入只读 Settings。"""
    return getSettings()


async def getSessionDep(session: AsyncSession = Depends(getDb)) -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：注入数据库会话。"""
    yield session


async def getAdminOnlyActor(
    user: CurrentUser = Depends(getCurrentUser),
) -> CurrentUser:
    """admin-only 审计 API 的 actor 派生（feat-audit-history-api Task 1）。

    与 assertCanModify 模式平行：调用方只需 ``_admin: CurrentUser = Depends(getAdminOnlyActor)``，
    非 admin 直接 403，无需 service 层重复校验。

    设计要点：
    - 角色检查直接读 ``user.roles``（已由 getCurrentUser 解析 headers 完成），
      不要再调一次 _buildCurrentUser，避免解析逻辑漂移。
    - 缺省 stub auth 默认带 admin（DEFAULT_STUB_ROLES），所以 dev/test 默认放行；
      生产 stub 关闭后由反向代理剥离 X-User-* 头保证安全。
    - ``user.roles or []`` 兼容手工构造的 CurrentUser(roles=None) 场景。
    """
    if "admin" not in (user.roles or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅 admin 可访问审计 API",
        )
    return user
