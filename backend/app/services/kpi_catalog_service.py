"""KPI Catalog service.

Legacy rows: kpi_catalog.datasource_id IS NULL（migration 0125 之前的历史行 +
未填的运营行）— chat 路径 L1 mismatch 检查会跳过（per chat_stream_output.py:197
"NULL ⇒ 不校验"分支），不警告。要让警告生效，必须先 PUT 一次以填补 datasource_id。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
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
from app.services.messages_zh import (
    MSG_KPI_CATALOG_DUPLICATE_CODE,
    MSG_KPI_CATALOG_NOT_FOUND,
    MSG_KPI_CATALOG_STATUS_NULL,
    MSG_KPI_DATASOURCE_INACTIVE,
    MSG_KPI_DATASOURCE_NOT_FOUND,
)
from app.services.outbox_service import OutboxService

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
            match_threshold=Decimal(str(dto.match_threshold)) if dto.match_threshold is not None else Decimal("0.75"),
        )
        # F2 §4.2: 校验 datasource 存在 + is_active
        ds = await session.get(DataSource, dto.datasource_id)
        if ds is None:
            raise ValidationError(MSG_KPI_DATASOURCE_NOT_FOUND.format(id=dto.datasource_id))
        if not ds.is_active:
            raise ValidationError(MSG_KPI_DATASOURCE_INACTIVE.format(id=ds.id, name=ds.name))
        await _warnGrainMismatch(session, dto.metric_id, dto.grain, dto.kpi_code)
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
        # status / metric_id 是枚举/int；service 层把 KpiStatus 归一为字符串
        if "status" in updates and updates["status"] is not None:
            updates["status"] = updates["status"].value
        for key, value in updates.items():
            setattr(entity, key, value)
        entity.revision_count += 1
        # B5：grain 与挂靠 metric 的 dimension_defaults 粒度弱校验（改挂靠或改 grain 时）
        if "metric_id" in updates or "grain" in updates:
            await _warnGrainMismatch(session, entity.metric_id, entity.grain, entity.kpi_code)
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

    async def ensureDraftForMetric(
        self,
        session: AsyncSession,
        metric: "OntologyMetric",
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> KpiCatalog:
        """为 metric 幂等创建 DRAFT 治理户头（B5：消灭「有技术形态无户头」的孤儿）。

        chat `/metric` 只建 ontology_metric ⇒ 指标永远进不了 KPI 直连/报告/治理。
        本方法在该 metric 已有挂靠 KPI 时直接返回（幂等，可重复调用）；否则插入：

        - status=DRAFT：不进 L1 匹配缓存（refreshOne 只收 PUBLISHED），人工补全
          numerator/denominator/grain/semantic_keywords 并发布后才对企业可见；
        - **不走 createKpi**：createKpi 承载 F2 的 datasource 强校验（人工治理入口
          的契约），自动户头在创建时还没有数据源可绑——绕开 dto 直接落实体，
          但保留 outbox（kpi_created）→ audit_log + kpi_catalog_history 的同一管线。
        - formula/numerator/denominator 留空：kpi.formula 在现行契约下是
          FeatureDefinition 锚文本，由 metric.formula 承担技术公式，不双写。

        kpi_code 生成：``KPI_AUTO_M{metric.id}``（确定性，幂等重跑不产生新行）；
        极小概率与手工创建的 code 撞车 → 加时间戳后缀兜底。
        """
        existing = await session.execute(
            select(KpiCatalog).where(KpiCatalog.metric_id == metric.id)
        )
        found = existing.scalar_one_or_none()
        if found is not None:
            logger.info(
                "metric_id=%d 已有挂靠 KPI code=%s，跳过自动建户头",
                metric.id, found.kpi_code,
            )
            return found

        kpiCode = f"KPI_AUTO_M{metric.id}"
        if await self.get_by_code(session, kpiCode) is not None:
            kpiCode = f"{kpiCode}_{int(datetime.now(timezone.utc).timestamp())}"
        entity = KpiCatalog(
            kpi_code=kpiCode,
            kpi_name=metric.metric_name,
            status=KpiStatus.DRAFT.value,
            metric_id=metric.id,
            created_by=actor,
            match_threshold=Decimal("0.75"),
        )
        session.add(entity)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise ConflictError(
                MSG_KPI_CATALOG_DUPLICATE_CODE.format(code=kpiCode)
            ) from exc
        # 与 createKpi 同管线：worker 消费 kpi_created 写 audit_log + history
        await self._outbox.enqueue(
            session,
            event_type="kpi_created",
            entity_type="kpi_catalog",
            entity_id=entity.id,
            actor=actor,
            actor_departments=actor_departments,
            payload={"after": _entityToDict(entity)},
        )
        await session.commit()
        await session.refresh(entity)
        await get_kpi_match_cache().refreshOne(session, entity.kpi_code)
        logger.info(
            "metric_id=%d 自动创建 DRAFT KPI 户头 code=%s by %s",
            metric.id, entity.kpi_code, actor,
        )
        return entity


# B5 弱校验的维度别名表：dimension_defaults 的键多为英文技术名（month/supplier），
# grain 是中文自由文本（「按月+供应商」）——裸子串匹配会恒告警（全是噪声）。
# 命中判定 = 键本身或其常见中文别名出现在 grain 中；别名表外的键按原文匹配。
_DIM_ALIASES: dict[str, tuple[str, ...]] = {
    "month": ("月", "按月"),
    "date": ("日期", "日"),
    "day": ("日", "天"),
    "week": ("周",),
    "quarter": ("季度",),
    "year": ("年",),
    "supplier": ("供应商",),
    "material": ("物料",),
    "product": ("产品",),
    "customer": ("客户",),
    "warehouse": ("仓库", "仓"),
    "region": ("区域", "大区"),
    "org": ("组织", "部门"),
    "category": ("品类", "类别"),
    "channel": ("渠道",),
}


def _grainCoversDimension(grain: str, key: str) -> bool:
    """grain 文本是否覆盖维度键（键原文或别名表中的别名任一出现即算覆盖）。"""
    if key in grain:
        return True
    return any(alias in grain for alias in _DIM_ALIASES.get(key, ()))


def grainCoverageGaps(grain: str | None, dimensionDefaults: object) -> list[str]:
    """grain 文本未覆盖的维度键列表（B2 编译期对账与 B5 写侧弱校验共用同一判定）。

    dimensionDefaults 非 dict 或为空、grain 空白 → 无缺口（无可对账内容）。
    """
    if not isinstance(dimensionDefaults, dict) or not dimensionDefaults:
        return []
    if not grain or not grain.strip():
        return []
    return [key for key in dimensionDefaults if not _grainCoversDimension(grain, key)]


async def _warnGrainMismatch(
    session: AsyncSession,
    metricId: int | None,
    grain: str | None,
    kpiCode: str,
) -> None:
    """B5：grain 与挂靠 metric 的 dimension_defaults 粒度弱校验（只警告不阻断）。

    grain 是自由文本（如「月+供应商」），dimension_defaults 是维度键 dict ——
    无法做等值判定，只做启发式别名包含检查（见 grainCoverageGaps / _DIM_ALIASES）：
    缺失 → logger.warning（治理声明与技术实现可能漂移，供人工复核；表单级提示
    需要响应契约变更，延后到 B3 批次一并做）。
    """
    if metricId is None or not grain or not grain.strip():
        return
    from app.domain.models import OntologyMetric

    metric = await session.get(OntologyMetric, metricId)
    if metric is None:
        return
    missing = grainCoverageGaps(grain, metric.dimension_defaults)
    if missing:
        logger.warning(
            "KPI %s grain=%r 未覆盖挂靠 metric(id=%d) dimension_defaults 的键 %s"
            "——粒度声明与技术默认维度可能不一致，请人工复核",
            kpiCode, grain, metricId, missing,
        )


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