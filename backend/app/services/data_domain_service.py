"""数据域基础数据 CRUD service（feat-data-domain-layer 方案 1）.

SSOT = data_domain 表；Agent 授权域词表的 DB 来源（迁移自
agent_vocabulary.AGENT_DATA_DOMAINS 常量，常量降级为 registry 未 warmUp fallback）。

守卫：
  - create: code 唯一（409）；primary_org_id 存在（若提供）
  - update: org 存在（若变更）
  - delete: 三处引用检查（business_object.primary_domain_id /
    business_object_domain / agent_definition.data_domains JSONB）→ 409
写后调 dataDomainRegistry.reloadOne（registry 同款写时失效）。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    AgentDefinition,
    BusinessObject,
    BusinessObjectDomain,
    DataDomain,
    Organization,
)
from app.domain.schemas import (
    DataDomainCreate,
    DataDomainRead,
    DataDomainUpdate,
)
from app.domain.exceptions import ConflictError, NotFoundError
from app.services.data_domain_registry import dataDomainRegistry
from app.services.messages_zh import (
    MSG_DATA_DOMAIN_CODE_EXISTS,
    MSG_DATA_DOMAIN_IN_USE,
    MSG_DATA_DOMAIN_NOT_FOUND,
    MSG_DATA_DOMAIN_ORG_NOT_FOUND,
)


def toRead(row: DataDomain, orgName: str | None = None) -> DataDomainRead:
    return DataDomainRead(
        code=row.code,
        name=row.name,
        primaryOrgId=row.primary_org_id,
        primaryOrgName=orgName,
        description=row.description,
        enabled=row.enabled,
        createdTime=row.created_time,
        updatedTime=row.updated_time,
    )


class DataDomainService:
    """数据域 CRUD + 守卫。"""

    async def listDomains(
        self, session: AsyncSession, *, enabledOnly: bool = False
    ) -> list[DataDomainRead]:
        """管理页要看到禁用域，默认全量；Agent 侧下拉由 registry enabled 集过滤。"""
        stmt = select(DataDomain, Organization.name).outerjoin(
            Organization, DataDomain.primary_org_id == Organization.id
        ).order_by(DataDomain.code)
        rows = (await session.execute(stmt)).all()
        return [
            toRead(row, orgName)
            for row, orgName in rows
            if not enabledOnly or row.enabled
        ]

    async def getDomain(self, session: AsyncSession, code: str) -> DataDomain:
        row = await session.get(DataDomain, code)
        if row is None:
            raise NotFoundError(MSG_DATA_DOMAIN_NOT_FOUND.format(code=code))
        return row

    async def getDomainRead(
        self, session: AsyncSession, code: str
    ) -> DataDomainRead:
        """单行带主管理组织名。"""
        row = await self.getDomain(session, code)
        orgName: str | None = None
        if row.primary_org_id is not None:
            orgName = (
                await session.execute(
                    select(Organization.name).where(Organization.id == row.primary_org_id)
                )
            ).scalar_one_or_none()
        return toRead(row, orgName)

    async def _assertOrgExists(
        self, session: AsyncSession, orgId: int | None
    ) -> None:
        if orgId is None:
            return
        exists = (
            await session.execute(
                select(func.count()).select_from(Organization).where(
                    Organization.id == orgId
                )
            )
        ).scalar()
        if not exists:
            raise NotFoundError(MSG_DATA_DOMAIN_ORG_NOT_FOUND.format(org_id=orgId))

    async def createDomain(
        self, session: AsyncSession, dto: DataDomainCreate, actor: str
    ) -> DataDomainRead:
        existing = await session.get(DataDomain, dto.code)
        if existing is not None:
            raise ConflictError(
                MSG_DATA_DOMAIN_CODE_EXISTS.format(code=dto.code)
            )
        await self._assertOrgExists(session, dto.primary_org_id)
        row = DataDomain(
            code=dto.code,
            name=dto.name,
            primary_org_id=dto.primary_org_id,
            description=dto.description,
            enabled=dto.enabled,
            created_by=actor,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        await dataDomainRegistry.reloadOne(session, row.code)
        return toRead(row)

    async def updateDomain(
        self, session: AsyncSession, code: str, dto: DataDomainUpdate
    ) -> DataDomainRead:
        row = await self.getDomain(session, code)
        updates = dto.model_dump(exclude_unset=True)
        if "primary_org_id" in updates:
            await self._assertOrgExists(session, updates["primary_org_id"])
        # 不可变构造（business_object_service.updateObject 同款）
        updated = DataDomain(
            code=row.code,
            name=updates.get("name", row.name),
            primary_org_id=updates.get("primary_org_id", row.primary_org_id),
            description=updates.get("description", row.description),
            enabled=updates.get("enabled", row.enabled),
            created_by=row.created_by,
        )
        updated = await session.merge(updated)
        await session.commit()
        await session.refresh(updated)
        await dataDomainRegistry.reloadOne(session, code)
        return toRead(updated)

    async def deleteDomain(self, session: AsyncSession, code: str) -> None:
        row = await self.getDomain(session, code)
        refs: list[str] = []

        boPrimary = (
            await session.execute(
                select(func.count()).select_from(BusinessObject).where(
                    BusinessObject.primary_domain_id == code
                )
            )
        ).scalar()
        if boPrimary:
            refs.append(f"business_object.primary_domain_id({boPrimary})")

        boAttached = (
            await session.execute(
                select(func.count()).select_from(BusinessObjectDomain).where(
                    BusinessObjectDomain.domain_id == code
                )
            )
        ).scalar()
        if boAttached:
            refs.append(f"business_object_domain({boAttached})")

        agentUses = (
            await session.execute(
                select(func.count()).select_from(AgentDefinition).where(
                    AgentDefinition.data_domains.contains([code])
                )
            )
        ).scalar()
        if agentUses:
            refs.append(f"agent_definition.data_domains({agentUses})")

        if refs:
            raise ConflictError(
                MSG_DATA_DOMAIN_IN_USE.format(code=code, refs=", ".join(refs))
            )
        await session.delete(row)
        await session.commit()
        await dataDomainRegistry.reloadOne(session, code)
