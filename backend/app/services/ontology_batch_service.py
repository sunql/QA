"""通用「批量关系引擎」服务（Phase 5.7 批量关系）。

批量建立三种关系，可任选其一或多个执行：
1. syncGraph     本体入图：把 live 的 ontology_class/property upsert 成 Neo4j
                 Class/Property 节点 + HAS_PROPERTY/REFERENCES 边（幂等，对账用）。
2. inferJoins    共享列推断物理关联 join：Pass A X3 命名约定（SAGE_X3_REFERENCE_MAP）
                 + Pass B 通用共享列（一方 is_primary_key 的 FK→PK 对），跳过样板列
                 黑名单；推断候选可落库为 ontology_join + (:Class)-[:JOIN]->(:Class)。
3. applyManifest 应用清单（joins + relations），对已存在关系可选 skip / overwrite。

写入口径：内存去重 + 显式 create/update + 逐行 audit + **单次 commit**。
不逐条调 OntologyService.createJoin/createRelation（各自 commit + 重复抛 422），
而在此预取已存在行、逐条判定、复用 makeJoinKey/_entityToDict/_logNeo4jFailure。

约束：Neo4j 失败一律 fail-open（_logNeo4jFailure），PG 为准；预览只读不落库。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ClassRelationType
from app.domain.models import (
    OntologyClass,
    OntologyJoin,
    OntologyProperty,
    OntologyRelation,
)
from app.domain.schemas import (
    BatchCounts,
    BatchRelationRequest,
    BatchRelationResult,
    BatchRowError,
    GraphSyncResult,
    InferredJoin,
    OntologyJoinCreate,
    OntologyRelationCreate,
)
from app.infrastructure import neo4j_client as neo4j
from app.services.audit_service import AuditService
from app.services.join_inference import SAGE_X3_REFERENCE_MAP
from app.services.ontology_service import _entityToDict, _logNeo4jFailure, makeJoinKey

logger = logging.getLogger(__name__)

_audit = AuditService()

# 语义关系类型词表（与 ClassRelationType 对齐，service 层校验）
_CLASS_RELATION_VALUES = frozenset(rel.value for rel in ClassRelationType)

# 共享列推断的样板列黑名单（ETL/审计列名约定，横跨几乎全部表，无关联语义）。
# 初版为经验值，后续可按实际数据扩充。
SHARED_COLUMN_BLACKLIST = frozenset(
    {
        "ETL_LOAD_TS",
        "CREATION_DATE",
        "UPDDATTIM_0",
        "CREDATTIM_0",
        "UPDUSR_0",
        "CREUSR_0",
        "CREDAT_0",
        "UPDDAT_0",
        "CREATION_USER",
        "UPDATE_USER",
        "ROWVERSION",
        "DATECRE_0",
        "DATEMAJ_0",
        "TIMCRE_0",
        "TIMUPD_0",
    }
)


def _joinVirtualRow(dto: OntologyJoinCreate) -> SimpleNamespace:
    """预览占位 join 行：仅含覆盖语义字段，供批内重复行判定，不触碰 ORM。"""
    return SimpleNamespace(
        join_type=dto.join_type, relation_type=dto.relation_type, description=dto.description
    )


def _relationVirtualRow(dto: OntologyRelationCreate) -> SimpleNamespace:
    """预览占位语义关系行：仅含可覆盖字段 description。"""
    return SimpleNamespace(description=dto.description)


class OntologyBatchService:
    """批量关系引擎：推断 / 执行 / 只读预览。"""

    async def inferSharedColumnJoins(self, session: AsyncSession) -> list[InferredJoin]:
        """从 live 类/属性推断 join 候选（安全优先，幂等口径 makeJoinKey 去重）。

        Pass A：列名 ∈ SAGE_X3_REFERENCE_MAP → 映射目标表（X3 命名约定，信任注册表）。
        Pass B：通用共享列（排除 X3 已处理列 + 样板黑名单），一方 is_primary_key → FK→PK
                方向（PK 侧为 target），其余类为 source。无 PK 命中则不产生（过滤噪声）。
        均为非墓碑类/属性；跳过自环；目标列用目标类实际列名。
        """
        classes = (
            (
                await session.execute(
                    select(OntologyClass).where(OntologyClass.valid_to.is_(None))
                )
            )
            .scalars()
            .all()
        )
        classNames = {c.id: c.class_name for c in classes}
        tableToClass: dict[str, int] = {}
        for c in classes:
            if not c.source_table:
                continue
            key = c.source_table.rsplit(".", 1)[-1].lower()
            tableToClass.setdefault(key, c.id)
        classIds = list(classNames)
        props = (
            (
                await session.execute(
                    select(OntologyProperty).where(OntologyProperty.class_id.in_(classIds))
                )
            )
            .scalars()
            .all()
        )
        byColumn: dict[str, list[OntologyProperty]] = {}
        for p in props:
            if not p.source_column:
                continue
            byColumn.setdefault(p.source_column.upper(), []).append(p)

        # 目标类列索引：Pass A 命中 X3 注册表后，目标列要落在目标类真实列
        # （注册列若存在则用注册列，否则退到目标表主键列），避免拼接出不存在的列。
        colsByClass: dict[int, set[str]] = {}
        pkColumnByClass: dict[int, str] = {}
        for p in props:
            if not p.source_column:
                continue
            upper = p.source_column.upper()
            colsByClass.setdefault(p.class_id, set()).add(upper)
            if p.is_primary_key:
                pkColumnByClass.setdefault(p.class_id, p.source_column)

        candidates: dict[str, InferredJoin] = {}

        def add(source: OntologyProperty, targetId: int, targetCol: str, by: str) -> None:
            if source.class_id == targetId:
                return
            key = makeJoinKey(
                source.class_id, [source.source_column], targetId, [targetCol]
            )
            if key in candidates:
                return
            candidates[key] = InferredJoin(
                source_class_id=source.class_id,
                source_class_name=classNames.get(source.class_id, str(source.class_id)),
                source_columns=[source.source_column],
                target_class_id=targetId,
                target_class_name=classNames.get(targetId, str(targetId)),
                target_columns=[targetCol],
                relation_type="foreign_key",
                inferred_by=by,
            )

        for col, colProps in byColumn.items():
            ref = SAGE_X3_REFERENCE_MAP.get(col)
            if ref is None:
                continue
            targetTable, targetKey = ref
            targetId = tableToClass.get(targetTable.lower())
            if targetId is None:
                continue
            classCols = colsByClass.get(targetId, set())
            if classCols:
                # 目标类已镜像属性：目标列需落在真实列（注册列缺失则退到主键列，仍无则跳过）
                actualCol = (
                    targetKey
                    if targetKey.upper() in classCols
                    else pkColumnByClass.get(targetId)
                )
                if actualCol is None:
                    continue
            else:
                # 目标类尚未镜像任何属性：信任 X3 注册表命名约定（列名规范由导入保证）
                actualCol = targetKey
            for p in colProps:
                add(p, targetId, actualCol, "name_convention")

        for col, colProps in byColumn.items():
            if col in SHARED_COLUMN_BLACKLIST:
                continue
            if col in SAGE_X3_REFERENCE_MAP:
                continue  # Pass A 已按注册表映射，跳过 all-to-all 噪声
            pkProps = [p for p in colProps if p.is_primary_key]
            if not pkProps:
                continue
            for src in colProps:
                if src.is_primary_key:
                    continue
                for tgt in pkProps:
                    add(src, tgt.class_id, tgt.source_column, "shared_column")

        return [candidates[k] for k in sorted(candidates)]

    async def runBatch(
        self,
        session: AsyncSession,
        request: BatchRelationRequest,
        *,
        actor: str,
        actor_departments: str | None = None,
    ) -> BatchRelationResult:
        """执行批量关系（任选动作），单次 commit；Neo4j 失败不阻断 PG。"""
        mirrors: list[OntologyJoin | OntologyRelation] = []
        result = await self._execute(
            session, request, mirrors=mirrors, write=True,
            actor=actor, actor_departments=actor_departments,
        )
        changed = (
            result.joins.created
            + result.joins.overwritten
            + result.relations.created
            + result.relations.overwritten
        )
        if changed:
            await session.commit()
            # PG 提交成功后才镜像 Neo4j 边：避免 PG 回滚后残留孤儿图边。
            for row in mirrors:
                if isinstance(row, OntologyRelation):
                    self._mirrorRelation(row)
                else:
                    self._mirrorJoin(row)
        return result

    async def preview(
        self, session: AsyncSession, request: BatchRelationRequest
    ) -> BatchRelationResult:
        """只读预览：推断候选 + 冲突预判计数 + 图统计，不写库不 commit。"""
        return await self._execute(session, request, mirrors=[], write=False,
                                   actor="preview")

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    async def _execute(
        self,
        session: AsyncSession,
        request: BatchRelationRequest,
        *,
        mirrors: list[OntologyJoin | OntologyRelation],
        write: bool,
        actor: str,
        actor_departments: str | None = None,
    ) -> BatchRelationResult:
        result = BatchRelationResult()
        if request.sync_graph:
            result.sync_graph = await self._syncGraph(session, write=write)
        inferred: list[InferredJoin] = []
        joinItems: list[tuple[int | None, OntologyJoinCreate]] = []
        relationItems: list[tuple[int, OntologyRelationCreate]] = []
        if request.infer_joins:
            inferred = await self.inferSharedColumnJoins(session)
            result.inferred_joins = inferred
            for cand in inferred:
                joinItems.append(
                    (
                        None,
                        OntologyJoinCreate(
                            source_class_id=cand.source_class_id,
                            source_columns=list(cand.source_columns),
                            target_class_id=cand.target_class_id,
                            target_columns=list(cand.target_columns),
                            relation_type=cand.relation_type,
                        ),
                    )
                )
        manifest = request.manifest
        if request.apply_manifest and manifest is not None:
            for i, dto in enumerate(manifest.joins):
                joinItems.append((i, dto))
            for i, dto in enumerate(manifest.relations):
                relationItems.append((i, dto))

        if joinItems or relationItems:
            liveClassIds = await self._liveClassIdSet(session)
            existingByKey = await self._existingJoinMap(session)
            existingByTriple = await self._existingRelationMap(session)
            if joinItems:
                result.joins = await self._processJoins(
                    session, joinItems, request.on_conflict, liveClassIds,
                    existingByKey, mirrors=mirrors, write=write, actor=actor,
                    actor_departments=actor_departments,
                )
            if relationItems:
                result.relations = await self._processRelations(
                    session, relationItems, request.on_conflict, liveClassIds,
                    existingByTriple, mirrors=mirrors, write=write, actor=actor,
                    actor_departments=actor_departments,
                )
        return result

    async def _syncGraph(
        self, session: AsyncSession, *, write: bool
    ) -> GraphSyncResult | None:
        classes = (
            (
                await session.execute(
                    select(OntologyClass).where(OntologyClass.valid_to.is_(None))
                )
            )
            .scalars()
            .all()
        )
        classIds = [c.id for c in classes]
        props = (
            (
                await session.execute(
                    select(OntologyProperty).where(OntologyProperty.class_id.in_(classIds))
                )
            )
            .scalars()
            .all()
        )
        if not write:  # 预览：投影 PG 计数，不触碰 Neo4j（只读）
            refEdges = sum(1 for p in props if p.ref_class_id is not None)
            return GraphSyncResult(
                classes=len(classes),
                properties=len(props),
                has_property_edges=len(props),
                reference_edges=refEdges,
            )
        classRows = [
            {
                "id": c.id,
                "name": c.class_name,
                "alias": c.class_alias,
                "description": c.description,
                "sourceTable": c.source_table,
            }
            for c in classes
        ]
        propRows = [
            {
                "id": p.id,
                "classId": p.class_id,
                "name": p.property_name,
                "alias": p.property_alias,
                "dataType": p.data_type,
                "sourceColumn": p.source_column,
                "isPrimaryKey": p.is_primary_key,
                "isForeignKey": p.is_foreign_key,
                "refClassId": p.ref_class_id,
            }
            for p in props
        ]
        try:
            counts = neo4j.syncOntologyNodes(classRows, propRows)
            return GraphSyncResult(
                classes=counts["classes"],
                properties=counts["properties"],
                has_property_edges=counts["has_property_edges"],
                reference_edges=counts["reference_edges"],
            )
        except Exception as exc:  # noqa: BLE001 - fail-open
            _logNeo4jFailure("本体入图 syncOntologyNodes", 0, exc)
            return None

    async def _liveClassIdSet(self, session: AsyncSession) -> set[int]:
        ids = (
            await session.execute(select(OntologyClass.id).where(OntologyClass.valid_to.is_(None)))
        ).scalars().all()
        return set(ids)

    async def _existingJoinMap(self, session: AsyncSession) -> dict[str, OntologyJoin]:
        rows = (await session.execute(select(OntologyJoin))).scalars().all()
        return {r.join_key: r for r in rows}

    async def _existingRelationMap(
        self, session: AsyncSession
    ) -> dict[tuple[int, int, str], OntologyRelation]:
        rows = (await session.execute(select(OntologyRelation))).scalars().all()
        return {
            (r.source_class_id, r.target_class_id, r.relation_type): r for r in rows
        }

    @staticmethod
    def _joinValidationError(liveClassIds: set[int], dto: OntologyJoinCreate) -> str | None:
        if dto.source_class_id not in liveClassIds:
            return f"源类 {dto.source_class_id} 不存在或已删除"
        if dto.target_class_id not in liveClassIds:
            return f"目标类 {dto.target_class_id} 不存在或已删除"
        if len(dto.source_columns) != len(dto.target_columns):
            return "源/目标列数不一致"
        return None

    @staticmethod
    def _relationValidationError(
        liveClassIds: set[int], dto: OntologyRelationCreate
    ) -> str | None:
        if dto.relation_type not in _CLASS_RELATION_VALUES:
            return f"非法关系类型 {dto.relation_type}"
        if dto.source_class_id not in liveClassIds:
            return f"源类 {dto.source_class_id} 不存在或已删除"
        if dto.target_class_id not in liveClassIds:
            return f"目标类 {dto.target_class_id} 不存在或已删除"
        if dto.source_class_id == dto.target_class_id:
            return "不允许自环语义关系"
        return None

    @staticmethod
    def _joinDiffs(row: object, dto: OntologyJoinCreate) -> dict[str, object]:
        diffs: dict[str, object] = {}
        for field in ("join_type", "relation_type", "description"):
            newValue = getattr(dto, field)
            if getattr(row, field) != newValue:
                diffs[field] = newValue
        return diffs

    @staticmethod
    def _relationDiffs(
        row: object, dto: OntologyRelationCreate
    ) -> dict[str, object]:
        diffs: dict[str, object] = {}
        if row.description != dto.description:  # type: ignore[union-attr]
            diffs["description"] = dto.description
        return diffs

    async def _processJoins(
        self,
        session: AsyncSession,
        items: list[tuple[int | None, OntologyJoinCreate]],
        onConflict: str,
        liveClassIds: set[int],
        existingByKey: dict[str, object],
        *,
        mirrors: list[OntologyJoin | OntologyRelation],
        write: bool,
        actor: str,
        actor_departments: str | None = None,
    ) -> BatchCounts:
        counts = BatchCounts()
        # 预览（write=False）时用虚拟行占位已判定的新 join，使批内重复行的
        # create/overwrite/skip 计数与执行口径一致（dry-run 不虚增 created）。
        for index, dto in items:
            err = self._joinValidationError(liveClassIds, dto)
            if err:
                if index is not None:
                    counts.errors.append(BatchRowError(index=index, message=err))
                continue
            joinKey = makeJoinKey(
                dto.source_class_id, dto.source_columns,
                dto.target_class_id, dto.target_columns,
            )
            row = existingByKey.get(joinKey)
            if row is not None:
                if onConflict == "skip":
                    counts.skipped += 1
                    continue
                diffs = self._joinDiffs(row, dto)
                if not diffs:
                    counts.skipped += 1
                    continue
                if write:
                    before = _entityToDict(row)
                    for field, value in diffs.items():
                        setattr(row, field, value)
                    await session.flush()
                    await _audit.record(
                        session, entity_type="ONTOLOGY_JOIN", entity_id=row.id,
                        action="UPDATE", actor=actor, actor_departments=actor_departments,
                        before=before, after=_entityToDict(row),
                    )
                    mirrors.append(row)
                else:
                    # 预览：不触碰 ORM 行（避免弄脏会话），用虚拟行表示覆盖后状态
                    existingByKey[joinKey] = _joinVirtualRow(dto)
                counts.overwritten += 1
                continue
            if write:
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
                existingByKey[joinKey] = entity
                await _audit.record(
                    session, entity_type="ONTOLOGY_JOIN", entity_id=entity.id,
                    action="CREATE", actor=actor, actor_departments=actor_departments,
                    after=_entityToDict(entity),
                )
                mirrors.append(entity)
            else:
                existingByKey[joinKey] = _joinVirtualRow(dto)
            counts.created += 1
        return counts

    async def _processRelations(
        self,
        session: AsyncSession,
        items: list[tuple[int, OntologyRelationCreate]],
        onConflict: str,
        liveClassIds: set[int],
        existingByTriple: dict[tuple[int, int, str], object],
        *,
        mirrors: list[OntologyJoin | OntologyRelation],
        write: bool,
        actor: str,
        actor_departments: str | None = None,
    ) -> BatchCounts:
        counts = BatchCounts()
        for index, dto in items:
            err = self._relationValidationError(liveClassIds, dto)
            if err:
                counts.errors.append(BatchRowError(index=index, message=err))
                continue
            triple = (dto.source_class_id, dto.target_class_id, dto.relation_type)
            row = existingByTriple.get(triple)
            if row is not None:
                if onConflict == "skip":
                    counts.skipped += 1
                    continue
                diffs = self._relationDiffs(row, dto)
                if not diffs:
                    counts.skipped += 1
                    continue
                if write:
                    before = _entityToDict(row)
                    for field, value in diffs.items():
                        setattr(row, field, value)
                    await session.flush()
                    await _audit.record(
                        session, entity_type="ONTOLOGY_RELATION", entity_id=row.id,
                        action="UPDATE", actor=actor, actor_departments=actor_departments,
                        before=before, after=_entityToDict(row),
                    )
                    mirrors.append(row)
                else:
                    existingByTriple[triple] = _relationVirtualRow(dto)
                counts.overwritten += 1
                continue
            if write:
                entity = OntologyRelation(
                    source_class_id=dto.source_class_id,
                    target_class_id=dto.target_class_id,
                    relation_type=dto.relation_type,
                    description=dto.description,
                )
                session.add(entity)
                await session.flush()
                existingByTriple[triple] = entity
                await _audit.record(
                    session, entity_type="ONTOLOGY_RELATION", entity_id=entity.id,
                    action="CREATE", actor=actor, actor_departments=actor_departments,
                    after=_entityToDict(entity),
                )
                mirrors.append(entity)
            else:
                existingByTriple[triple] = _relationVirtualRow(dto)
            counts.created += 1
        return counts

    @staticmethod
    def _mirrorJoin(row: OntologyJoin) -> None:
        """(Class)-[:JOIN]->(Class) 图边镜像，best-effort。"""
        try:
            neo4j.linkClassJoin(row.source_class_id, row.target_class_id)
        except Exception as exc:  # noqa: BLE001 - fail-open
            _logNeo4jFailure("批量关联入图", row.id, exc)

    @staticmethod
    def _mirrorRelation(row: OntologyRelation) -> None:
        """(Class)-[:{relType}]->(Class) 图边镜像，best-effort。"""
        try:
            neo4j.linkClassRelation(
                row.source_class_id, row.target_class_id, row.relation_type
            )
        except Exception as exc:  # noqa: BLE001 - fail-open
            _logNeo4jFailure("批量语义关系入图", row.id, exc)
