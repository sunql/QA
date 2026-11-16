"""P4 **统一权限源**：用户 → 可见域 / 组织 / 业务对象。

为什么需要这个模块
------------------
P3a（语义发布 P1a）把「维度」做成了**调用方显式传入**的维度，并在模块文档里
明写：**仓内当时没有「用户 → 可见域 / 组织」的策略源**（现有 ACL 只有 admin 级的
`assertCanModify`）⇒ 策略接线被**明确留给后续**。P4 就是那个「后续」。

本模块的定位
------------
1. **只推导一次**：编排层（`facade.py`）在一次查询里推导出一个 `KnowledgeScope`，
   四路各自拿它**收窄** ⇒ 「四路权限判定同源」不靠约定，靠**只造一个对象**。
2. **请求只能收窄**：请求体里的 `scope` 与服务端推导结果**取交集**，
   永远不能放宽（否则调用方能自选越权的域）。
3. **fail-closed**：`primary_org_id IS NULL` 的域对非 admin **不可见**
   （没有主组织 = 不知道归谁 ⇒ 不能因为「查不到归属」就放行）。
4. **admin 判定与仓内其它门禁同口径**：`"admin" in user.roles`
   （同 `getAdminOnlyActor` / `requireAdmin`）⇒ 生产 `AUTH_MODE=real` 时
   roles 只来自 DB、stub 头被反向代理剥离 ⇒ 伪造不出 admin。

已知边界
--------
- **L-P4-1**：`primary_org_id IS NULL` 的域，非 admin 看不见（fail-closed 的代价：
  存量域若没补主组织，需要管理员补齐后才可被普通用户查询）。
- **L-P4-8**：用户的**文档安全等级**没有权威来源（`document_catalog.security_level`
  是**文档**的属性，不是用户的）⇒ 权威值按「admin ⇒ L3、其余 ⇒ L1」取值；
  请求里的 `maxSecurityLevel` **本批不消费**（没有可据以过滤的文档维度）
  ⇒ 显式传值时响应带一条明确警告（不许「传了却当没传」）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.knowledge_schemas import (
    DEFAULT_MAX_SECURITY_LEVEL,
    KnowledgeScopeRequest,
)
from app.domain.models import BusinessObject, BusinessObjectDomain, DataDomain
from app.models.rbac import ADMIN_ROLE_CODE, Organization

logger = logging.getLogger(__name__)

#: 安全等级顺序（数字大 = 可见面大）。
_SECURITY_LEVEL_ORDER: dict[str, int] = {"L1": 1, "L2": 2, "L3": 3}

#: admin 的安全等级上限；非 admin 按最小可见等级。
ADMIN_SECURITY_LEVEL = "L3"


@dataclass(frozen=True, slots=True)
class KnowledgeScope:
    """一次查询的**权威权限口径**（四路共用，只推导一次）。

    Attributes:
        domain_codes: 权威可见域集合（已含请求 scope 的收窄结果）。
        org_codes: 权威可见组织集合。
        entity_types: 权威可见业务对象集合。
        max_security_level: 可见文档的最高安全等级。
        is_admin: 是否 admin（admin 可见全部 enabled 的域）。
    """

    domain_codes: tuple[str, ...]
    org_codes: tuple[str, ...]
    entity_types: tuple[str, ...]
    max_security_level: str
    is_admin: bool

    def isDomainVisible(self, domainCode: str) -> bool:
        """该域对当前用户是否可见。"""
        return domainCode in self.domain_codes


# ---------------------------------------------------------------------------
# 纯推导（可单测；DB 读取在下面的 resolveKnowledgeScope）
# ---------------------------------------------------------------------------


def deriveVisibleDomains(
    domainRows: Sequence[tuple[str, str | None]],
    *,
    departments: Sequence[str],
    isAdmin: bool,
) -> tuple[str, ...]:
    """推导可见域集合。

    Args:
        domainRows: (域 code, 主组织 code) 列表；主组织为 `None` ⇒ 未设主组织。
        departments: 当前用户的组织 code 集合。
        isAdmin: 是否 admin。

    Returns:
        可见域 code（**已排序** ⇒ 结果可复现、可比对）。
    """
    if isAdmin:
        return tuple(sorted(code for code, _orgCode in domainRows))
    allowed = set(departments)
    return tuple(
        sorted(
            code
            for code, orgCode in domainRows
            if orgCode and orgCode in allowed
        )
    )


def narrowDomainsByOrgs(
    domainRows: Sequence[tuple[str, str | None]],
    *,
    visibleDomains: Sequence[str],
    allowedOrgCodes: Sequence[str] | None,
) -> tuple[str, ...]:
    """按组织收窄可见域集合。

    Args:
        domainRows: (域 code, 主组织 code) 列表。
        visibleDomains: 收窄前的可见域集合。
        allowedOrgCodes: 允许的组织 code；**`None` = 不按组织收窄**。

    Returns:
        收窄后的域集合（已排序）。域必须有主组织且主组织 ∈ 允许集合才会留下。
    """
    if allowedOrgCodes is None:
        return tuple(sorted(visibleDomains))
    allowed = set(allowedOrgCodes)
    visible = set(visibleDomains)
    return tuple(
        sorted(
            code
            for code, orgCode in domainRows
            if code in visible and orgCode and orgCode in allowed
        )
    )


def deriveEntityTypes(
    boRows: Sequence[tuple[str, str]],
    *,
    visibleDomains: Sequence[str],
) -> tuple[str, ...]:
    """推导可见业务对象集合（可见域下的业务对象，已排序）。

    Args:
        boRows: (业务对象 code, 归属域 code) 行。**含主域与附域两套行**
            （`business_object.primary_domain_id` + `business_object_domain`）
            ⇒ 与域切片的归属口径一致（切片也把「附了该域」的业务对象算进去）。
        visibleDomains: 可见域集合。

    Returns:
        可见业务对象 code（已排序）。
    """
    visible = set(visibleDomains)
    return tuple(sorted(code for code, domainCode in boRows if domainCode in visible))


def narrowEntityTypes(
    current: Sequence[str],
    requested: Sequence[str],
) -> tuple[str, ...]:
    """请求里的业务对象只能**收窄**权威集合（空请求 = 不收窄）。"""
    if not requested:
        return tuple(current)
    allowed = set(requested)
    return tuple(code for code in current if code in allowed)


def narrowSecurityLevel(current: str, requested: str | None) -> str:
    """请求里的安全等级只能**往下**收窄（`min`）；`None` = 不指定（不收窄）。"""
    if requested is None:
        return current
    currentOrder = _SECURITY_LEVEL_ORDER.get(current, _SECURITY_LEVEL_ORDER[DEFAULT_MAX_SECURITY_LEVEL])
    requestedOrder = _SECURITY_LEVEL_ORDER.get(requested, _SECURITY_LEVEL_ORDER[DEFAULT_MAX_SECURITY_LEVEL])
    return current if currentOrder <= requestedOrder else requested


def resolveAllowedOrgCodes(
    departments: Sequence[str],
    requestedOrgCodes: Sequence[str],
    *,
    isAdmin: bool,
) -> tuple[str, ...] | None:
    """推导「按组织收窄」时允许的组织集合。

    - 请求指定了组织 ⇒ 与用户的组织**取交集**（非 admin）/ 直接采用（admin）。
    - 请求没指定 ⇒ 用用户自己的组织（非 admin）/ **不收窄**（admin，返回 `None`）。
    """
    if requestedOrgCodes:
        if isAdmin:
            return tuple(sorted(set(requestedOrgCodes)))
        allowed = set(departments)
        return tuple(sorted({code for code in requestedOrgCodes if code in allowed}))
    if isAdmin:
        return None
    return tuple(sorted(set(departments)))


def isAdminIdentity(roles: Sequence[str]) -> bool:
    """admin 判定（与仓内 `getAdminOnlyActor` / `requireAdmin` 同口径）。"""
    return ADMIN_ROLE_CODE in set(roles or ())


# ---------------------------------------------------------------------------
# DB 读取
# ---------------------------------------------------------------------------


async def _loadDomainRows(session: AsyncSession) -> list[tuple[str, str | None]]:
    """读取 (域 code, 主组织 code)；主组织为 `None` ⇒ `primary_org_id IS NULL`。"""
    rows = (
        await session.execute(
            select(DataDomain.code, Organization.code)
            .select_from(DataDomain)
            .outerjoin(Organization, Organization.id == DataDomain.primary_org_id)
            .where(DataDomain.enabled.is_(True))
        )
    ).all()
    return [(str(row[0]), row[1] if row[1] is not None else None) for row in rows]


async def _loadBusinessObjectRows(session: AsyncSession) -> list[tuple[str, str]]:
    """读取 (业务对象 code, 归属域 code) 行：**主域 + 附域**两套。

    ⚠️ 必须含附域（`business_object_domain`）：域切片把「附了该域」的业务对象
    也算进该域（`domain_slice.py`）⇒ 权限侧若只看主域，两端口径会漂移
    ⇒ 切片里有、权限侧看不见（或反过来）⇒ 请求侧收窄会误删本域条目。
    """
    primaryRows = (
        await session.execute(
            select(BusinessObject.code, BusinessObject.primary_domain_id)
        )
    ).all()
    attachedRows = (
        await session.execute(
            select(BusinessObjectDomain.bo_code, BusinessObjectDomain.domain_id)
        )
    ).all()
    rows = [(str(row[0]), str(row[1])) for row in primaryRows]
    rows.extend((str(row[0]), str(row[1])) for row in attachedRows)
    return rows


async def resolveKnowledgeScope(
    session: AsyncSession,
    *,
    roles: Sequence[str],
    departments: Sequence[str],
    requestScope: KnowledgeScopeRequest | None = None,
) -> KnowledgeScope:
    """推导一次查询的权威权限口径。

    Args:
        session: 数据库会话（只读）。
        roles: 当前用户的角色 code 集合（`user.roles`）。
        departments: 当前用户的组织 code 集合（`user.departments`）。
        requestScope: 请求侧 scope（**只能收窄**）。

    Returns:
        `KnowledgeScope`；四路共用这一个对象 ⇒ 权限判定同源。
    """
    isAdmin = isAdminIdentity(roles)
    domainRows = await _loadDomainRows(session)
    visible = deriveVisibleDomains(
        domainRows, departments=departments, isAdmin=isAdmin
    )
    allowedOrgCodes = resolveAllowedOrgCodes(
        departments,
        requestScope.org_codes if requestScope else (),
        isAdmin=isAdmin,
    )
    visible = narrowDomainsByOrgs(
        domainRows, visibleDomains=visible, allowedOrgCodes=allowedOrgCodes
    )
    boRows = await _loadBusinessObjectRows(session)
    entityTypes = deriveEntityTypes(boRows, visibleDomains=visible)
    if requestScope:
        entityTypes = narrowEntityTypes(entityTypes, requestScope.entity_types)
    securityLevel = ADMIN_SECURITY_LEVEL if isAdmin else DEFAULT_MAX_SECURITY_LEVEL
    if requestScope:
        securityLevel = narrowSecurityLevel(
            securityLevel, requestScope.max_security_level
        )
    return KnowledgeScope(
        domain_codes=visible,
        org_codes=allowedOrgCodes if allowedOrgCodes is not None else (),
        entity_types=entityTypes,
        max_security_level=securityLevel,
        is_admin=isAdmin,
    )


__all__ = [
    "ADMIN_SECURITY_LEVEL",
    "KnowledgeScope",
    "deriveEntityTypes",
    "deriveVisibleDomains",
    "isAdminIdentity",
    "narrowDomainsByOrgs",
    "narrowEntityTypes",
    "narrowSecurityLevel",
    "resolveAllowedOrgCodes",
    "resolveKnowledgeScope",
]
