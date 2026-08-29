"""FastAPI 共享依赖。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, getSettings
from app.infrastructure.database import getDb


@dataclass(frozen=True)
class CurrentUser:
    """当前用户（MVP stub，后续替换为 JWT/API Key 解析）。"""

    userId: str = "anonymous"
    tenantId: str = "default"
    roles: tuple[str, ...] = ("user",)


async def getCurrentUser(
    xUserId: str | None = Header(default=None, alias="X-User-Id"),
    xTenantId: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> CurrentUser:
    """从请求头解析当前用户（stub）。"""
    return CurrentUser(
        userId=xUserId or "anonymous",
        tenantId=xTenantId or "default",
    )


def getSettingsDep() -> Settings:
    """FastAPI 依赖：注入只读 Settings。"""
    return getSettings()


async def getSessionDep(session: AsyncSession = Depends(getDb)) -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：注入数据库会话。"""
    yield session
