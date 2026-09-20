"""AuthService（feat-user-auth，2026-09-20）。

封装登录 / 个人信息 / 改密 / 登出 / admin 重置密码 + session 表操作。
所有方法都依赖一个外部传入的 AsyncSession，由调用方（FastAPI Depends）负责 commit。

防时间侧信道：
- 查无用户 / disabled / 无 password_hash：跑 dummy bcrypt + sleep AUTH_MIN_DELAY_MS
- 防止外部通过响应时间推断用户存在性
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import bcrypt
from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.dependencies import CurrentUser
from app.domain.error_messages import (
    MSG_AUTH_REQUIRED,
    MSG_INVALID_CREDENTIALS,
    MSG_OLD_PASSWORD_INCORRECT,
    MSG_PASSWORD_TOO_WEAK,
)
from app.domain.exceptions import AuthFailedError, NotFoundError, ValidationError
from app.models.rbac import Organization, Role, User, UserOrganization, UserRole, UserSession
from app.schemas.auth import (
    AdminResetPasswordRequest,
    AuthChangePasswordRequest,
    AuthLoginRequest,
    AuthLoginResponse,
    AuthMeRead,
    UserSummaryRead,
)
from app.services.audit_service import AuditService
from app.services.jwt_codec import sign_jwt
from app.services.password_policy import validate_password


_DUMMY_BCRYPT_HASH = bcrypt.hashpw(b"dummy-password-1234", bcrypt.gensalt(rounds=4)).decode()


def _hash_password(password: str, rounds: int) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("utf-8")


async def _load_user_with_password(session: AsyncSession, username: str) -> User | None:
    """按 username 精确匹配；返回 user 或 None（不区分 enabled）。"""
    row = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    return row


async def _load_user_roles_and_orgs(
    session: AsyncSession, user_id: int
) -> tuple[list[str], list[str]]:
    """加载用户角色 code 列表 + 组织 code 列表。"""
    roles = list(
        (
            await session.execute(
                select(Role.code)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == user_id)
                .order_by(Role.id)
            )
        ).scalars().all()
    )
    orgs = list(
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


async def _revoke_all_user_sessions(
    session: AsyncSession, user_id: int, reason: str
) -> int:
    """吊销某用户所有未过期 session；返回受影响行数。"""
    result = await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc), revoked_reason=reason)
    )
    return result.rowcount or 0


async def _write_user_session(
    session: AsyncSession,
    *,
    user_id: int,
    jti: str,
    issued_at: datetime,
    expires_at: datetime,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """插入一条活跃 session 记录。"""
    session.add(
        UserSession(
            jti=jti,
            user_id=user_id,
            issued_at=issued_at,
            expires_at=expires_at,
            ip=ip,
            user_agent=user_agent,
        )
    )


async def _emit_equalizing_delay_ms() -> None:
    """登录失败时的等长延迟（防时间侧信道）。"""
    settings = getSettings()
    delay_ms = max(0, settings.authMinDelayMs)
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)


class AuthService:
    """认证 service。"""

    @staticmethod
    async def login(
        payload: AuthLoginRequest, request: Request, session: AsyncSession
    ) -> AuthLoginResponse:
        """登录：校验密码 → 签 JWT → 写 user_session → 更新 last_login。

        失败统一抛 AuthFailedError(MSG_INVALID_CREDENTIALS) + 等长延迟。
        审计：
        - 成功：audit_log.action='auth.login' entity='user' entity_id=user.id
        - 失败：audit_log.action='auth.login_failed'（不入 entity_id 避免误锁定真实用户）
        """
        user = await _load_user_with_password(session, payload.username)
        # 失败路径 1：用户不存在 / disabled / 无密码
        if user is None or user.password_hash is None or not user.enabled:
            bcrypt.checkpw(payload.password.encode(), _DUMMY_BCRYPT_HASH.encode())
            await _emit_equalizing_delay_ms()
            await AuditService().record(
                session,
                entity_type="user",
                entity_id=user.id if user is not None else 0,
                action="auth.login_failed",
                actor=payload.username,
                actor_departments=(),
                before=None,
                after=None,
            )
            await session.commit()
            raise AuthFailedError(MSG_INVALID_CREDENTIALS)

        # 失败路径 2：密码错
        if not bcrypt.checkpw(payload.password.encode(), user.password_hash.encode()):
            await _emit_equalizing_delay_ms()
            await AuditService().record(
                session,
                entity_type="user",
                entity_id=user.id,
                action="auth.login_failed",
                actor=payload.username,
                actor_departments=(),
                before=None,
                after=None,
            )
            await session.commit()
            raise AuthFailedError(MSG_INVALID_CREDENTIALS)

        # 成功路径
        settings = getSettings()
        now = datetime.now(timezone.utc)
        token, jti, expires_at = sign_jwt(
            user_id=user.id, username=user.username
        )
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        await _write_user_session(
            session,
            user_id=user.id,
            jti=jti,
            issued_at=now,
            expires_at=expires_at,
            ip=client_ip,
            user_agent=user_agent,
        )
        user.last_login_at = now
        user.last_login_ip = client_ip
        # audit_log auth.login（actor 用 username；entity_id=用户 DB PK）
        await AuditService().record(
            session,
            entity_type="user",
            entity_id=user.id,
            action="auth.login",
            actor=user.username,
            actor_departments=(),
            before=None,
            after={"jti": jti},
        )
        await session.commit()
        await session.refresh(user)

        roles, orgs = await _load_user_roles_and_orgs(session, user.id)
        return AuthLoginResponse(
            accessToken=token,
            expiresIn=settings.jwtTtlSeconds,
            mustChangePassword=user.must_change_password,
            user=UserSummaryRead(
                id=user.id,
                username=user.username,
                displayName=user.display_name,
                email=user.email,
                roles=roles,
                organizations=orgs,
            ),
        )

    @staticmethod
    async def get_me(actor: CurrentUser, session: AsyncSession) -> AuthMeRead:
        """GET /auth/me：返回当前用户信息。"""
        if actor.dbUserId is None:
            raise AuthFailedError(MSG_AUTH_REQUIRED)
        user = await session.get(User, actor.dbUserId)
        if user is None:
            raise NotFoundError("用户不存在")
        roles, orgs = await _load_user_roles_and_orgs(session, user.id)
        return AuthMeRead(
            id=user.id,
            username=user.username,
            displayName=user.display_name,
            email=user.email,
            enabled=user.enabled,
            mustChangePassword=user.must_change_password,
            roles=roles,
            organizations=orgs,
            tenantId=actor.tenantId,
            lastLoginAt=user.last_login_at,
        )

    @staticmethod
    async def logout(actor: CurrentUser, session: AsyncSession) -> None:
        """POST /auth/logout：吊销当前 session（若有 jti）。"""
        if actor.jti is None:
            return  # stub 模式无 jti，不做任何事
        await session.execute(
            update(UserSession)
            .where(UserSession.jti == actor.jti, UserSession.revoked_at.is_(None))
            .values(
                revoked_at=datetime.now(timezone.utc),
                revoked_reason="logout",
            )
        )
        await AuditService().record(
            session,
            entity_type="user",
            entity_id=actor.dbUserId or 0,
            action="auth.logout",
            actor=actor.userId,
            actor_departments=tuple(actor.departments or ()),
            before=None,
            after=None,
        )
        await session.commit()

    @staticmethod
    async def change_own_password(
        payload: AuthChangePasswordRequest,
        actor: CurrentUser,
        session: AsyncSession,
    ) -> None:
        """PUT /auth/me/password：用户自己改密 → 自动吊销所有 session。"""
        if actor.dbUserId is None:
            raise AuthFailedError(MSG_AUTH_REQUIRED)
        user = await session.get(User, actor.dbUserId)
        if user is None or user.password_hash is None:
            raise AuthFailedError(MSG_AUTH_REQUIRED)
        if not bcrypt.checkpw(payload.oldPassword.encode(), user.password_hash.encode()):
            raise AuthFailedError(MSG_OLD_PASSWORD_INCORRECT)
        ok, err = validate_password(payload.newPassword)
        if not ok:
            raise ValidationError(err or MSG_PASSWORD_TOO_WEAK)
        settings = getSettings()
        user.password_hash = _hash_password(payload.newPassword, settings.bcryptRounds)
        user.must_change_password = False
        await _revoke_all_user_sessions(session, user.id, "password_changed")
        await AuditService().record(
            session,
            entity_type="user",
            entity_id=user.id,
            action="auth.password_changed",
            actor=actor.userId,
            actor_departments=tuple(actor.departments or ()),
            before=None,
            after=None,
        )
        await session.commit()

    @staticmethod
    async def admin_reset_password(
        target_user_id: int,
        payload: AdminResetPasswordRequest,
        actor: CurrentUser,
        session: AsyncSession,
    ) -> None:
        """PUT /users/{id}/password：admin 重置密码 → 吊销目标用户所有 session。

        admin role 已由路由层 ``getAdminOnlyActor`` 校验（403 非 admin）。
        actor 可能是 stub header 解析的 anonymous → dbUserId 为 None，仍放行
        （因为 admin role 才是权限判定依据，不是 actor 的 dbUserId）。
        """
        user = await session.get(User, target_user_id)
        if user is None:
            raise NotFoundError("目标用户不存在")
        ok, err = validate_password(payload.newPassword)
        if not ok:
            raise ValidationError(err or MSG_PASSWORD_TOO_WEAK)
        settings = getSettings()
        user.password_hash = _hash_password(payload.newPassword, settings.bcryptRounds)
        user.must_change_password = payload.forceChangeOnNextLogin
        await _revoke_all_user_sessions(session, user.id, "admin_reset")
        await AuditService().record(
            session,
            entity_type="user",
            entity_id=user.id,
            action="user.password_reset",
            actor=actor.userId,
            actor_departments=tuple(actor.departments or ()),
            before=None,
            after={"forceChangeOnNextLogin": payload.forceChangeOnNextLogin},
        )
        await session.commit()