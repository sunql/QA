"""FastAPI 共享依赖。"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, getSettings
from app.domain.exceptions import PermissionDeniedError
from app.infrastructure.database import getDb

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurrentUser:
    """当前用户（MVP stub，后续替换为 JWT/API Key 解析）。

    departments 用于 owner-based ACL（Phase 4.5 governance hardening）：
    与 entity.owner 字符串匹配；用户属于多部门时（如「采购+财务」双岗），
    headers 可一次性携带多个部门逗号分隔。
    """

    userId: str = "anonymous"
    tenantId: str = "default"
    roles: tuple[str, ...] = ("user",)
    departments: tuple[str, ...] = ()


async def getCurrentUser(
    xUserId: str | None = Header(default=None, alias="X-User-Id"),
    xTenantId: str | None = Header(default=None, alias="X-Tenant-Id"),
    xUserRoles: str | None = Header(default=None, alias="X-User-Roles"),
    xUserDepartments: str | None = Header(default=None, alias="X-User-Departments"),
) -> CurrentUser:
    """从请求头解析当前用户（stub）。

    Headers:
        X-User-Id：用户 ID（默认 anonymous）
        X-Tenant-Id：租户 ID（默认 default）
        X-User-Roles：逗号分隔角色（默认 ['user']）
        X-User-Departments：逗号分隔部门（默认 []）

    真实生产应由 JWT/IdP 解析并填充 departments；stub 模式保证
    Phase 4.5 ACL 接口稳定，鉴权接入后无需改 ACL 规则。

    安全护栏（security-reviewer 反馈）：
        任意客户端可直接伪造 X-User-Roles=admin 绕过 ACL；
        因此：
        - 默认 AUTH_STUB_ENABLED=1（dev/test 默认开）
        - 生产部署应设为 AUTH_STUB_ENABLED=0 + 由反向代理剥离 X-User-* 头，
          或后续接入 JWT 时移除该 stub 函数本身。
        - 应用启动时若 APP_ENV=production 且 stub 仍开启，日志 ERROR 告警。
    """
    if os.environ.get("AUTH_STUB_ENABLED", "1") != "1":
        # 真实生产应拒绝任何 stub 头请求（未接入 JWT 前的安全兜底）
        raise PermissionDeniedError(
            "Stub auth 未启用：生产环境必须由 JWT/IdP 解析用户身份，"
            "或设置 AUTH_STUB_ENABLED=1（仅 dev/test）"
        )
    roles = tuple(r.strip() for r in (xUserRoles or "user").split(",") if r.strip())
    departments = tuple(
        d.strip() for d in (xUserDepartments or "").split(",") if d.strip()
    )
    return CurrentUser(
        userId=xUserId or "anonymous",
        tenantId=xTenantId or "default",
        roles=roles or ("user",),
        departments=departments,
    )


def getSettingsDep() -> Settings:
    """FastAPI 依赖：注入只读 Settings。"""
    return getSettings()


async def getSessionDep(session: AsyncSession = Depends(getDb)) -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：注入数据库会话。"""
    yield session
