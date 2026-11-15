"""P3a D19：治理维度解析（PG 治理表 → Milvus v2 过滤维度）。

维度映射规则（P3a D18 定稿，见 `docs/update/1109/p0/milvus-方案.md` §4.3）
------------------------------------------------------------------------
- **class**：``business_object.header_class_id = class.id`` ⇒ 主域 + 附域 + BO 码；
  组织 = 域的 ``primary_org_id`` → ``organizations.code`` ∪ ``class.object_owner``
  （后者按组织码**精确匹配**，匹配不到就**不并入**——宁可少一个维度，
  不可猜一个错组织）；
- **property**：继承其 class 的全部维度；
- **metric**：经 ``target_class_id`` → class 的维度；
- **kpi**：经 ``metric_id`` → metric → class 的维度。

三条不可让步的口径
------------------
1. **链路断了就留空**（空数组 / 0），不可瞎猜一个域 —— 空维度在域过滤下
   是 fail-closed（被排除），是**可解释**的收窄；
2. **顺序稳定**（域码/组织码排序后去重）⇒ 回填幂等、影子比对逐字可比；
3. **有效期用 epoch 毫秒**（Milvus 的 INT64 范围比较实测最稳），
   ``valid_to`` 为空 ⇒ 0（= 长期有效的哨兵）。

⚠️ 这里**没有**授权策略
----------------------
本模块只回答「这条向量行属于哪些域/组织/实体」，**不回答**「谁能看到它」。
本仓至今没有「用户 → 可见域/组织」的策略源（现有 ACL 只有 admin 级的
``assertCanModify``）⇒ 过滤维度由调用方显式给出，策略接线是另一次受控变更。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    BusinessObject,
    BusinessObjectDomain,
    DataDomain,
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyProperty,
)
from app.models.rbac import Organization

# 可解析维度的实体类型（与 Milvus 的 type 域同名）。
RESOLVABLE_TYPES: tuple[str, ...] = ("class", "property", "metric", "kpi")


@dataclass(frozen=True)
class DimensionSet:
    """一条向量行的治理维度（不可变）。

    Attributes:
        domainCodes: 数据域码（主域 + 附域），已排序去重。
        orgCodes: 组织码，已排序去重。
        entityType: 业务对象码；``""`` = 非 BO 头类。
        validFrom: 有效期起点 epoch 毫秒；0 = 自始。
        validTo: 有效期终点 epoch 毫秒；0 = 长期有效。
        releaseId: 语义发布 id；0 = 无激活发布。
    """

    domainCodes: tuple[str, ...] = ()
    orgCodes: tuple[str, ...] = ()
    entityType: str = ""
    validFrom: int = 0
    validTo: int = 0
    releaseId: int = 0


# ---------------------------------------------------------------------------
# 纯映射
# ---------------------------------------------------------------------------


def _epochMs(value: datetime | None) -> int:
    """datetime → epoch 毫秒；``None`` → 0（哨兵）。

    naive datetime 视作 UTC（PG 侧 ``DateTime(timezone=True)`` 回读带 tzinfo，
    但手工构造/迁移遗留行可能是 naive ⇒ 显式补 tz，避免「本地时区偏移」
    把有效期整体平移几个小时）。
    """
    if value is None:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _sortedCodes(values: Any) -> tuple[str, ...]:
    """去空、去重、排序 —— 顺序稳定 ⇒ 回填幂等、影子比对逐字可比。

    ``None`` 与空串都算「没有这个维度」⇒ 跳过（不可 ``str(None)`` 变成 "None"）。
    """
    codes: set[str] = set()
    for value in values or ():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            codes.add(text)
    return tuple(sorted(codes))


def buildClassDimension(
    *,
    validFrom: datetime | None,
    validTo: datetime | None,
    boCode: str | None,
    primaryDomainCode: str | None,
    attachedDomainCodes: tuple[str, ...],
    orgCodes: tuple[str, ...],
    releaseId: int = 0,
) -> DimensionSet:
    """把类的治理事实装配成维度集合（纯函数，不碰 DB）。

    这是维度装配的**唯一**入口：解析层（DB → 事实）与装配层（事实 → 维度）
    分开 ⇒ 装配逻辑可被纯单测覆盖，不用连库。
    """
    domains = _sortedCodes([primaryDomainCode, *attachedDomainCodes])
    return DimensionSet(
        domainCodes=domains,
        orgCodes=_sortedCodes(orgCodes),
        entityType=(boCode or "").strip(),
        validFrom=_epochMs(validFrom),
        validTo=_epochMs(validTo),
        releaseId=int(releaseId or 0),
    )


# ---------------------------------------------------------------------------
# 真实 PG 解析
# ---------------------------------------------------------------------------


async def resolveDimensions(
    session: AsyncSession,
    type_: str,
    ids: list[int] | tuple[int, ...],
) -> dict[int, DimensionSet]:
    """解析一批实体的治理维度。

    Args:
        session: 异步会话。
        type_: 实体类型（class / property / metric / kpi）。
        ids: 实体 id 列表。

    Returns:
        ``{实体 id: DimensionSet}``；查不到的行**不出现**在结果里
        （调用方按缺省处理 ⇒ 等价于空维度）。

    Raises:
        ValueError: ``type_`` 不是受支持的类型。
    """
    if type_ not in RESOLVABLE_TYPES:
        raise ValueError(f"unknown dimension type: {type_!r}")
    if not ids:
        return {}

    if type_ == "class":
        return await _resolveClassDimensions(session, list(ids))
    if type_ == "property":
        return await _resolvePropertyDimensions(session, list(ids))
    if type_ == "metric":
        return await _resolveMetricDimensions(session, list(ids))
    return await _resolveKpiDimensions(session, list(ids))


async def _resolvePropertyDimensions(
    session: AsyncSession, ids: list[int]
) -> dict[int, DimensionSet]:
    """属性 → 其 class 的维度（class 查不到 ⇒ 空维度）。"""
    rows = (
        await session.execute(
            select(OntologyProperty).where(OntologyProperty.id.in_(ids))
        )
    ).scalars().all()
    if not rows:
        return {}
    classDims = await _resolveClassDimensions(
        session, list({row.class_id for row in rows})
    )
    return {
        row.id: classDims.get(row.class_id, DimensionSet()) for row in rows
    }


async def _resolveMetricDimensions(
    session: AsyncSession, ids: list[int]
) -> dict[int, DimensionSet]:
    """指标 → 其 target_class 的维度（无 target ⇒ 空维度）。"""
    rows = (
        await session.execute(select(OntologyMetric).where(OntologyMetric.id.in_(ids)))
    ).scalars().all()
    if not rows:
        return {}
    classIds = [row.target_class_id for row in rows if row.target_class_id]
    classDims = await _resolveClassDimensions(session, classIds)
    return {
        row.id: classDims.get(row.target_class_id, DimensionSet()) for row in rows
    }


async def _resolveKpiDimensions(
    session: AsyncSession, ids: list[int]
) -> dict[int, DimensionSet]:
    """KPI → 其 metric → class 的维度（链路断 ⇒ 空维度）。"""
    rows = (
        await session.execute(select(KpiCatalog).where(KpiCatalog.id.in_(ids)))
    ).scalars().all()
    if not rows:
        return {}
    metricIds = [row.metric_id for row in rows if row.metric_id]
    metricDims = await _resolveMetricDimensions(session, metricIds)
    return {
        row.id: metricDims.get(row.metric_id, DimensionSet()) for row in rows
    }


async def _resolveClassDimensions(
    session: AsyncSession, classIds: list[int]
) -> dict[int, DimensionSet]:
    """类 → 域/组织/实体/有效期。

    关联链：``ontology_class`` → ``business_object``（头类唯一）→
    ``data_domain``（主域）＋ ``business_object_domain``（附域）→
    ``organizations``（主管理组织）。
    """
    if not classIds:
        return {}

    classRows = (
        await session.execute(
            select(OntologyClass).where(OntologyClass.id.in_(classIds))
        )
    ).scalars().all()
    if not classRows:
        return {}

    boRows = (
        await session.execute(
            select(BusinessObject).where(
                BusinessObject.header_class_id.in_(classIds)
            )
        )
    ).scalars().all()
    boByClass = {bo.header_class_id: bo for bo in boRows}

    attachedByBo = await _attachedDomains(session, [bo.code for bo in boRows])
    domainRows = await _domainsByCode(
        session, _domainCodes(boRows, attachedByBo)
    )
    orgCodeById = await _orgCodesById(
        session,
        [row.primary_org_id for row in domainRows.values() if row.primary_org_id],
    )
    ownerOrgCodes = await _matchedOwnerOrgCodes(
        session, [row.object_owner for row in classRows if row.object_owner]
    )

    result: dict[int, DimensionSet] = {}
    for cls in classRows:
        bo = boByClass.get(cls.id)
        primaryDomain = bo.primary_domain_id if bo else None
        orgCodes: list[str] = []
        if primaryDomain:
            domain = domainRows.get(primaryDomain)
            if domain and domain.primary_org_id:
                code = orgCodeById.get(domain.primary_org_id)
                if code:
                    orgCodes.append(code)
        if cls.object_owner and cls.object_owner in ownerOrgCodes:
            orgCodes.append(cls.object_owner)
        result[cls.id] = buildClassDimension(
            validFrom=cls.valid_from,
            validTo=cls.valid_to,
            boCode=bo.code if bo else None,
            primaryDomainCode=primaryDomain,
            attachedDomainCodes=tuple(attachedByBo.get(bo.code, ())) if bo else (),
            orgCodes=tuple(orgCodes),
        )
    return result


async def _attachedDomains(
    session: AsyncSession, boCodes: list[str]
) -> dict[str, list[str]]:
    """BO → 附域码列表（主域不在这里）。"""
    if not boCodes:
        return {}
    rows = (
        await session.execute(
            select(BusinessObjectDomain).where(
                BusinessObjectDomain.bo_code.in_(boCodes)
            )
        )
    ).scalars().all()
    attached: dict[str, list[str]] = {}
    for row in rows:
        attached.setdefault(row.bo_code, []).append(row.domain_id)
    return attached


def _domainCodes(
    boRows: list[BusinessObject], attachedByBo: dict[str, list[str]]
) -> list[str]:
    """主域 + 附域的并集（去重）。"""
    codes: set[str] = {bo.primary_domain_id for bo in boRows}
    for extra in attachedByBo.values():
        codes.update(extra)
    return list(codes)


async def _domainsByCode(
    session: AsyncSession, codes: list[str]
) -> dict[str, DataDomain]:
    if not codes:
        return {}
    rows = (
        await session.execute(select(DataDomain).where(DataDomain.code.in_(codes)))
    ).scalars().all()
    return {row.code: row for row in rows}


async def _orgCodesById(
    session: AsyncSession, orgIds: list[int]
) -> dict[int, str]:
    """组织 id → 组织码。"""
    if not orgIds:
        return {}
    rows = (
        await session.execute(select(Organization).where(Organization.id.in_(orgIds)))
    ).scalars().all()
    return {row.id: row.code for row in rows}


async def _matchedOwnerOrgCodes(
    session: AsyncSession, owners: list[str]
) -> set[str]:
    """``class.object_owner`` 里**能精确匹配到组织码**的那些。

    匹配不到就不并入（object_owner 允许是人名/自由文本）⇒ 宁可少一个维度，
    不可猜一个错组织（错的组织码会让过滤结果「看起来对但其实是另一条链」）。
    """
    if not owners:
        return set()
    rows = (
        await session.execute(
            select(Organization).where(Organization.code.in_(owners))
        )
    ).scalars().all()
    return {row.code for row in rows}
