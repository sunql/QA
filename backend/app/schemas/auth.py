"""认证端点 DTO（feat-user-auth，2026-09-20）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.domain.schemas import CamelModel


class AuthLoginRequest(CamelModel):
    """POST /auth/login 请求体。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserSummaryRead(CamelModel):
    """嵌入在 AuthLoginResponse.user；不含 mustChangePassword（外层已有）。"""

    id: int
    username: str
    displayName: str
    email: str | None
    roles: list[str]
    organizations: list[str]


class AuthLoginResponse(CamelModel):
    """POST /auth/login 响应。"""

    accessToken: str
    tokenType: str = "Bearer"
    expiresIn: int
    mustChangePassword: bool
    user: UserSummaryRead


class AuthMeRead(CamelModel):
    """GET /auth/me 响应。"""

    id: int
    username: str
    displayName: str
    email: str | None
    enabled: bool
    mustChangePassword: bool
    roles: list[str]
    organizations: list[str]
    tenantId: str
    lastLoginAt: datetime | None


class AuthChangePasswordRequest(CamelModel):
    """PUT /auth/me/password 请求体。"""

    oldPassword: str
    newPassword: str


class AdminResetPasswordRequest(CamelModel):
    """PUT /users/{id}/password 请求体。"""

    newPassword: str
    forceChangeOnNextLogin: bool = True


class PasswordPolicyRead(CamelModel):
    """GET /auth/password-policy 响应（公开端点，无需登录）。"""

    minLength: int = 8
    requireLetter: bool = True
    requireDigit: bool = True
    requireSpecial: bool = False