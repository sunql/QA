"""KPI Catalog 服务（Phase 4.1）。

与 OntologyService.createMetric/updateMetric 同模式，但更轻：
- 不写 Neo4j / Milvus（KPI 是治理层，不进向量检索）
- revision_count 由 service 自增（PUT 时不论改什么都 +1）
- 不强制 metric_id 关联（业务 KPI 可先于技术 metric 存在）
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import KpiCatalog
from app.domain.schemas import KpiCatalogCreate, KpiCatalogUpdate
from app.services.messages_zh import (
    MSG_KPI_CATALOG_DUPLICATE_CODE,
    MSG_KPI_CATALOG_NOT_FOUND,
    MSG_KPI_CATALOG_STATUS_NULL,
)

logger = logging.getLogger(__name__)


class KpiCatalogService:
    """KPI 业务目录 CRUD 服务。"""

    async def listKpis(self, session: AsyncSession) -> list[KpiCatalog]:
        """按 kpi_code 升序列表。"""
        result = await session.execute(
            select(KpiCatalog).order_by(KpiCatalog.kpi_code)
        )
        return list(result.scalars().all())

    async def getKpi(self, session: AsyncSession, id: int) -> KpiCatalog:
        row = await session.get(KpiCatalog, id)
        if row is None:
            raise NotFoundError(MSG_KPI_CATALOG_NOT_FOUND.format(id=id))
        return row

    async def createKpi(
        self, session: AsyncSession, dto: KpiCatalogCreate
    ) -> KpiCatalog:
        """创建 KPI。重复 kpi_code 抛 ConflictError（DB unique 兜底）。"""
        entity = KpiCatalog(
            kpi_code=dto.kpi_code,
            kpi_name=dto.kpi_name,
            business_definition=dto.business_definition,
            formula=dto.formula,
            numerator=dto.numerator,
            denominator=dto.denominator,
            grain=dto.grain,
            unit=dto.unit,
            data_source=dto.data_source,
            owner=dto.owner,
            version=dto.version or "v1.0",
            status=dto.status.value,
            metric_id=dto.metric_id,
            created_by=dto.created_by,
        )
        session.add(entity)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ConflictError(
                MSG_KPI_CATALOG_DUPLICATE_CODE.format(code=dto.kpi_code)
            ) from exc
        await session.refresh(entity)
        logger.info("创建 KPI Catalog id=%d code=%s", entity.id, entity.kpi_code)
        return entity

    async def updateKpi(
        self, session: AsyncSession, id: int, dto: KpiCatalogUpdate
    ) -> KpiCatalog:
        """更新 KPI。revision_count 始终 +1（PUT 即视为一次修订）。"""
        entity = await self.getKpi(session, id)
        updates = dto.model_dump(exclude_unset=True)
        # status 是 NOT NULL 列；显式 null 应被拒绝（前端清空表单用「不改」而非「传 null」）
        if "status" in updates and updates["status"] is None:
            raise ValidationError(MSG_KPI_CATALOG_STATUS_NULL)
        # status / metric_id 是枚举/int；service 层把 KpiStatus 归一为字符串
        if "status" in updates and updates["status"] is not None:
            updates["status"] = updates["status"].value
        for key, value in updates.items():
            setattr(entity, key, value)
        entity.revision_count += 1
        await session.commit()
        await session.refresh(entity)
        logger.info("更新 KPI Catalog id=%d revision=%d", id, entity.revision_count)
        return entity

    async def deleteKpi(self, session: AsyncSession, id: int) -> None:
        entity = await self.getKpi(session, id)
        await session.delete(entity)
        await session.commit()
        logger.info("删除 KPI Catalog id=%d", id)
