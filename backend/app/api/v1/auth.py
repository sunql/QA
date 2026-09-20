"""认证端点（feat-user-auth，2026-09-20）。

端点：
- POST /auth/login             公开
- GET  /auth/me                需 Bearer（getCurrentUser）
- POST /auth/logout            需 Bearer（getCurrentUser）
- PUT  /auth/me/password       需 Bearer
- GET  /auth/password-policy   公开

注意：login 不走 getCurrentUser（尚未登录）。其余 4 个端点均依赖
getCurrentUser 强制 Bearer 解析（AUTH_MODE=real 时）或 stub 解析
（AUTH_MODE=stub 时）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.schemas.auth import (
    AdminResetPasswordRequest,
    AuthChangePasswordRequest,
    AuthLoginRequest,
    AuthLoginResponse,
    AuthMeRead,
    PasswordPolicyRead,
)
from app.services.auth_service import AuthService
from app.services.password_policy import MIN_LENGTH

router = APIRouter(tags=["auth"])


@router.post("/login", response_model=AuthLoginResponse)
async def login(
    payload: AuthLoginRequest,
    request: Request,
    session: AsyncSession = Depends(getDb),
) -> AuthLoginResponse:
    return await AuthService.login(payload, request, session)


@router.get("/me", response_model=AuthMeRead)
async def getMe(
    actor: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> AuthMeRead:
    return await AuthService.get_me(actor, session)


@router.post("/logout", status_code=204)
async def logout(
    actor: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> Response:
    await AuthService.logout(actor, session)
    return Response(status_code=204)


@router.put("/me/password", status_code=204)
async def changeOwnPassword(
    payload: AuthChangePasswordRequest,
    actor: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> Response:
    await AuthService.change_own_password(payload, actor, session)
    return Response(status_code=204)


@router.get("/password-policy", response_model=PasswordPolicyRead)
async def getPasswordPolicy() -> PasswordPolicyRead:
    """公开端点：前端 LoginPage 用此即时校验密码强度。"""
    return PasswordPolicyRead(minLength=MIN_LENGTH)


# 导出 AdminResetPasswordRequest 以便 users.py 复用
__all__ = ["router", "AdminResetPasswordRequest"]