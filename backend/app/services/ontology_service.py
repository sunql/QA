"""本体服务（Ontology Service）。

协调 PostgreSQL（持久化）与 Neo4j（图谱）双写，保持关系一致性。
Milvus 仅在调用方提供 embedding 时同步写入向量。

主要职责：
- Class / Property / Metric 的 CRUD（PG + Neo4j）
- 关系维护（HAS_PROPERTY / REFERENCES / DERIVED_FROM）
- 语义搜索（Milvus，向量由调用方提供）

Phase 4.5 扩展：updateClass / deleteClass 走 AclService.assertCanModify
（基于 ontology_class.object_owner 字段，Phase 3.4 已加）。CREATE 不走 ACL。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from neo4j.exceptions import AuthError, ServiceUnavailable
from pymilvus.exceptions import MilvusException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import CurrentUser
from app.domain.exceptions import (
    MilvusError,
    NotFoundError,
    OntologyError,
    ValidationError,
)
from app.domain.models import OntologyClass, OntologyJoin, OntologyMetric, OntologyProperty
from app.domain.schemas import (
    OntologyClassCreate,
    OntologyClassUpdate,
    OntologyJoinCreate,
    OntologyJoinUpdate,
    OntologyMetricCreate,
    OntologyMetricUpdate,
    OntologyPropertyCreate,
    OntologyPropertyUpdate,
    OntologySearchResult,
)
from app.infrastructure import milvus_client as milvus
from app.infrastructure import neo4j_client as neo4j
from app.services.acl_service import AclService
from app.services.audit_service import AuditService
from app.services.embedding_service import EmbeddingService
from app.services.messages_zh import (
    MSG_CLASS_ALREADY_EXPIRED,
    MSG_CLASS_INHERIT_CYCLE,
    MSG_CLASS_INHERIT_SELF,
    MSG_CLASS_NAME_EXISTS,
    MSG_INHERIT_CHECK_UNAVAILABLE,
    MSG_ONTOLOGY_CLASS_EXPIRED,
    MSG_ONTOLOGY_CLASS_NOT_FOUND,
    MSG_ONTOLOGY_JOIN_COLUMN_COUNT_MISMATCH,
    MSG_ONTOLOGY_JOIN_DUP,
    MSG_ONTOLOGY_JOIN_NOT_FOUND,
    MSG_ONTOLOGY_METRIC_NOT_FOUND,
    MSG_ONTOLOGY_PROPERTY_NOT_FOUND,
    MSG_PARENT_CLASS_NOT_FOUND,
    MSG_VECTOR_SEARCH_FAILED,
    MSG_VECTOR_SYNC_FAILED,
)


def _utcnow() -> datetime:
    """时区感知的当前 UTC 时间。与 models._utcnow 同实现（避免循环导入）。"""
    from datetime import UTC

    return datetime.now(UTC)

logger = logging.getLogger(__name__)

_audit = AuditService()


def _entityToDict(entity: Any) -> dict[str, Any]:
    """Convert a SQLAlchemy model instance to a JSON-serializable dict for audit_log.

    datetime → ISO string; Decimal → float; everything else passed through as-is.
    This avoids ``Object of type X is not JSON serializable`` when writing to the
    JSONB ``before_json`` / ``after_json`` columns.
    """
    out: dict[str, Any] = {}
    for col in entity.__table__.columns.keys():
        v = getattr(entity, col)
        if isinstance(v, datetime):
            out[col] = v.isoformat()
        elif isinstance(v, Decimal):
            out[col] = float(v)
        else:
            out[col] = v
    return out


def _logNeo4jFailure(operation: str, entityId: int, exc: Exception) -> None:
    """按失败类型分级记录 Neo4j 同步失败，避免认证/不可达等安全相关错误被掩盖。"""
    if isinstance(exc, AuthError):
        logger.critical("Neo4j 认证失败 %s id=%d: %s（请检查凭据）", operation, entityId, exc)
    elif isinstance(exc, ServiceUnavailable):
        logger.error("Neo4j 不可达 %s id=%d: %s（图谱同步已跳过）", operation, entityId, exc)
    else:
        logger.warning("Neo4j %s 失败 id=%d: %s", operation, entityId, exc)


def makeJoinKey(
    sourceClassId: int,
    sourceColumns: list[str],
    targetClassId: int,
    targetColumns: list[str],
) -> str:
    """生成 join 边的幂等去重键（列按配对顺序拼接，不排序）。

    用普通文本列做唯一键，跨 PG/SQLite 都能比较（种子单测用 SQLite，
    JSONB 等值比较有方言差异）。种子与 service 共用，保证幂等口径一致。
    """
    src = ",".join(sourceColumns)
    tgt = ",".join(targetColumns)
    return f"{sourceClassId}|{src}->{targetClassId}|{tgt}"


# =============================================================================
# Class
# =============================================================================


class OntologyService:
    """本体服务：Class / Property / Metric CRUD。"""

    def __init__(
        self,
        *,
        embeddingService: EmbeddingService | None = None,
        acl: AclService | None = None,
    ) -> None:
        # EmbeddingService 延迟到首次语义检索时构造（未注入则按需创建）
        self._embedding: EmbeddingService | None = embeddingService
        # AclService 默认实例：service 内部 new；测试可注入 mock
        self._acl = acl or AclService()

    def _ensureEmbedding(self) -> EmbeddingService:
        if self._embedding is None:
            self._embedding = EmbeddingService()
        return self._embedding

    async def createClass(
        self,
        session: AsyncSession,
        dto: OntologyClassCreate,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> OntologyClass:
        """创建本体类（起始 version=1, validFrom=now, validTo=None）。

        重名校验：class_name 一旦被占用（含软删除墓碑）即拒绝——DB 唯一约束
        (class_name, version) 使墓碑名称无法复用（复用会 IntegrityError 500），
        故此处统一拦截为干净的 ValidationError。

        Phase 4.5：object_owner 由 actor.departments[0] 派生，**不接受**
        client body 中的 object_owner（DTO 已移除），防止越权。
        """
        # 名称占用校验：任一行（含墓碑）同名即拒绝
        existing = await session.execute(
            select(OntologyClass).where(
                OntologyClass.class_name == dto.class_name,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValidationError(MSG_CLASS_NAME_EXISTS.format(name=dto.class_name))

        # 校验父类存在（FK 约束兜底，但显式校验给出更清晰的错误）
        if dto.parent_class_id is not None:
            parent = await session.get(OntologyClass, dto.parent_class_id)
            if parent is None:
                raise ValidationError(MSG_PARENT_CLASS_NOT_FOUND.format(id=dto.parent_class_id))

        derivedOwner = actor_departments.split(",")[0] if actor_departments else None
        entity = OntologyClass(
            class_name=dto.class_name,
            class_alias=dto.class_alias,
            description=dto.description,
            source_table=dto.source_table,
            object_type=dto.object_type.value if dto.object_type is not None else None,
            object_owner=derivedOwner,
            parent_class_id=dto.parent_class_id,
            created_by=dto.created_by,
            version=1,
            valid_from=_utcnow(),
            valid_to=None,
        )
        session.add(entity)
        await session.flush()
        await _audit.record(
            session,
            entity_type="ONTOLOGY_CLASS",
            entity_id=entity.id,
            action="CREATE",
            actor=actor,
            actor_departments=actor_departments,
            after=_entityToDict(entity),
        )
        await session.commit()
        await session.refresh(entity)

        # Neo4j 节点 + 继承边（upsert 幂等：重复创建/重跑不冲突）
        try:
            neo4j.upsertClassNode(
                id=entity.id,
                name=entity.class_name,
                alias=entity.class_alias,
                description=entity.description,
                sourceTable=entity.source_table,
            )
            if entity.parent_class_id:
                neo4j.reconcileClassSubclassOf(entity.id, entity.parent_class_id)
        except Exception as exc:  # noqa: BLE001
            _logNeo4jFailure("节点创建", entity.id, exc)

        logger.info("创建本体类 id=%d name=%s version=1", entity.id, entity.class_name)
        return entity

    async def getClass(self, session: AsyncSession, id: int) -> OntologyClass:
        row = await session.get(OntologyClass, id, options=[selectinload(OntologyClass.properties)])
        if row is None:
            raise NotFoundError(MSG_ONTOLOGY_CLASS_NOT_FOUND.format(id=id))
        return row

    async def listClasses(
        self, session: AsyncSession, *, includeExpired: bool = False
    ) -> list[OntologyClass]:
        """列出本体类。

        includeExpired=False（默认）：仅返回未软删除的行（valid_to IS NULL），
        墓碑行默认不返回。版本管理移除后每类仅一行，includeExpired 仅作兼容保留。
        """
        stmt = select(OntologyClass).options(selectinload(OntologyClass.properties))
        if not includeExpired:
            stmt = stmt.where(OntologyClass.valid_to.is_(None))
        stmt = stmt.order_by(OntologyClass.class_name, OntologyClass.version.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def listVersions(
        self, session: AsyncSession, className: str
    ) -> list[OntologyClass]:
        """按 class_name 列出全部行（含软删除墓碑；版本管理移除后兼容保留）。"""
        result = await session.execute(
            select(OntologyClass)
            .options(selectinload(OntologyClass.properties))
            .where(OntologyClass.class_name == className)
            .order_by(OntologyClass.version.desc())
        )
        return list(result.scalars().all())

    async def updateClass(
        self,
        session: AsyncSession,
        id: int,
        dto: OntologyClassUpdate,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> OntologyClass:
        """更新本体类：原地 UPDATE，主键 id 稳定（版本管理已移除）。

        版本管理移除原因（运维报障根因）：原实现 INSERT 新行 + 克隆属性 +
        重映射引用，引入 id 漂移——Milvus 向量仍指向旧 id、新版本属性因克隆
        时序成为空壳、入边引用悬空，最终智能问答持续报"无法生成通过校验的
        查询计划"且属性视图为空。原地更新后 id 不变，属性与引用天然保持有效。

        流程（单事务）：
        1. ACL 校验（Phase 4.5：owner 不匹配 + 非 admin → 403）。
        2. 校验 id 存在且未被软删除（valid_to IS NULL）。
        3. 继承校验（同 Phase 1，仅在显式更新 parent_class_id 时执行）。
        4. 逐字段覆盖到原行并提交。
        5. 父类变化时，Neo4j 重建 SUBCLASS_OF 边（id 不变，按原 id 同步）。

        返回更新后的同一行（id 与调用方传入一致）。
        """
        existing = await self.getClass(session, id)
        # ACL 需要 CurrentUser：构造一个临时对象（仅用于 ACL 检查）
        _acl_user = CurrentUser(userId=actor, departments=list(actor_departments.split(",")) if actor_departments else [])
        self._acl.assertCanModify(
            _acl_user,
            entity_owner=existing.object_owner,
            entity_label="ONTOLOGY_CLASS",
            entity_code=existing.class_name,
        )
        if existing.valid_to is not None:
            raise ValidationError(
                MSG_ONTOLOGY_CLASS_EXPIRED.format(id=id, valid_to=existing.valid_to)
            )

        updates = dto.model_dump(exclude_unset=True)

        # 治理字段归一化：object_type 为枚举成员，DB 列存字符串值（"Master"/...）
        if updates.get("object_type") is not None:
            updates["object_type"] = updates["object_type"].value

        # 改名重名校验：原地更新后 class_name 无 DB 唯一约束，若改名为另一行
        # 的同名（含墓碑），会产生同名双行（或 IntegrityError 500）。与 createClass
        # 同口径：class_name 被任一行占用（含软删除墓碑）即拒绝。
        if "class_name" in updates:
            newName = updates["class_name"]
            if newName is not None:
                clash = await session.execute(
                    select(OntologyClass.id).where(
                        OntologyClass.class_name == newName,
                        OntologyClass.id != id,
                    )
                )
                if clash.scalar_one_or_none() is not None:
                    raise ValidationError(MSG_CLASS_NAME_EXISTS.format(name=newName))

        # 继承校验：仅在显式更新 parent_class_id 时执行
        if "parent_class_id" in updates:
            newParentId = updates["parent_class_id"]
            if newParentId is not None:
                if newParentId == id:
                    raise ValidationError(MSG_CLASS_INHERIT_SELF)
                parent = await session.get(OntologyClass, newParentId)
                if parent is None:
                    raise ValidationError(MSG_PARENT_CLASS_NOT_FOUND.format(id=newParentId))
                try:
                    if neo4j.detectInheritanceCycle(id, newParentId):
                        raise ValidationError(
                            MSG_CLASS_INHERIT_CYCLE.format(id=newParentId)
                        )
                except ValidationError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _logNeo4jFailure("继承环检测", id, exc)
                    raise ValidationError(MSG_INHERIT_CHECK_UNAVAILABLE) from exc

        # 原地覆盖字段：id 不变，属性与入边引用保持有效，无需克隆/重映射
        before = _entityToDict(existing)
        for key, value in updates.items():
            setattr(existing, key, value)
        await session.flush()
        await _audit.record(
            session,
            entity_type="ONTOLOGY_CLASS",
            entity_id=existing.id,
            action="UPDATE",
            actor=actor,
            actor_departments=actor_departments,
            before=before,
            after=_entityToDict(existing),
        )
        await session.commit()
        await session.refresh(existing)

        # Neo4j 节点属性 + 继承边同步（id 不变，按原 id 同步，best-effort）
        try:
            neo4j.upsertClassNode(
                id=existing.id,
                name=existing.class_name,
                alias=existing.class_alias,
                description=existing.description,
                sourceTable=existing.source_table,
            )
            if "parent_class_id" in updates:
                neo4j.reconcileClassSubclassOf(existing.id, existing.parent_class_id)
        except Exception as exc:  # noqa: BLE001
            _logNeo4jFailure("节点更新", existing.id, exc)

        logger.info("更新本体类 id=%d name=%s", existing.id, existing.class_name)
        return existing

    async def deleteClass(
        self,
        session: AsyncSession,
        id: int,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> None:
        """软删除本体类：valid_to = now()（墓碑标记），listClasses 默认不再返回。

        版本管理移除后 valid_to 退化为软删除标记。同步清理 Neo4j 节点与
        Milvus 向量（best-effort，避免图谱/向量悬空引用）。

        Phase 4.5 扩展：先 ACL 检查（object_owner 不匹配 + 非 admin → 403）。
        """
        entity = await self.getClass(session, id)
        _acl_user = CurrentUser(userId=actor, departments=list(actor_departments.split(",")) if actor_departments else [])
        self._acl.assertCanModify(
            _acl_user,
            entity_owner=entity.object_owner,
            entity_label="ONTOLOGY_CLASS",
            entity_code=entity.class_name,
        )
        if entity.valid_to is not None:
            raise ValidationError(MSG_CLASS_ALREADY_EXPIRED.format(id=id))
        before = _entityToDict(entity)
        entity.valid_to = _utcnow()
        await session.flush()
        await _audit.record(
            session,
            entity_type="ONTOLOGY_CLASS",
            entity_id=entity.id,
            action="DELETE",
            actor=actor,
            actor_departments=actor_departments,
            before=before,
        )
        await session.commit()
        try:
            neo4j.deleteNode("Class", id)
        except Exception as exc:  # noqa: BLE001
            _logNeo4jFailure("节点删除", id, exc)
        try:
            milvus.deleteByOntologyId(id, "class")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Milvus 记录删除失败 id=%d: %s", id, exc)
        logger.info("软删除本体类 id=%d（valid_to=%s）", id, entity.valid_to)

    # =============================================================================
    # Property
    # =============================================================================

    async def createProperty(
        self,
        session: AsyncSession,
        dto: OntologyPropertyCreate,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> OntologyProperty:
        """创建属性：写入 PG + Neo4j 节点 + 建立与 Class 的 HAS_PROPERTY 关系。"""
        # 验证 class 存在并拿到实体，供内存关系维护
        cls = await self.getClass(session, dto.class_id)

        entity = OntologyProperty(
            class_id=dto.class_id,
            property_name=dto.property_name,
            property_alias=dto.property_alias,
            business_aliases=dto.business_aliases,
            description=dto.description,
            data_type=dto.data_type,
            is_primary_key=dto.is_primary_key,
            is_foreign_key=dto.is_foreign_key,
            ref_class_id=dto.ref_class_id,
            source_column=dto.source_column,
        )
        # 维持会话内关系一致性：不 append 则本会话后续读 cls.properties 拿到陈旧空集合
        # （只写 class_id FK 不会触发 back_populates）。长事务/批量导入会因此漏读新属性。
        cls.properties.append(entity)
        session.add(entity)
        await session.flush()
        await _audit.record(
            session,
            entity_type="ONTOLOGY_PROPERTY",
            entity_id=entity.id,
            action="CREATE",
            actor=actor,
            actor_departments=actor_departments,
            after=_entityToDict(entity),
        )
        await session.commit()
        await session.refresh(entity)

        try:
            neo4j.upsertPropertyNode(
                id=entity.id,
                name=entity.property_name,
                alias=entity.property_alias,
                dataType=entity.data_type,
                sourceColumn=entity.source_column,
                isPrimaryKey=entity.is_primary_key,
                isForeignKey=entity.is_foreign_key,
            )
            neo4j.linkClassHasProperty(dto.class_id, entity.id)
            if entity.ref_class_id:
                neo4j.linkPropertyReferences(entity.id, entity.ref_class_id)
        except Exception as exc:  # noqa: BLE001
            _logNeo4jFailure("节点/关系创建", entity.id, exc)

        logger.info("创建本体属性 id=%d name=%s", entity.id, entity.property_name)
        return entity

    async def getProperty(self, session: AsyncSession, id: int) -> OntologyProperty:
        row = await session.get(OntologyProperty, id)
        if row is None:
            raise NotFoundError(MSG_ONTOLOGY_PROPERTY_NOT_FOUND.format(id=id))
        return row

    async def listPropertiesByClass(
        self, session: AsyncSession, classId: int
    ) -> list[OntologyProperty]:
        result = await session.execute(
            select(OntologyProperty)
            .where(OntologyProperty.class_id == classId)
            .order_by(OntologyProperty.property_name)
        )
        return list(result.scalars().all())

    async def updateProperty(
        self,
        session: AsyncSession,
        id: int,
        dto: OntologyPropertyUpdate,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> OntologyProperty:
        entity = await self.getProperty(session, id)
        before = _entityToDict(entity)
        updates = dto.model_dump(exclude_unset=True)
        for key, value in updates.items():
            setattr(entity, key, value)
        await session.flush()
        await _audit.record(
            session,
            entity_type="ONTOLOGY_PROPERTY",
            entity_id=entity.id,
            action="UPDATE",
            actor=actor,
            actor_departments=actor_departments,
            before=before,
            after=_entityToDict(entity),
        )
        await session.commit()
        await session.refresh(entity)

        # Neo4j 节点属性 + 关系同步（best-effort）
        try:
            neo4j.upsertPropertyNode(
                id=entity.id,
                name=entity.property_name,
                alias=entity.property_alias,
                dataType=entity.data_type,
                sourceColumn=entity.source_column,
                isPrimaryKey=entity.is_primary_key,
                isForeignKey=entity.is_foreign_key,
            )
            # 幂等 MERGE，自愈 create 阶段若中断而缺失的 HAS_PROPERTY 边
            neo4j.linkClassHasProperty(entity.class_id, entity.id)
            # 外键/引用类变化时重建 REFERENCES；目标以 is_foreign_key 为准，
            # 避免 is_foreign_key=False 时仍残留 REFERENCES 边
            if "is_foreign_key" in updates or "ref_class_id" in updates:
                target = entity.ref_class_id if entity.is_foreign_key else None
                neo4j.reconcilePropertyReferences(entity.id, target)
        except Exception as exc:  # noqa: BLE001
            _logNeo4jFailure("更新", id, exc)

        logger.info("更新本体属性 id=%d", id)
        return entity

    async def deleteProperty(
        self,
        session: AsyncSession,
        id: int,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> None:
        entity = await self.getProperty(session, id)
        before = _entityToDict(entity)
        await session.flush()
        await _audit.record(
            session,
            entity_type="ONTOLOGY_PROPERTY",
            entity_id=entity.id,
            action="DELETE",
            actor=actor,
            actor_departments=actor_departments,
            before=before,
        )
        await session.delete(entity)
        await session.commit()
        try:
            neo4j.deleteNode("Property", id)
        except Exception as exc:  # noqa: BLE001
            _logNeo4jFailure("节点删除", id, exc)
        try:
            milvus.deleteByOntologyId(id, "property")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Milvus 记录删除失败 id=%d: %s", id, exc)
        logger.info("删除本体属性 id=%d", id)

    # =============================================================================
    # Metric
    # =============================================================================

    async def createMetric(
        self,
        session: AsyncSession,
        dto: OntologyMetricCreate,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> OntologyMetric:
        """创建指标：写入 PG + Neo4j 节点 + DERIVED_FROM 关系。"""
        if dto.target_class_id:
            await self.getClass(session, dto.target_class_id)

        entity = OntologyMetric(
            metric_name=dto.metric_name,
            metric_alias=dto.metric_alias,
            formula=dto.formula,
            agg_function=dto.agg_function,
            target_class_id=dto.target_class_id,
            dimension_defaults=dto.dimension_defaults,
            created_by=dto.created_by,
        )
        session.add(entity)
        await session.flush()
        await _audit.record(
            session,
            entity_type="ONTOLOGY_METRIC",
            entity_id=entity.id,
            action="CREATE",
            actor=actor,
            actor_departments=actor_departments,
            after=_entityToDict(entity),
        )
        await session.commit()
        await session.refresh(entity)

        try:
            neo4j.upsertMetricNode(
                id=entity.id,
                name=entity.metric_name,
                alias=entity.metric_alias,
                formula=entity.formula,
                aggFunction=entity.agg_function,
            )
            if entity.target_class_id:
                neo4j.linkMetricDerivedFrom(entity.id, entity.target_class_id)
        except Exception as exc:  # noqa: BLE001
            _logNeo4jFailure("节点/关系创建", entity.id, exc)

        logger.info("创建本体指标 id=%d name=%s", entity.id, entity.metric_name)
        return entity

    async def getMetric(self, session: AsyncSession, id: int) -> OntologyMetric:
        row = await session.get(OntologyMetric, id)
        if row is None:
            raise NotFoundError(MSG_ONTOLOGY_METRIC_NOT_FOUND.format(id=id))
        return row

    async def listMetrics(self, session: AsyncSession) -> list[OntologyMetric]:
        result = await session.execute(
            select(OntologyMetric)
            .options(selectinload(OntologyMetric.target_class))
            .order_by(OntologyMetric.metric_name)
        )
        return list(result.scalars().all())

    async def updateMetric(
        self,
        session: AsyncSession,
        id: int,
        dto: OntologyMetricUpdate,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> OntologyMetric:
        entity = await self.getMetric(session, id)
        before = _entityToDict(entity)
        updates = dto.model_dump(exclude_unset=True)
        for key, value in updates.items():
            setattr(entity, key, value)
        await session.flush()
        await _audit.record(
            session,
            entity_type="ONTOLOGY_METRIC",
            entity_id=entity.id,
            action="UPDATE",
            actor=actor,
            actor_departments=actor_departments,
            before=before,
            after=_entityToDict(entity),
        )
        await session.commit()
        await session.refresh(entity)

        # Neo4j 节点属性 + 关系同步（best-effort）
        try:
            neo4j.upsertMetricNode(
                id=entity.id,
                name=entity.metric_name,
                alias=entity.metric_alias,
                formula=entity.formula,
                aggFunction=entity.agg_function,
            )
            # 目标类变化时重建 DERIVED_FROM 关系
            if "target_class_id" in updates:
                neo4j.reconcileMetricDerivedFrom(entity.id, entity.target_class_id)
        except Exception as exc:  # noqa: BLE001
            _logNeo4jFailure("更新", id, exc)

        logger.info("更新本体指标 id=%d", id)
        return entity

    async def deleteMetric(
        self,
        session: AsyncSession,
        id: int,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> None:
        entity = await self.getMetric(session, id)
        before = _entityToDict(entity)
        await session.flush()
        await _audit.record(
            session,
            entity_type="ONTOLOGY_METRIC",
            entity_id=entity.id,
            action="DELETE",
            actor=actor,
            actor_departments=actor_departments,
            before=before,
        )
        await session.delete(entity)
        await session.commit()
        try:
            neo4j.deleteNode("Metric", id)
        except Exception as exc:  # noqa: BLE001
            _logNeo4jFailure("节点删除", id, exc)
        try:
            milvus.deleteByOntologyId(id, "metric")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Milvus 记录删除失败 id=%d: %s", id, exc)
        logger.info("删除本体指标 id=%d", id)

    # =============================================================================
    # Join（关联关系目录）
    # =============================================================================

    async def listJoins(self, session: AsyncSession) -> list[OntologyJoin]:
        """列出全部 join 边（按 id 升序，供 NL2SQL 渲染 + 前端目录页）。"""
        result = await session.execute(
            select(OntologyJoin).order_by(OntologyJoin.id)
        )
        return list(result.scalars().all())

    async def createJoin(
        self,
        session: AsyncSession,
        dto: OntologyJoinCreate,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> OntologyJoin:
        """创建 join 边：校验两端类存在、列数一致、去重后写入 PG。

        join 目录仅 NL2SQL 消费，不写 Neo4j/Milvus（join 边不是本体节点/关系）。
        """
        # 两端类存在性：复用 getClass，缺失抛 NotFoundError（404）
        await self.getClass(session, dto.source_class_id)
        await self.getClass(session, dto.target_class_id)
        if len(dto.source_columns) != len(dto.target_columns):
            raise ValidationError(MSG_ONTOLOGY_JOIN_COLUMN_COUNT_MISMATCH)

        joinKey = makeJoinKey(
            dto.source_class_id, dto.source_columns,
            dto.target_class_id, dto.target_columns,
        )
        existing = await session.execute(
            select(OntologyJoin).where(OntologyJoin.join_key == joinKey)
        )
        if existing.scalar_one_or_none() is not None:
            raise ValidationError(MSG_ONTOLOGY_JOIN_DUP)

        entity = OntologyJoin(
            source_class_id=dto.source_class_id,
            source_columns=dto.source_columns,
            target_class_id=dto.target_class_id,
            target_columns=dto.target_columns,
            join_type=dto.join_type,
            relation_type=dto.relation_type,
            description=dto.description,
            join_key=joinKey,
        )
        session.add(entity)
        await session.flush()
        await _audit.record(
            session,
            entity_type="ONTOLOGY_JOIN",
            entity_id=entity.id,
            action="CREATE",
            actor=actor,
            actor_departments=actor_departments,
            after=_entityToDict(entity),
        )
        await session.commit()
        await session.refresh(entity)
        logger.info(
            "创建关联关系 id=%d %d->%d type=%s",
            entity.id, entity.source_class_id, entity.target_class_id, entity.join_type,
        )
        return entity

    async def updateJoin(
        self,
        session: AsyncSession,
        id: int,
        dto: OntologyJoinUpdate,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> OntologyJoin:
        """更新 join 边：仅允许更新 join_type / relation_type / description（join_key 不可变）。"""
        entity = await session.get(OntologyJoin, id)
        if entity is None:
            raise NotFoundError(MSG_ONTOLOGY_JOIN_NOT_FOUND.format(id=id))
        before = _entityToDict(entity)
        updates = dto.model_dump(exclude_unset=True)
        for key, value in updates.items():
            setattr(entity, key, value)
        await session.flush()
        await _audit.record(
            session,
            entity_type="ONTOLOGY_JOIN",
            entity_id=entity.id,
            action="UPDATE",
            actor=actor,
            actor_departments=actor_departments,
            before=before,
            after=_entityToDict(entity),
        )
        await session.commit()
        await session.refresh(entity)
        logger.info("更新关联关系 id=%d", id)
        return entity

    async def deleteJoin(
        self,
        session: AsyncSession,
        id: int,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> None:
        """删除 join 边。"""
        entity = await session.get(OntologyJoin, id)
        if entity is None:
            raise NotFoundError(MSG_ONTOLOGY_JOIN_NOT_FOUND.format(id=id))
        before = _entityToDict(entity)
        await session.flush()
        await _audit.record(
            session,
            entity_type="ONTOLOGY_JOIN",
            entity_id=entity.id,
            action="DELETE",
            actor=actor,
            actor_departments=actor_departments,
            before=before,
        )
        await session.delete(entity)
        await session.commit()
        logger.info("删除关联关系 id=%d", id)

    # =============================================================================
    # Semantic Search (Milvus)
    # =============================================================================

    async def searchByKeyword(
        self,
        query: str,
        *,
        topK: int = 5,
        typeFilter: str | None = None,
    ) -> list[OntologySearchResult]:
        """端到端语义检索：文本 -> embedding -> Milvus 近邻搜索。

        相比 searchByEmbedding（需调用方预生成向量），此方法封装了 embedding 生成，
        供 API 层直接以关键词调用。embedding 生成失败抛 LlmClientError，
        Milvus 检索失败抛 MilvusError（均为领域异常，由全局处理器转 4xx）。

        Args:
            query: 自然语言关键词。
            topK: 返回条数。
            typeFilter: 可选，限定类型（class/property/metric）。
        """
        embedding = await self._ensureEmbedding().generateEmbedding(query)
        try:
            hits = await asyncio.to_thread(
                milvus.searchByEmbedding, embedding, topK, typeFilter
            )
        except (MilvusException, OSError) as exc:
            raise MilvusError(MSG_VECTOR_SEARCH_FAILED, detail=str(exc)) from exc

        # L2 距离 -> [0,1] 相似度（距离越小越相似；与 embedding_service 同口径）
        return [
            OntologySearchResult(
                id=h["ontology_id"],
                type=h["type"],
                name=h["name"],
                alias=h.get("alias") or None,
                description=h.get("description") or None,
                score=round(1.0 / (1.0 + max(float(h["distance"]), 0.0)), 4),
            )
            for h in hits
        ]

    def searchByEmbedding(
        self,
        queryEmbedding: list[float],
        topK: int = 5,
        typeFilter: str | None = None,
    ) -> list[dict[str, Any]]:
        """向量相似度搜索（embedding 由调用方通过 LLM 生成）。"""
        return milvus.searchByEmbedding(queryEmbedding, topK, typeFilter)

    def syncEmbedding(
        self,
        ontologyId: int,
        type: str,
        name: str,
        alias: str | None,
        description: str | None,
        embedding: list[float],
    ) -> None:
        """同步单条 embedding 到 Milvus（新建或覆盖）。"""
        try:
            milvus.deleteByOntologyId(ontologyId, type)
            milvus.insertEmbeddings([{
                "ontology_id": ontologyId,
                "type": type,
                "name": name,
                "alias": alias,
                "description": description,
                "embedding": embedding,
            }])
            logger.info("Milvus embedding 已同步 id=%d type=%s", ontologyId, type)
        except Exception as exc:  # noqa: BLE001
            logger.error("Milvus embedding 同步失败 id=%d: %s", ontologyId, exc)
            raise OntologyError(MSG_VECTOR_SYNC_FAILED.format(exc=exc)) from exc
