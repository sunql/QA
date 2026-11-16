"""测试用「已校验身份」助手（真实 JWT 链路）。

为什么需要它
------------
P5 的**治理写面**（审批决策 / 取消实例）要求**已校验**身份
（``CurrentUser.isRealIdentity`` = ``dbUserId is not None and jti is not None``）：

- stub 头（``X-User-Id: lead``）只能证明「这个名字命中了某个 DB 账号」
  ⇒ **任意客户端都能冒充那个账号** ⇒ 四眼原则与「谁批的」审计都失真；
- ``jti`` **只在 Bearer/JWT 路径**填 ⇒ 只有走 JWT 才是「服务端验过签的人」。

⇒ 测试必须走**完整 JWT 链路**：签发（``sign_jwt``）→ ``user_sessions`` 落库
（不落库 ⇒ 服务端按「session 已吊销」拒绝）→ 请求带 ``Authorization: Bearer``。

⚠️ 本模块**不是**测试文件（无 ``test_`` 前缀）⇒ 不进 CI 分片、不影响分片归属。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import Role, User, UserRole, UserSession
from app.services.jwt_codec import sign_jwt


async def issueToken(session: AsyncSession, user: User) -> str:
    """给已有用户签发一个**可用**的 JWT（同时落 ``user_sessions``）。"""

    token, jti, expiresAt = sign_jwt(user_id=user.id, username=user.username)
    session.add(
        UserSession(
            jti=jti,
            user_id=user.id,
            issued_at=datetime.now(timezone.utc),
            expires_at=expiresAt,
        )
    )
    await session.commit()
    return token


def bearerHeaders(token: str) -> dict[str, str]:
    """Bearer 头（``Authorization: Bearer <token>``）。"""

    return {"Authorization": f"Bearer {token}"}


async def ensureUser(
    session: AsyncSession,
    username: str,
    *,
    roleCodes: tuple[str, ...] = (),
    enabled: bool = True,
) -> tuple[User, str]:
    """建（或复用）一个 DB 用户 + 授予角色 ⇒ 返回 ``(用户, JWT)``。"""

    user = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None:
        user = User(username=username, display_name=username, enabled=enabled)
        session.add(user)
        await session.commit()
        await session.refresh(user)

    for roleCode in roleCodes:
        role = (
            await session.execute(select(Role).where(Role.code == roleCode))
        ).scalar_one_or_none()
        if role is None:
            role = Role(code=roleCode, name=roleCode)
            session.add(role)
            await session.commit()
            await session.refresh(role)
        session.add(UserRole(user_id=user.id, role_id=role.id))
    if roleCodes:
        await session.commit()

    token = await issueToken(session, user)
    return user, token
