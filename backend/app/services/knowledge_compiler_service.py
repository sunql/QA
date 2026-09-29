"""M8 Knowledge Compiler 统一门面（v3.1 任务 A5）。

把散在 ontology_service / embedding+wiki_compile / metric_promotion+kpi_catalog
的编译逻辑收编为统一入口（v3.1 蓝图 §4.14：3 种核心产物 + 运维约束）：

- ``compileObject(unifiedId)``：单对象增量编译，分发到三个编译器角色——
  graph（Semantic Graph / Neo4j）、vector（Vector Index / Milvus）、
  sql_metadata（SQL Metadata / PG KpiCatalog 挂靠）。门面只做分发与聚合，
  不复制编译逻辑；每角色独立 success/failed/skipped（multistep 失败隔离同款）。
- ``reconcile()``：三库对账巡检（只读）——PG / Neo4j / Milvus 三库 count 比对，
  不一致即 warning，结果落 audit_history。
- 调度纯函数（``nextReconcileRun`` / ``isReconcileDue``）：借
  agent_scheduler_service 模式（croniter）；每日执行由独立 worker
  （``app.workers.knowledge_reconcile_worker``）驱动，测试环境不真挂 cron。

不新增任何 LLM 调用：收编入口内部的 embedding 生成保持原样。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from croniter import croniter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import (
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyProperty,
)
from app.domain.schemas import IdMappingCreate
from app.infrastructure import neo4j_client as neo4j
from app.infrastructure.milvus_client import (
    _CLASS_COLLECTION_NAME,
    _METRIC_COLLECTION_NAME,
    _PROPERTY_COLLECTION_NAME,
    _connAlias,
    _connect,
)
from app.services.audit_service import AuditService
from app.services.embedding_service import EmbeddingService
from app.services.id_mapping_service import IdMappingService
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.messages_zh import (
    MSG_ONTOLOGY_CLASS_NOT_FOUND,
    MSG_ONTOLOGY_METRIC_NOT_FOUND,
    MSG_ONTOLOGY_PROPERTY_NOT_FOUND,
)
from app.services.ontology_service import (
    OntologyService,
    _metricEmbeddingText,
    _propertyEmbeddingText,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_ROLE_GRAPH = "graph"
_ROLE_VECTOR = "vector"
_ROLE_SQL_METADATA = "sql_metadata"
_STATUS_SUCCESS = "success"
_STATUS_FAILED = "failed"
_STATUS_SKIPPED = "skipped"

# 每日对账 cron（03:00，避开业务高峰）；执行节奏由 worker 轮询驱动
_DAILY_RECONCILE_CRON = "0 3 * * *"
_RECONCILE_ACTOR = "system:knowledge-compiler"
_RECONCILE_ENTITY_TYPE = "knowledge_compile_reconcile"
_RECONCILE_ENTITY_ID = 0

# unified_id 格式 obj:{type}:{ontology_id}（type ∈ class/property/metric）
_UNIFIED_ID_PREFIX = "obj"
_TYPE_TO_BUSINESS_OBJECT = {"class": "CLASS", "property": "PROPERTY", "metric": "METRIC"}
_TYPE_TO_MODEL = {
    "class": OntologyClass,
    "property": OntologyProperty,
    "metric": OntologyMetric,
}
_TYPE_TO_PG_TABLE = {
    "class": "ontology_class",
    "property": "ontology_property",
    "metric": "ontology_metric",
}
_TYPE_TO_COLLECTION = {
    "class": _CLASS_COLLECTION_NAME,
    "property": _PROPERTY_COLLECTION_NAME,
    "metric": _METRIC_COLLECTION_NAME,
}
_NOT_FOUND_BY_TYPE = {
    "class": MSG_ONTOLOGY_CLASS_NOT_FOUND,
    "property": MSG_ONTOLOGY_PROPERTY_NOT_FOUND,
    "metric": MSG_ONTOLOGY_METRIC_NOT_FOUND,
}
_TYPE_TO_EMBEDDING_TEXT = {
    "property": _propertyEmbeddingText,
    "metric": _metricEmbeddingText,
}

_MSG_INVALID_UNIFIED_ID = (
    "非法 unified_id（期望 obj:{{type}}:{{id}}，type ∈ class/property/metric）：{unified_id}"
)
_MSG_RECONCILE_STORE_UNAVAILABLE = "对账：{store} 不可达，本轮跳过其比对：{error}"
_MSG_RECONCILE_MISMATCH = "对账不一致 {object_type}: {counts}"
_MSG_RECONCILE_MISSING_COLLECTION = "Milvus 集合缺失: {collection}"

# 运行日志错误信息截断长度（与 agent_scheduler 同口径，避免堆栈细节外泄）
_MAX_ERROR_LEN = 500


# ---------------------------------------------------------------------------
# 结果结构（frozen，不可变）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompileRoleResult:
    """单编译角色结果：status ∈ success / failed / skipped。"""

    role: str
    status: str
    error: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class CompileObjectResult:
    """compileObject 聚合结果：每角色独立，单角色失败不拖垮其他角色。"""

    unified_id: str
    object_type: str
    ontology_id: int
    roles: tuple[CompileRoleResult, ...] = ()

    @property
    def isFullySuccessful(self) -> bool:
        """无 failed 角色即视为整体成功（skipped 不算失败）。"""
        return all(r.status != _STATUS_FAILED for r in self.roles)

    def toDict(self) -> dict[str, Any]:
        return {
            "unified_id": self.unified_id,
            "object_type": self.object_type,
            "ontology_id": self.ontology_id,
            "roles": [
                {"role": r.role, "status": r.status, "error": r.error, "detail": r.detail}
                for r in self.roles
            ],
        }


@dataclass(frozen=True)
class ReconcileSummary:
    """三库对账摘要。某库不可达时对应 counts 为 None（warning 已记录）。"""

    pg_counts: Mapping[str, int] | None
    neo4j_counts: Mapping[str, int] | None
    milvus_counts: Mapping[str, int] | None
    warnings: tuple[str, ...] = ()

    @property
    def isConsistent(self) -> bool:
        """无任何 warning（含库不可达 / count 不一致）即一致。"""
        return not self.warnings

    def toDict(self) -> dict[str, Any]:
        return {
            "pg_counts": dict(self.pg_counts) if self.pg_counts is not None else None,
            "neo4j_counts": dict(self.neo4j_counts) if self.neo4j_counts is not None else None,
            "milvus_counts": dict(self.milvus_counts) if self.milvus_counts is not None else None,
            "warnings": list(self.warnings),
            "is_consistent": self.isConsistent,
        }


def parseUnifiedId(unifiedId: str) -> tuple[str, int]:
    """``obj:{type}:{ontology_id}`` → (type, ontology_id)；非法格式 fail-fast。"""
    parts = unifiedId.split(":") if isinstance(unifiedId, str) else []
    if (
        len(parts) != 3
        or parts[0] != _UNIFIED_ID_PREFIX
        or parts[1] not in _TYPE_TO_BUSINESS_OBJECT
    ):
        raise ValidationError(
            _MSG_INVALID_UNIFIED_ID.format(unified_id=repr(unifiedId))
        )
    try:
        ontologyId = int(parts[2])
    except ValueError as exc:
        raise ValidationError(
            _MSG_INVALID_UNIFIED_ID.format(unified_id=repr(unifiedId))
        ) from exc
    if ontologyId <= 0:
        raise ValidationError(_MSG_INVALID_UNIFIED_ID.format(unified_id=repr(unifiedId)))
    return parts[1], ontologyId


# ---------------------------------------------------------------------------
# 门面
# ---------------------------------------------------------------------------


class KnowledgeCompilerService:
    """知识编译统一门面：只做分发与聚合，不复制编译逻辑（DRY）。"""

    def __init__(
        self,
        *,
        ontology: OntologyService | None = None,
        idMapping: IdMappingService | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self._ontology = ontology or OntologyService()
        self._idMapping = idMapping or IdMappingService()
        self._audit = audit or AuditService()

    # ------------------------------------------------------------------
    # compileObject：单对象增量编译（三角色分发 + 失败隔离）
    # ------------------------------------------------------------------

    async def compileObject(
        self, session: AsyncSession, unifiedId: str
    ) -> CompileObjectResult:
        """按 unifiedId 增量编译一个本体对象，返回三角色聚合结果。"""
        objectType, ontologyId = parseUnifiedId(unifiedId)
        entity = await self._loadEntity(session, objectType, ontologyId)
        roles = (
            await self._runRole(_ROLE_GRAPH, self._compileGraph, session, objectType, entity),
            await self._runRole(_ROLE_VECTOR, self._compileVector, session, objectType, entity),
            await self._runRole(
                _ROLE_SQL_METADATA, self._compileSqlMetadata, session, objectType, entity
            ),
        )
        return CompileObjectResult(
            unified_id=unifiedId,
            object_type=objectType,
            ontology_id=ontologyId,
            roles=roles,
        )

    async def _loadEntity(
        self, session: AsyncSession, objectType: str, ontologyId: int
    ):
        """PG 真源加载本体对象；不存在抛 NotFoundError（fail-fast at boundary）。"""
        entity = await session.get(_TYPE_TO_MODEL[objectType], ontologyId)
        if entity is None:
            raise NotFoundError(
                _NOT_FOUND_BY_TYPE[objectType].format(id=ontologyId)
            )
        return entity

    async def _runRole(
        self,
        roleName: str,
        roleFn,
        session: AsyncSession,
        objectType: str,
        entity,
    ) -> CompileRoleResult:
        """单角色执行 + 全兜底：失败只记录，不拖垮其他角色，不留半成品。"""
        try:
            return await roleFn(session, objectType, entity)
        except Exception as exc:  # noqa: BLE001 - 兜底 except 必须执行（僵尸任务教训）
            logger.warning(
                "编译角色 %s 失败 type=%s id=%s: %s",
                roleName, objectType, entity.id, exc, exc_info=True,
            )
            errMsg = f"{type(exc).__name__}: {exc}"
            return CompileRoleResult(
                role=roleName,
                status=_STATUS_FAILED,
                error=errMsg[:_MAX_ERROR_LEN],
            )

    # ------------------------------------------------------------------
    # 角色 1：Semantic Graph 编译器（Neo4j，薄包装 ontology 图同步）
    # ------------------------------------------------------------------

    async def _compileGraph(
        self, session: AsyncSession, objectType: str, entity
    ) -> CompileRoleResult:
        """按类型 upsert 图节点 + 幂等重建边（与 syncMissingGraph 同款函数）。"""
        uid = await self._resolveUid(session, objectType, entity.id)
        if objectType == "class":
            neo4j.upsertClassNode(
                unified_id=uid,
                name=entity.class_name,
                alias=entity.class_alias,
                description=entity.description,
                sourceTable=entity.source_table,
            )
            await self._linkClassParent(session, uid, entity)
            detail = "class 图节点已 upsert"
        elif objectType == "property":
            neo4j.upsertPropertyNode(
                unified_id=uid,
                name=entity.property_name,
                alias=entity.property_alias,
                dataType=entity.data_type,
                sourceColumn=entity.source_column,
                isPrimaryKey=bool(entity.is_primary_key),
                isForeignKey=bool(entity.is_foreign_key),
            )
            await self._linkPropertyEdges(session, uid, entity)
            detail = "property 图节点已 upsert"
        else:
            neo4j.upsertMetricNode(
                unified_id=uid,
                name=entity.metric_name,
                alias=entity.metric_alias,
                formula=entity.formula,
                aggFunction=entity.agg_function,
            )
            await self._linkMetricTarget(session, uid, entity)
            detail = "metric 图节点已 upsert"
        return CompileRoleResult(role=_ROLE_GRAPH, status=_STATUS_SUCCESS, detail=detail)

    async def _resolveUid(
        self, session: AsyncSession, objectType: str, ontologyId: int
    ) -> str:
        """解析 unified_id；未注册时补注册（与 syncMissingGraph 同口径）。"""
        businessObject = _TYPE_TO_BUSINESS_OBJECT[objectType]
        mapping = await self._idMapping.resolveByExternal(
            session, businessObject, str(ontologyId)
        )
        if mapping is not None:
            return mapping.unified_id
        created = await self._idMapping.register(
            session,
            IdMappingCreate(
                business_object=businessObject,
                external_id=str(ontologyId),
                pg_table=_TYPE_TO_PG_TABLE[objectType],
                pg_id=str(ontologyId),
            ),
        )
        return created.unified_id

    async def _linkClassParent(
        self, session: AsyncSession, uid: str, entity: OntologyClass
    ) -> None:
        """Class SUBCLASS_OF 边（父类未注册映射时跳过，同 syncMissingGraph）。"""
        if entity.parent_class_id is None:
            return
        parent = await self._idMapping.resolveByExternal(
            session, "CLASS", str(entity.parent_class_id)
        )
        if parent is not None:
            neo4j.reconcileClassSubclassOf(uid, parent.unified_id)

    async def _linkPropertyEdges(
        self, session: AsyncSession, uid: str, entity: OntologyProperty
    ) -> None:
        """Property 的 HAS_PROPERTY / REFERENCES 边（映射缺失跳过）。"""
        owner = await self._idMapping.resolveByExternal(
            session, "CLASS", str(entity.class_id)
        )
        if owner is not None:
            neo4j.linkClassHasProperty(owner.unified_id, uid)
        if entity.ref_class_id is not None:
            ref = await self._idMapping.resolveByExternal(
                session, "CLASS", str(entity.ref_class_id)
            )
            if ref is not None:
                neo4j.linkPropertyReferences(uid, ref.unified_id)

    async def _linkMetricTarget(
        self, session: AsyncSession, uid: str, entity: OntologyMetric
    ) -> None:
        """Metric DERIVED_FROM 边（目标类映射缺失跳过）。"""
        if entity.target_class_id is None:
            return
        target = await self._idMapping.resolveByExternal(
            session, "CLASS", str(entity.target_class_id)
        )
        if target is not None:
            neo4j.reconcileMetricDerivedFrom(uid, target.unified_id)

    # ------------------------------------------------------------------
    # 角色 2：Vector Index 编译器（Milvus，薄包装 embedding 同步入口）
    # ------------------------------------------------------------------

    async def _compileVector(
        self, session: AsyncSession, objectType: str, entity
    ) -> CompileRoleResult:
        """类走现有干净入口 syncClassEmbedding；属性/指标走同口径薄包装。"""
        if objectType == "class":
            await self._ontology.syncClassEmbedding(session, entity.id)
            return CompileRoleResult(
                role=_ROLE_VECTOR, status=_STATUS_SUCCESS, detail="class 向量已同步"
            )
        embeddingText = _TYPE_TO_EMBEDDING_TEXT[objectType](entity)
        vec = await EmbeddingService().generateEmbedding(embeddingText)
        if objectType == "property":
            self._ontology.syncEmbedding(
                ontologyId=entity.id,
                type="property",
                name=entity.property_name,
                alias=entity.property_alias,
                description=entity.description,
                embedding=vec,
            )
        else:
            self._ontology.syncEmbedding(
                ontologyId=entity.id,
                type="metric",
                name=entity.metric_name,
                alias=entity.metric_alias,
                description=entity.agg_function,
                embedding=vec,
            )
        return CompileRoleResult(
            role=_ROLE_VECTOR, status=_STATUS_SUCCESS, detail=f"{objectType} 向量已同步"
        )

    # ------------------------------------------------------------------
    # 角色 3：SQL Metadata 编译器（PG KpiCatalog 挂靠刷新）
    # ------------------------------------------------------------------

    async def _compileSqlMetadata(
        self, session: AsyncSession, objectType: str, entity
    ) -> CompileRoleResult:
        """metric → 刷新其挂靠 KpiCatalog 的匹配缓存；class/property 无此产物。"""
        if objectType != "metric":
            return CompileRoleResult(
                role=_ROLE_SQL_METADATA,
                status=_STATUS_SKIPPED,
                detail=f"{objectType} 无 SQL Metadata 编译产物",
            )
        rows = (
            await session.execute(
                select(KpiCatalog).where(KpiCatalog.metric_id == entity.id)
            )
        ).scalars().all()
        if not rows:
            return CompileRoleResult(
                role=_ROLE_SQL_METADATA,
                status=_STATUS_SKIPPED,
                detail="metric 未挂靠 kpi_catalog 行",
            )
        codes = [row.kpi_code for row in rows]
        cache = get_kpi_match_cache()
        for code in codes:
            await cache.refreshOne(session, code)
        return CompileRoleResult(
            role=_ROLE_SQL_METADATA,
            status=_STATUS_SUCCESS,
            detail=f"已刷新 KPI 匹配缓存: {','.join(codes)}",
        )

    # ------------------------------------------------------------------
    # reconcile：三库对账巡检（只读）+ audit_history 落库
    # ------------------------------------------------------------------

    async def reconcile(self, session: AsyncSession) -> ReconcileSummary:
        """三库 count 比对（只读）：不一致即 warning，结果落 audit_history。"""
        warnings: list[str] = []
        pgCounts = await self._safePgCounts(session, warnings)
        neo4jCounts = self._safeStoreCounts(
            self._countNeo4j, "neo4j", warnings
        )
        milvusCounts = self._safeStoreCounts(
            self._countMilvus, "milvus", warnings
        )
        warnings.extend(self._compareCounts(pgCounts, neo4jCounts, milvusCounts))
        summary = ReconcileSummary(
            pg_counts=pgCounts,
            neo4j_counts=neo4jCounts,
            milvus_counts=milvusCounts,
            warnings=tuple(warnings),
        )
        await self._audit.record(
            session,
            entity_type=_RECONCILE_ENTITY_TYPE,
            entity_id=_RECONCILE_ENTITY_ID,
            action="CREATE",
            actor=_RECONCILE_ACTOR,
            after=summary.toDict(),
        )
        await session.commit()
        logger.info("知识三库对账完成: %s", summary.toDict())
        return summary

    async def _safePgCounts(
        self, session: AsyncSession, warnings: list[str]
    ) -> Mapping[str, int] | None:
        try:
            return await self._countPg(session)
        except Exception as exc:  # noqa: BLE001 - 巡检不因单库不可达而中断
            warnings.append(
                _MSG_RECONCILE_STORE_UNAVAILABLE.format(store="pg", error=exc)
            )
            return None

    def _safeStoreCounts(
        self, countFn, storeName: str, warnings: list[str]
    ) -> Mapping[str, int] | None:
        try:
            return countFn()
        except Exception as exc:  # noqa: BLE001 - 巡检不因单库不可达而中断
            warnings.append(
                _MSG_RECONCILE_STORE_UNAVAILABLE.format(store=storeName, error=exc)
            )
            return None

    @staticmethod
    def _compareCounts(*storeCounts: Mapping[str, int] | None) -> list[str]:
        """逐类型三方比对：可用库之间 count 不一致即 warning。"""
        mismatches: list[str] = []
        for objectType in _TYPE_TO_BUSINESS_OBJECT:
            values = {
                name: counts[objectType]
                for name, counts in zip(("pg", "neo4j", "milvus"), storeCounts, strict=True)
                if counts is not None
            }
            if len(set(values.values())) > 1:
                rendered = ", ".join(f"{name}={count}" for name, count in values.items())
                mismatches.append(
                    _MSG_RECONCILE_MISMATCH.format(object_type=objectType, counts=rendered)
                )
        return mismatches

    @staticmethod
    async def _countPg(session: AsyncSession) -> dict[str, int]:
        """PG 三类本体行数（真源；类与 listClasses 同口径排除软删墓碑）。"""
        counts: dict[str, int] = {}
        for objectType, model in _TYPE_TO_MODEL.items():
            stmt = select(func.count(model.id))
            if model is OntologyClass:
                stmt = stmt.where(OntologyClass.valid_to.is_(None))
            counts[objectType] = (await session.execute(stmt)).scalar_one()
        return counts

    @staticmethod
    def _countNeo4j() -> dict[str, int]:
        """Neo4j 本体节点数（Class/Property/Metric label）。"""
        return {
            "class": len(neo4j.getClassIds()),
            "property": len(neo4j.getPropertyIds()),
            "metric": len(neo4j.listNodesByLabel("Metric")),
        }

    @staticmethod
    def _countMilvus() -> dict[str, int]:
        """Milvus 3-collection 行数（只读 num_entities；缺集合计 0）。"""
        _connect()
        counts: dict[str, int] = {}
        for objectType, collectionName in _TYPE_TO_COLLECTION.items():
            counts[objectType] = _milvusCollectionCount(collectionName)
        return counts

    # ------------------------------------------------------------------
    # 每日调度纯函数（借 agent_scheduler_service 模式：croniter）
    # ------------------------------------------------------------------

    @staticmethod
    def nextReconcileRun(fromTime: datetime) -> datetime:
        """fromTime 之后的下一个每日对账触发时刻（严格大于，UTC aware）。"""
        return croniter(_DAILY_RECONCILE_CRON, fromTime).get_next(datetime)

    @staticmethod
    def isReconcileDue(lastRunAt: datetime | None, now: datetime) -> bool:
        """从未跑过立即到期；否则过了上一个触发点即到期。"""
        if lastRunAt is None:
            return True
        return KnowledgeCompilerService.nextReconcileRun(lastRunAt) <= now


def _milvusCollectionCount(collectionName: str) -> int:
    """单个 Milvus 集合行数；集合缺失视为 0（交由 mismatch warning 呈现）。"""
    from pymilvus import Collection, utility

    if not utility.has_collection(collectionName, using=_connAlias()):
        logger.warning(_MSG_RECONCILE_MISSING_COLLECTION.format(collection=collectionName))
        return 0
    return Collection(collectionName, using=_connAlias()).num_entities


__all__ = [
    "CompileObjectResult",
    "CompileRoleResult",
    "KnowledgeCompilerService",
    "ReconcileSummary",
    "parseUnifiedId",
]
