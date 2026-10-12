"""文档密级过滤构造器（Task 4.3：audit-fx-4.3）。

根据用户身份（角色 + l3_acl 白名单 + restricted_access 标记）
返回该用户可访问的密级列表，供 Milvus 检索过滤使用。

过滤规则（优先级递减）：
1. compliance_officer 角色 OR l3_acl 表中有记录 → ["L0", "L1", "L2", "L3"]
2. restricted_access=True OR restricted_viewer 角色 → ["L0", "L1", "L2"]
3. 默认 → ["L0", "L1"]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.dependencies import CurrentUser

logger = logging.getLogger(__name__)

# 密级常量
CLEARANCE_ALL = ["L0", "L1", "L2", "L3"]
CLEARANCE_RESTRICTED = ["L0", "L1", "L2"]
CLEARANCE_DEFAULT = ["L0", "L1"]


async def _inL3Acl(session: AsyncSession, userDbId: int) -> bool:
    """检查用户是否在 L3 ACL 白名单中（有任意 L3 访问权）。"""
    from app.domain.models import L3Acl

    result = await session.execute(
        select(L3Acl).where(L3Acl.user_id == userDbId).limit(1)
    )
    return result.scalar_one_or_none() is not None


def _hasRole(user: CurrentUser, roleCode: str) -> bool:
    """检查 CurrentUser 是否拥有指定角色代码。"""
    return roleCode in user.roles


async def buildClearanceFilter(
    session: AsyncSession,
    user: CurrentUser,
) -> list[str]:
    """返回用户可访问的密级列表。

    Args:
        session: DB session（用于查询 l3_acl 和 users 表）
        user: 当前用户身份

    Returns:
        密级列表，如 ["L0", "L1", "L2", "L3"]
    """
    if user.dbUserId is None:
        # 无 DB 身份（anonymous）：降级为最小权限
        logger.warning("buildClearanceFilter: anonymous user, using default clearance")
        return CLEARANCE_DEFAULT

    # 检查 L3 全量访问权限
    hasComplianceRole = _hasRole(user, "compliance_officer")
    inL3Acl = await _inL3Acl(session, user.dbUserId)

    if hasComplianceRole or inL3Acl:
        return CLEARANCE_ALL

    # 检查 restricted_access 字段（需查 User 模型）
    from app.models.rbac import User

    result = await session.execute(
        select(User.restricted_access).where(User.id == user.dbUserId)
    )
    row = result.first()
    hasRestrictedAccess = bool(row.restricted_access) if row else False

    if hasRestrictedAccess or _hasRole(user, "restricted_viewer"):
        return CLEARANCE_RESTRICTED

    return CLEARANCE_DEFAULT
