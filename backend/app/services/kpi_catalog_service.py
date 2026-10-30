"""KPI Catalog service.

Legacy rows: kpi_catalog.datasource_id IS NULL（migration 0125 之前的历史行 +
未填的运营行）— chat 路径 L1 mismatch 检查会跳过（per chat_stream_output.py:197
"NULL ⇒ 不校验"分支），不警告。要让警告生效，必须先 PUT 一次以填补 datasource_id。
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import KpiStatus
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import DataSource, KpiCatalog
from app.domain.schemas import KpiCatalogCreate, KpiCatalogUpdate
from app.services.acl_service import AclService
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_metric_publish_gate import enforceKpiPublishGate
from app.services.kpi_metric_reconcile import GovernanceSpec
from app.services.messages_zh import (
    MSG_KPI_CATALOG_DUPLICATE_CODE,
    MSG_KPI_CATALOG_NOT_FOUND,
    MSG_KPI_CATALOG_STATUS_NULL,
    MSG_KPI_DATASOURCE_INACTIVE,
    MSG_KPI_DATASOURCE_NOT_FOUND,
)
from app.services.outbox_service import OutboxService

logger = logging.getLogger(__name__)

# 治理侧结构化列（改动它们 ⇒ 闸门必须按**新值**判对账，而不是旧口径）
_GOVERNANCE_KEYS = ("numerator_columns", "denominator_columns", "grain_keys")

# match_threshold 的默认值：与 DB server_default 一致（models.KpiCatalog 处为
# Numeric(3, 2), nullable=False, default=Decimal("0.75")）。create / update 两条路径
# 共用，避免「一边归一、一边写穿 NOT NULL」的不对称。
_DEFAULT_MATCH_THRESHOLD = Decimal("0.75")


def _governanceOverride(
    entity: KpiCatalog, updates: dict
) -> GovernanceSpec | None:
    """本次请求改动了治理列 ⇒ 用新旧合并后的治理快照；否则返回 None（用实体现值）。"""
    if not any(key in updates for key in _GOVERNANCE_KEYS):
        return None
    return GovernanceSpec(
        numerator_columns=tuple(
            updates.get("numerator_columns", entity.numerator_columns) or ()
        ),
        denominator_columns=tuple(
            updates.get("denominator_columns", entity.denominator_columns) or ()
        ),
        grain_keys=tuple(updates.get("grain_keys", entity.grain_keys) or ()),
    )


def _publishGateOverrides(entity: KpiCatalog, updates: dict) -> dict:
    """闸门覆盖参数：只覆盖本次请求**明确改动**的字段（显式 null = 解绑）。"""
    overrides: dict = {}
    if "metric_id" in updates:
        overrides["metricId"] = updates["metric_id"]
    if "datasource_id" in updates:
        overrides["datasourceId"] = updates["datasource_id"]
    governance = _governanceOverride(entity, updates)
    if governance is not None:
        overrides["governance"] = governance
    return overrides


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

    async def listKpis(
        self, session: AsyncSession, *, unbound: bool = False
    ) -> list[KpiCatalog]:
        """按 kpi_code 升序列表；``unbound`` 只返回未绑定数据源的 KPI（P2 报告用）。"""
        stmt = select(KpiCatalog).order_by(KpiCatalog.kpi_code)
        if unbound:
            stmt = stmt.where(KpiCatalog.datasource_id.is_(None))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def getKpi(self, session: AsyncSession, id: int) -> KpiCatalog:
        row = await session.get(KpiCatalog, id)
        if row is None:
            raise NotFoundError(MSG_KPI_CATALOG_NOT_FOUND.format(id=id))
        return row

    async def get_by_code(self, session: AsyncSession, code: str) -> KpiCatalog | None:
        """按 kpi_code 查，无则返回 None（用于 idempotency 检查）。"""
        result = await session.execute(
            select(KpiCatalog).where(KpiCatalog.kpi_code == code)
        )
        return result.scalar_one_or_none()

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
            datasource_id=dto.datasource_id,  # F2 §4.2: 持久化到列（subagent Task 3 漏写，单独 commit 补）
            metric_id=dto.metric_id,
            created_by=dto.created_by,
            semantic_keywords=dto.semantic_keywords,
            match_threshold=(
                Decimal(str(dto.match_threshold))
                if dto.match_threshold is not None
                else _DEFAULT_MATCH_THRESHOLD
            ),
            # P1：治理侧结构化列（漏写 ⇒ 对账恒 unknown / 闸门恒拦）
            grain_keys=dto.grain_keys,
            numerator_columns=dto.numerator_columns,
            denominator_columns=dto.denominator_columns,
        )
        # F2 §4.2: 校验 datasource 存在 + is_active
        ds = await session.get(DataSource, dto.datasource_id)
        if ds is None:
            raise ValidationError(MSG_KPI_DATASOURCE_NOT_FOUND.format(id=dto.datasource_id))
        if not ds.is_active:
            raise ValidationError(MSG_KPI_DATASOURCE_INACTIVE.format(id=ds.id, name=ds.name))
        # P1 发布闸门：创建即 PUBLISHED 也要过闸（不通过 ⇒ 409，且不入库）
        if dto.status == KpiStatus.PUBLISHED:
            await enforceKpiPublishGate(session, entity)
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
        await get_kpi_match_cache().refreshOne(
            session, entity.kpi_code, kpiId=entity.id
        )
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
        # F2 §4.2: updateKpi 仅在 datasource_id 非 None 时校验
        if dto.datasource_id is not None:
            ds = await session.get(DataSource, dto.datasource_id)
            if ds is None:
                raise ValidationError(MSG_KPI_DATASOURCE_NOT_FOUND.format(id=dto.datasource_id))
            if not ds.is_active:
                raise ValidationError(MSG_KPI_DATASOURCE_INACTIVE.format(id=ds.id, name=ds.name))
        before = _entityToDict(entity)
        updates = dto.model_dump(exclude_unset=True)
        # status 是 NOT NULL 列；显式 null 应被拒绝（前端清空表单用「不改」而非「传 null」）
        if "status" in updates and updates["status"] is None:
            raise ValidationError(MSG_KPI_CATALOG_STATUS_NULL)
        # 目标状态（枚举成员，归一前取）—— 发布闸门据此判断是否要过闸
        targetStatus = updates.get("status")
        # status / metric_id 是枚举/int；service 层把 KpiStatus 归一为字符串
        if "status" in updates and updates["status"] is not None:
            updates["status"] = updates["status"].value
        # Numeric 列归一为 Decimal：asyncpg 不接受 float 直接绑到 Numeric。
        # match_threshold 是 NOT NULL 列（DB 默认 0.75）；创建路径已把 None 归一为默认值
        # （见 createKpi），update 路径必须对称，否则显式 null 会写穿 NOT NULL 约束 ⇒
        # commit 抛 IntegrityError ⇒ 无全局处理器 ⇒ 500。
        # 语义：「显式 null = 复位成默认阈值」，与创建路径一致（status 没有默认值，故不做同样处理）。
        if "match_threshold" in updates:
            updates["match_threshold"] = (
                Decimal(str(updates["match_threshold"]))
                if updates["match_threshold"] is not None
                else _DEFAULT_MATCH_THRESHOLD
            )
        # P1 发布闸门：发布前过闸；按**新值**判定（同一请求里改挂靠/治理列也要算）
        if targetStatus == KpiStatus.PUBLISHED:
            await enforceKpiPublishGate(
                session, entity, **_publishGateOverrides(entity, updates)
            )
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
        await get_kpi_match_cache().refreshOne(
            session, entity.kpi_code, kpiId=entity.id
        )
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
        get_kpi_match_cache().onKpiChanged(entity.kpi_code, kpiId=entity.id)
        logger.info("删除 KPI Catalog id=%d by %s", id, actor.userId)


def _entityToDict(entity: KpiCatalog) -> dict:
    """KPI 实体 → JSON 可序列化 dict（用于 audit_log / kpi_catalog_history JSONB 列）。

    datetime / Decimal 等非 JSON-native 类型在 dict 内原样保留会导致 PG JSONB 写入失败
    （`Object of type datetime is not JSON serializable`），故在此统一转字符串/数值。
    """
    out: dict = {}
    for col in entity.__table__.columns:
        v = getattr(entity, col.name)
        if isinstance(v, datetime):
            out[col.name] = v.isoformat()
        elif isinstance(v, Decimal):
            out[col.name] = float(v)
        else:
            out[col.name] = v
    return out