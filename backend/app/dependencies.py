"""FastAPI 共享依赖。"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, getSettings
from app.domain.exceptions import PermissionDeniedError
from app.infrastructure.database import getDb

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
    """当前用户（MVP stub，后续替换为 JWT/API Key 解析）。

    departments 用于 owner-based ACL（Phase 4.5 governance hardening）：
    与 entity.owner 字符串匹配；用户属于多部门时（如「采购+财务」双岗），
    headers 可一次性携带多个部门逗号分隔。
    """

    userId: str = DEFAULT_STUB_USER_ID
    tenantId: str = "default"
    roles: tuple[str, ...] = DEFAULT_STUB_ROLES
    departments: tuple[str, ...] = ()


async def getCurrentUser(
    xUserId: str | None = Header(default=None, alias="X-User-Id"),
    xTenantId: str | None = Header(default=None, alias="X-Tenant-Id"),
    xUserRoles: str | None = Header(default=None, alias="X-User-Roles"),
    xUserDepartments: str | None = Header(default=None, alias="X-User-Departments"),
) -> CurrentUser:
    """从请求头解析当前用户（stub）。

    Headers:
        X-User-Id：用户 ID（默认 'anonymous'）
        X-Tenant-Id：租户 ID（默认 'default'）
        X-User-Roles：逗号分隔角色（默认 ['user', 'admin']，见 DEFAULT_STUB_ROLES）
        X-User-Departments：逗号分隔部门（默认 []）

    真实生产应由 JWT/IdP 解析并填充 departments；stub 模式保证
    Phase 4.5 ACL 接口稳定，鉴权接入后无需改 ACL 规则。

    默认 admin 设计意图（2026-08-31 与用户对齐）：
        早期 dev/test 中默认只有 'user' 角色，导致未登录访问
        /api/v1/features 等有 owner-based ACL 的端点时一律 403，
        新人易踩坑。改为默认带 admin 让 stub 模式下「打开即用」，
        显式测试非 admin 路径时通过 X-User-Roles 覆盖即可。

    安全护栏（security-reviewer 反馈）：
        任意客户端可直接伪造 X-User-Roles=admin 绕过 ACL；
        因此：
        - 默认 AUTH_STUB_ENABLED=1（dev/test 默认开）
        - 生产部署必须设 AUTH_STUB_ENABLED=0 + 由反向代理剥离 X-User-* 头，
          或后续接入 JWT 时移除该 stub 函数本身（DEFAULT_STUB_ROLES 此时
          失效，因为 stub 路径根本不被走到）。
        - 应用启动时若 APP_ENV=production 且 stub 仍开启，日志 ERROR 告警。
    """
    if os.environ.get("AUTH_STUB_ENABLED", "1") != "1":
        # 真实生产应拒绝任何 stub 头请求（未接入 JWT 前的安全兜底）
        raise PermissionDeniedError(
            "Stub auth 未启用：生产环境必须由 JWT/IdP 解析用户身份，"
            "或设置 AUTH_STUB_ENABLED=1（仅 dev/test）"
        )
    return _buildCurrentUser(
        userId=xUserId,
        tenantId=xTenantId,
        rolesHeader=xUserRoles,
        departmentsHeader=xUserDepartments,
    )


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
