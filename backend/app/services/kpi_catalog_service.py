"""KPI Catalog 服务（Phase 4.1 + Phase 4.5 governance hardening）。

与 OntologyService.createMetric/updateMetric 同模式，但更轻：
- 不写 Neo4j / Milvus（KPI 是治理层，不进向量检索）
- revision_count 由 service 自增（PUT 时不论改什么都 +1）
- 不强制 metric_id 关联（业务 KPI 可先于技术 metric 存在）

Phase 4.5 governance hardening 增量：
- deleteKpi / updateKpi 走 AclService.assertCanModify（owner-based ACL）

feat-audit-outbox 增量：
- createKpi / updateKpi / deleteKpi 只入队 audit_outbox（同事务）；
  audit_log / kpi_catalog_history 由独立 worker 进程消费写入（审计失败不回滚业务）
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import KpiCatalog
from app.domain.schemas import KpiCatalogCreate, KpiCatalogUpdate
from app.services.acl_service import AclService
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.outbox_service import OutboxService
from app.services.messages_zh import (
    MSG_KPI_CATALOG_DUPLICATE_CODE,
    MSG_KPI_CATALOG_NOT_FOUND,
    MSG_KPI_CATALOG_STATUS_NULL,
)

logger = logging.getLogger(__name__)


class KpiCatalogService:
    """KPI 业务目录 CRUD 服务。"""

    def __init__(
        self,
        outbox: OutboxService | None = None,
        acl: AclService | None = None,
    ) -> None:
        # 默认实例：service 内部 new；测试可注入 mock
        self._outbox = outbox or OutboxService()
        self._acl = acl or AclService()

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
        self,
        session: AsyncSession,
        dto: KpiCatalogCreate,
        actor: CurrentUser,
    ) -> KpiCatalog:
        """创建 KPI。重复 kpi_code 抛 ConflictError（DB unique 兜底）。

        Phase 4.5：写入 audit_log（CREATE）+ kpi_catalog_history（revision=0）。
        CREATE 不做 ACL 检查（无既有 entity，无 owner 比较意义；任何部门都能创建新 KPI）。
        """
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
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise ConflictError(
                MSG_KPI_CATALOG_DUPLICATE_CODE.format(code=dto.kpi_code)
            ) from exc
        # outbox 入队（同一事务绑定）：worker 消费后写 audit_log + history
        await self._outbox.enqueue(
            session,
            event_type="kpi_created",
            entity_type="kpi_catalog",
            entity_id=entity.id,
            actor=actor.userId,
            actor_departments=actor.departments,
            payload={"after": _entityToDict(entity)},
        )
        await session.commit()
        await session.refresh(entity)
        await get_kpi_match_cache().refreshOne(session, entity.kpi_code)
        logger.info("创建 KPI Catalog id=%d code=%s", entity.id, entity.kpi_code)
        return entity

    async def updateKpi(
        self,
        session: AsyncSession,
        id: int,
        dto: KpiCatalogUpdate,
        actor: CurrentUser,
    ) -> KpiCatalog:
        """更新 KPI。revision_count 始终 +1（PUT 即视为一次修订）。

        Phase 4.5：先 ACL 检查（owner 不匹配 + 非 admin → PermissionDeniedError），
        再 audit_log（UPDATE，before+after）+ kpi_catalog_history（revision=new）。
        """
        entity = await self.getKpi(session, id)
        # ACL：必须在改之前检查；通过后才允许修改
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="KPI",
            entity_code=entity.kpi_code,
        )
        before = _entityToDict(entity)
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
        # outbox 入队（同一事务绑定）：worker 消费后写 audit_log + history
        await self._outbox.enqueue(
            session,
            event_type="kpi_updated",
            entity_type="kpi_catalog",
            entity_id=entity.id,
            actor=actor.userId,
            actor_departments=actor.departments,
            payload={"before": before, "after": _entityToDict(entity)},
        )
        await session.commit()
        await session.refresh(entity)
        await get_kpi_match_cache().refreshOne(session, entity.kpi_code)
        logger.info(
            "更新 KPI Catalog id=%d revision=%d by %s",
            id,
            entity.revision_count,
            actor.userId,
        )
        return entity

    async def deleteKpi(
        self,
        session: AsyncSession,
        id: int,
        actor: CurrentUser,
    ) -> None:
        """删除 KPI。Phase 4.5：先 ACL 检查，再 audit_log（DELETE，before）。"""
        entity = await self.getKpi(session, id)
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="KPI",
            entity_code=entity.kpi_code,
        )
        before = _entityToDict(entity)
        # outbox 入队先于 session.delete（保持 entity 属性可访问），同事务 commit
        await self._outbox.enqueue(
            session,
            event_type="kpi_deleted",
            entity_type="kpi_catalog",
            entity_id=entity.id,
            actor=actor.userId,
            actor_departments=actor.departments,
            payload={"before": before},
        )
        await session.delete(entity)
        await session.commit()
        get_kpi_match_cache().onKpiChanged(entity.kpi_code)
        logger.info("删除 KPI Catalog id=%d by %s", id, actor.userId)


def _entityToDict(entity: KpiCatalog) -> dict:
    """KPI 实体 → JSON 可序列化 dict（用于 audit_log / kpi_catalog_history JSONB 列）。

    datetime / Decimal 等非 JSON-native 类型在 dict 内原样保留会导致 PG JSONB 写入失败
    （`Object of type datetime is not JSON serializable`），故在此统一转字符串/数值。
    """
    out: dict = {}
    for col in entity.__table__.columns.keys():
        v = getattr(entity, col)
        if isinstance(v, datetime):
            out[col] = v.isoformat()
        elif isinstance(v, Decimal):
            out[col] = float(v)
        else:
            out[col] = v
    return out