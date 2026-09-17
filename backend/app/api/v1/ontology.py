"""本体（Ontology）管理 API。

Class / Property / Metric CRUD + Milvus 语义检索：
- GET/POST       /ontology/classes
- GET/PUT/DELETE /ontology/classes/{id}
- GET/POST       /ontology/properties
- GET/PUT/DELETE /ontology/properties/{id}
- GET/POST       /ontology/metrics
- GET/PUT/DELETE /ontology/metrics/{id}
- POST           /ontology/embeddings/sync
- GET            /ontology/search?q=
"""

from __future__ import annotations

import csv
import io
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.error_messages import (
    MSG_PARAM_EMBEDDING_VECTOR,
    MSG_PARAM_INCLUDE_EXPIRED_ONTOLOGY,
    MSG_PARAM_ONTOLOGY_PG_PRIMARY_KEY,
    MSG_PARAM_SEARCH_ENTITY_TYPE,
    MSG_PARAM_SEARCH_QUERY,
    MSG_PARAM_SEARCH_TOP_K,
)
from app.domain.exceptions import ValidationError
from app.domain.models import OntologyClass
from app.domain.schemas import (
    BatchRelationRequest,
    BatchRelationResult,
    BatchRowError,
    OntologyClassCreate,
    OntologyClassRead,
    OntologyClassUpdate,
    OntologyCsvParseResult,
    OntologyJoinCreate,
    OntologyJoinRead,
    OntologyJoinUpdate,
    OntologyMetricCreate,
    OntologyMetricRead,
    OntologyMetricUpdate,
    OntologyPropertyCreate,
    OntologyPropertyRead,
    OntologyPropertyUpdate,
    OntologyRelationCreate,
    OntologyRelationRead,
    OntologySearchResult,
    RelationBackfillResult,
    RelationManifest,
)
from app.services.embedding_service import EmbeddingService
from app.services.ontology_batch_service import OntologyBatchService
from app.services.ontology_service import OntologyService

router = APIRouter(prefix="/ontology", tags=["ontology"])
# 模块级 EmbeddingService：与 chat 模块同构，由 main.shutdownCleanup 统一关闭
_embeddingService = EmbeddingService()
_ontologyService = OntologyService(embeddingService=_embeddingService)
_batchService = OntologyBatchService()

# CSV 模板表头（关系清单下载/解析共用；列名与 Ontology*Create 契约对齐）
_RELATIONS_TEMPLATE_HEADER = "sourceClassName,targetClassName,relationType,description"
_JOINS_TEMPLATE_HEADER = (
    "sourceClassName,sourceColumns,targetClassName,targetColumns,"
    "joinType,relationType,description"
)
_TEMPLATE_FILENAMES = {
    "relations": "ontology_relations_template.csv",
    "joins": "ontology_joins_template.csv",
}

# 关系清单 CSV 上限：防超大文件拖垮解析（模板行数有限，正常清单远小于此）。
_CSV_MAX_BYTES = 2 * 1024 * 1024
_CSV_READ_CHUNK = 64 * 1024  # 分块读取，超限即中断，避免整包先读入内存
_MSG_CSV_TOO_LARGE = "关系清单 CSV 超过大小限制（2MB）"


# =============================================================================
# Class
# =============================================================================


@router.get("/classes", response_model=list[OntologyClassRead], status_code=status.HTTP_200_OK)
async def listClasses(
    includeExpired: bool = Query(
        False, description=MSG_PARAM_INCLUDE_EXPIRED_ONTOLOGY
    ),
    db: AsyncSession = Depends(getDb),
) -> list[OntologyClassRead]:
    """列出本体类（Phase 6）。

    includeExpired=false（默认）：仅返回未删除的类（valid_to IS NULL）。
    includeExpired=true：额外返回软删除墓碑（版本管理移除后兼容保留）。
    """
    entities = await _ontologyService.listClasses(db, includeExpired=includeExpired)
    return [OntologyClassRead.model_validate(e) for e in entities]


@router.post("/classes", response_model=OntologyClassRead, status_code=status.HTTP_201_CREATED)
async def createClass(
    dto: OntologyClassCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyClassRead:
    """创建本体类（起始 version=1）。

    Phase 4.5：object_owner 由 service 从 actor.departments[0] 派生，DTO
    中不接受该字段（防越权声明）。
    """
    entity = await _ontologyService.createClass(
        db, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyClassRead.model_validate(entity)


@router.get(
    "/classes/{className}/versions",
    response_model=list[OntologyClassRead],
    status_code=status.HTTP_200_OK,
)
async def listClassVersions(
    className: str,
    db: AsyncSession = Depends(getDb),
) -> list[OntologyClassRead]:
    """按 class_name 列出该类的全部行（含软删除墓碑；版本管理移除后兼容保留）。"""
    entities = await _ontologyService.listVersions(db, className)
    return [OntologyClassRead.model_validate(e) for e in entities]


@router.get("/classes/{id}", response_model=OntologyClassRead, status_code=status.HTTP_200_OK)
async def getClass(
    id: int,
    db: AsyncSession = Depends(getDb),
) -> OntologyClassRead:
    """按 ID 查询本体类。"""
    entity = await _ontologyService.getClass(db, id)
    return OntologyClassRead.model_validate(entity)


@router.put("/classes/{id}", response_model=OntologyClassRead, status_code=status.HTTP_200_OK)
async def updateClass(
    id: int,
    dto: OntologyClassUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyClassRead:
    """更新本体类（Phase 6：创建新版本而非原地修改，返回新 id）。

    Phase 4.5 扩展：走 owner-based ACL（object_owner 字段）。
    """
    entity = await _ontologyService.updateClass(
        db, id, dto,
        actor=user,
    )
    return OntologyClassRead.model_validate(entity)


@router.delete("/classes/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteClass(
    id: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """软删除本体类：valid_to = now()（墓碑），listClasses 默认不再返回。

    Phase 4.5 扩展：走 owner-based ACL。
    """
    await _ontologyService.deleteClass(
        db, id,
        actor=user,
    )


# =============================================================================
# Property
# =============================================================================


@router.get(
    "/classes/{classId}/properties",
    response_model=list[OntologyPropertyRead],
    status_code=status.HTTP_200_OK,
)
async def listPropertiesByClass(
    classId: int,
    db: AsyncSession = Depends(getDb),
) -> list[OntologyPropertyRead]:
    """列出某本体类的所有属性。"""
    entities = await _ontologyService.listPropertiesByClass(db, classId)
    return [OntologyPropertyRead.model_validate(e) for e in entities]


@router.get(
    "/properties",
    response_model=list[OntologyPropertyRead],
    status_code=status.HTTP_200_OK,
)
async def listAllProperties(
    db: AsyncSession = Depends(getDb),
) -> list[OntologyPropertyRead]:
    """列出全部本体属性（跨类）。本体属性管理页（/ontology-properties）专用。"""
    entities = await _ontologyService.listAllProperties(db)
    return [OntologyPropertyRead.model_validate(e) for e in entities]


@router.post(
    "/properties",
    response_model=OntologyPropertyRead,
    status_code=status.HTTP_201_CREATED,
)
async def createProperty(
    dto: OntologyPropertyCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyPropertyRead:
    """创建本体属性。"""
    entity = await _ontologyService.createProperty(
        db, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyPropertyRead.model_validate(entity)


@router.get(
    "/properties/{id}",
    response_model=OntologyPropertyRead,
    status_code=status.HTTP_200_OK,
)
async def getProperty(
    id: int,
    db: AsyncSession = Depends(getDb),
) -> OntologyPropertyRead:
    """按 ID 查询本体属性。"""
    entity = await _ontologyService.getProperty(db, id)
    return OntologyPropertyRead.model_validate(entity)


@router.put(
    "/properties/{id}",
    response_model=OntologyPropertyRead,
    status_code=status.HTTP_200_OK,
)
async def updateProperty(
    id: int,
    dto: OntologyPropertyUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyPropertyRead:
    """更新本体属性。"""
    entity = await _ontologyService.updateProperty(
        db, id, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyPropertyRead.model_validate(entity)


@router.delete("/properties/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteProperty(
    id: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """删除本体属性。"""
    await _ontologyService.deleteProperty(
        db, id,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )


# =============================================================================
# Metric
# =============================================================================


@router.get("/metrics", response_model=list[OntologyMetricRead], status_code=status.HTTP_200_OK)
async def listMetrics(
    db: AsyncSession = Depends(getDb),
) -> list[OntologyMetricRead]:
    """列出所有本体指标。"""
    entities = await _ontologyService.listMetrics(db)
    return [OntologyMetricRead.model_validate(e) for e in entities]


@router.post("/metrics", response_model=OntologyMetricRead, status_code=status.HTTP_201_CREATED)
async def createMetric(
    dto: OntologyMetricCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyMetricRead:
    """创建本体指标。"""
    entity = await _ontologyService.createMetric(
        db, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyMetricRead.model_validate(entity)


@router.get("/metrics/{id}", response_model=OntologyMetricRead, status_code=status.HTTP_200_OK)
async def getMetric(
    id: int,
    db: AsyncSession = Depends(getDb),
) -> OntologyMetricRead:
    """按 ID 查询本体指标。"""
    entity = await _ontologyService.getMetric(db, id)
    return OntologyMetricRead.model_validate(entity)


@router.put("/metrics/{id}", response_model=OntologyMetricRead, status_code=status.HTTP_200_OK)
async def updateMetric(
    id: int,
    dto: OntologyMetricUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyMetricRead:
    """更新本体指标。"""
    entity = await _ontologyService.updateMetric(
        db, id, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyMetricRead.model_validate(entity)


@router.delete("/metrics/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteMetric(
    id: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """删除本体指标。"""
    await _ontologyService.deleteMetric(
        db, id,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )


# =============================================================================
# Join（关联关系目录）
# =============================================================================


@router.get("/joins", response_model=list[OntologyJoinRead], status_code=status.HTTP_200_OK)
async def listJoins(
    db: AsyncSession = Depends(getDb),
) -> list[OntologyJoinRead]:
    """列出全部 join 边（运行时 JOIN 生成的唯一真源）。"""
    entities = await _ontologyService.listJoins(db)
    return [OntologyJoinRead.model_validate(e) for e in entities]


@router.post("/joins", response_model=OntologyJoinRead, status_code=status.HTTP_201_CREATED)
async def createJoin(
    dto: OntologyJoinCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyJoinRead:
    """创建 join 边。"""
    entity = await _ontologyService.createJoin(
        db, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyJoinRead.model_validate(entity)


@router.put("/joins/{id}", response_model=OntologyJoinRead, status_code=status.HTTP_200_OK)
async def updateJoin(
    id: int,
    dto: OntologyJoinUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyJoinRead:
    """更新 join 边。"""
    entity = await _ontologyService.updateJoin(
        db, id, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyJoinRead.model_validate(entity)


@router.delete("/joins/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteJoin(
    id: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """删除 join 边。"""
    await _ontologyService.deleteJoin(
        db, id,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )


# =============================================================================
# Semantic Relation（类 × 类语义关系）
# =============================================================================


@router.get(
    "/relations", response_model=list[OntologyRelationRead], status_code=status.HTTP_200_OK
)
async def listRelations(
    db: AsyncSession = Depends(getDb),
) -> list[OntologyRelationRead]:
    """列出全部类级语义关系（按 id 升序）。"""
    entities = await _ontologyService.listRelations(db)
    return [OntologyRelationRead.model_validate(e) for e in entities]


@router.post(
    "/relations",
    response_model=OntologyRelationRead,
    status_code=status.HTTP_201_CREATED,
)
async def createRelation(
    dto: OntologyRelationCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyRelationRead:
    """创建类级语义关系（PG SSOT + Neo4j 镜像边）。"""
    entity = await _ontologyService.createRelation(
        db, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyRelationRead.model_validate(entity)


@router.delete("/relations/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteRelation(
    id: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """删除类级语义关系（同步删除 Neo4j 镜像边）。"""
    await _ontologyService.deleteRelation(
        db, id,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )


@router.post(
    "/relations/backfill",
    response_model=RelationBackfillResult,
    status_code=status.HTTP_200_OK,
)
async def backfillRelations(
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> RelationBackfillResult:
    """一键补关系：join 全量入图 + X3 外键补 ref_class_id（幂等修复）。

    访问控制：仅要求登录态（与 join/property 写入一致）；被补全的外键引用逐条
    落 ONTOLOGY_PROPERTY UPDATE 审计归属到当前 user（见 service.backfillRelations）。
    返回 {syncedJoins, backfilledReferences}，供前端提示本次修复量。
    """
    counts = await _ontologyService.backfillRelations(
        db,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return RelationBackfillResult(**counts)


# =============================================================================
# Batch Relation Engine（通用批量关系引擎：join / 语义关系 / 本体入图 + 模板清单）
# =============================================================================


class _ClassIndex:
    """一次性加载的 live 类目录，供 CSV 清单逐行按类名/source_table 尾段反解 id。

    相较逐行查询 DB：避免 N×全表扫描的放大；对重复类名/多表同名尾段显式判歧义
    （返回行级错误），而不是静默取首个或抛 MultipleResultsFound 500。
    """

    def __init__(self) -> None:
        self._byName: dict[str, int] = {}
        self._ambiguousNames: set[str] = set()
        self._byTail: dict[str, list[int]] = {}

    def add(self, classId: int, className: str | None, sourceTable: str | None) -> None:
        if className:
            key = className.upper()
            if key in self._byName or key in self._ambiguousNames:
                self._byName.pop(key, None)
                self._ambiguousNames.add(key)
            else:
                self._byName[key] = classId
        if sourceTable:
            tail = sourceTable.rsplit(".", 1)[-1].upper()
            self._byTail.setdefault(tail, []).append(classId)

    def resolve(self, name: str) -> tuple[int | None, str | None]:
        """按类名或 source_table 尾段解析 id；歧义/缺失返回 (None, 原因)。"""
        trimmed = name.strip()
        if not trimmed:
            return None, None
        upper = trimmed.upper()
        if upper in self._ambiguousNames:
            return None, "类名重复，请在清单中使用唯一类名/表名"
        exactId = self._byName.get(upper)
        if exactId is not None:
            return exactId, None
        tail = trimmed.rsplit(".", 1)[-1].upper()
        candidates = self._byTail.get(upper) or self._byTail.get(tail) or []
        if not candidates:
            return None, None
        if len(candidates) > 1:
            return None, "类名/表名匹配到多个类，请在清单中使用唯一类名"
        return candidates[0], None


def _pydanticRowMessage(exc: PydanticValidationError) -> str:
    """提取首个 pydantic 校验错误为 CSV 行级错误文案（超长/非法字段等）。"""
    errors = exc.errors()
    if errors and errors[0].get("msg"):
        return f"字段非法：{errors[0]['msg']}"
    return "字段非法或超长"


def _splitCsvColumns(raw: str) -> list[str]:
    """CSV 单格多列：用 ; 或空格分隔，去空。"""
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def _parseRelationsCsv(
    text: str, index: _ClassIndex
) -> tuple[list[OntologyRelationCreate], list[BatchRowError]]:
    relations: list[OntologyRelationCreate] = []
    errors: list[BatchRowError] = []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return relations, errors
    for lineno, row in enumerate(reader, start=2):  # 数据行从第 2 行起
        source, srcErr = index.resolve(row.get("sourceClassName") or "")
        target, tgtErr = index.resolve(row.get("targetClassName") or "")
        if source is None or target is None:
            errors.append(
                BatchRowError(
                    index=lineno,
                    message=srcErr or tgtErr or "源类或目标类不存在（须为已入系统的类名/表名）",
                )
            )
            continue
        relType = (row.get("relationType") or "").strip().upper()
        if not relType:
            errors.append(BatchRowError(index=lineno, message="relationType 不能为空"))
            continue
        try:
            relations.append(
                OntologyRelationCreate(
                    source_class_id=source,
                    target_class_id=target,
                    relation_type=relType,
                    description=(row.get("description") or "").strip() or None,
                )
            )
        except PydanticValidationError as exc:
            # 超长/非法字段逐行记错，而不是让整个解析 500
            errors.append(BatchRowError(index=lineno, message=_pydanticRowMessage(exc)))
    return relations, errors


def _parseJoinsCsv(
    text: str, index: _ClassIndex
) -> tuple[list[OntologyJoinCreate], list[BatchRowError]]:
    joins: list[OntologyJoinCreate] = []
    errors: list[BatchRowError] = []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return joins, errors
    for lineno, row in enumerate(reader, start=2):
        source, srcErr = index.resolve(row.get("sourceClassName") or "")
        target, tgtErr = index.resolve(row.get("targetClassName") or "")
        sourceCols = _splitCsvColumns(row.get("sourceColumns") or "")
        targetCols = _splitCsvColumns(row.get("targetColumns") or "")
        if source is None or target is None:
            errors.append(
                BatchRowError(
                    index=lineno,
                    message=srcErr or tgtErr or "源类或目标类不存在（须为已入系统的类名/表名）",
                )
            )
            continue
        if not sourceCols or not targetCols:
            errors.append(BatchRowError(index=lineno, message="sourceColumns/targetColumns 不能为空"))
            continue
        if len(sourceCols) != len(targetCols):
            errors.append(BatchRowError(index=lineno, message="源/目标列数不一致"))
            continue
        try:
            joins.append(
                OntologyJoinCreate(
                    source_class_id=source,
                    source_columns=sourceCols,
                    target_class_id=target,
                    target_columns=targetCols,
                    join_type=(row.get("joinType") or "INNER").strip().upper() or "INNER",
                    relation_type=(row.get("relationType") or "foreign_key").strip(),
                    description=(row.get("description") or "").strip() or None,
                )
            )
        except PydanticValidationError as exc:
            # 超长/非法字段逐行记错，而不是让整个解析 500
            errors.append(BatchRowError(index=lineno, message=_pydanticRowMessage(exc)))
    return joins, errors


@router.post(
    "/batch",
    response_model=BatchRelationResult,
    status_code=status.HTTP_200_OK,
)
async def runOntologyBatch(
    dto: BatchRelationRequest,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> BatchRelationResult:
    """批量关系引擎执行：syncGraph / inferJoins / applyManifest（可多选）。

    对已存在关系按 onConflict（skip/overwrite）处理；Neo4j 失败不阻断 PG。
    写操作逐行落审计（ONTOLOGY_JOIN / ONTOLOGY_RELATION），归属当前 user。
    """
    return await _batchService.runBatch(
        db, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )


@router.post(
    "/batch/preview",
    response_model=BatchRelationResult,
    status_code=status.HTTP_200_OK,
)
async def previewOntologyBatch(
    dto: BatchRelationRequest,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> BatchRelationResult:
    """批量关系引擎只读预览：推断候选 + 冲突预判计数，不写库。"""
    return await _batchService.preview(db, dto)


@router.get("/batch/template", status_code=status.HTTP_200_OK)
async def downloadBatchTemplate(
    kind: Literal["joins", "relations"] = "relations",
) -> Response:
    """下载关系清单 CSV 模板（Excel 可编辑后上传 parse-csv）。"""
    header = _JOINS_TEMPLATE_HEADER if kind == "joins" else _RELATIONS_TEMPLATE_HEADER
    content = "﻿" + header + "\n"  # BOM 便于 Excel 正确识别 UTF-8
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{_TEMPLATE_FILENAMES[kind]}"'
        },
    )


@router.post(
    "/batch/parse-csv",
    response_model=OntologyCsvParseResult,
    status_code=status.HTTP_200_OK,
)
async def parseOntologyBatchCsv(
    kind: Literal["joins", "relations"] = Form("relations"),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyCsvParseResult:
    """上传关系清单 CSV → 按类名反解 id + 校验，返回可执行的 manifest JSON。

    返回后前端把 manifest 填入批量弹窗 JSON 区，用户可再编辑并执行。
    访问控制：需登录态（与 /batch、/batch/preview 一致）。
    """
    # 分块读取 + 硬上限：超限立即 422，不先把整个上传包读入内存
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_CSV_READ_CHUNK):
        total += len(chunk)
        if total > _CSV_MAX_BYTES:
            raise ValidationError(_MSG_CSV_TOO_LARGE)
        chunks.append(chunk)
    text = b"".join(chunks).decode("utf-8-sig", errors="replace")
    # live 类一次性载入做目录（类名 + source_table 尾段），逐行内存反解
    classes = (
        (
            await db.execute(
                select(OntologyClass).where(OntologyClass.valid_to.is_(None))
            )
        )
        .scalars()
        .all()
    )
    index = _ClassIndex()
    for cls in classes:
        index.add(cls.id, cls.class_name, cls.source_table)
    if kind == "joins":
        joins, errors = _parseJoinsCsv(text, index)
        return OntologyCsvParseResult(manifest=RelationManifest(joins=joins), errors=errors)
    relations, errors = _parseRelationsCsv(text, index)
    return OntologyCsvParseResult(
        manifest=RelationManifest(relations=relations), errors=errors
    )


# =============================================================================
# Semantic Search — Milvus
# =============================================================================


class EmbeddingSyncRequest(BaseModel):
    """embedding 同步请求体。向量由调用方通过 LLM 生成后传入。"""

    ontologyId: int = Field(..., description=MSG_PARAM_ONTOLOGY_PG_PRIMARY_KEY)
    type: str = Field(..., pattern=r"^(class|property|metric)$")
    name: str
    alias: str | None = None
    description: str | None = None
    embedding: list[float] = Field(..., description=MSG_PARAM_EMBEDDING_VECTOR)


@router.post("/embeddings/sync", status_code=status.HTTP_204_NO_CONTENT)
async def syncEmbedding(payload: EmbeddingSyncRequest) -> None:
    """将单条 embedding 同步到 Milvus（新建或覆盖）。"""
    _ontologyService.syncEmbedding(
        ontologyId=payload.ontologyId,
        type=payload.type,
        name=payload.name,
        alias=payload.alias,
        description=payload.description,
        embedding=payload.embedding,
    )


class EmbeddingSyncFailure(BaseModel):
    """对账补同步单条失败记录。"""

    classId: int
    className: str
    error: str


class EmbeddingSyncMissingResult(BaseModel):
    """类向量对账摘要：以 PG 为真源补齐 Milvus 缺失的类向量。"""

    totalClasses: int
    missingCount: int
    syncedCount: int
    failedCount: int
    failures: list[EmbeddingSyncFailure]


@router.post(
    "/embeddings/sync-missing",
    response_model=EmbeddingSyncMissingResult,
    status_code=status.HTTP_200_OK,
)
async def syncMissingEmbeddings(db: AsyncSession = Depends(getDb)) -> EmbeddingSyncMissingResult:
    """向量对账：为 PG 有而 Milvus 缺失的未软删类补生成向量（批量人工同步入口）。"""
    result = await _ontologyService.syncMissingClassEmbeddings(db)
    return EmbeddingSyncMissingResult(**result)


@router.post("/classes/{id}/embedding", status_code=status.HTTP_204_NO_CONTENT)
async def syncClassEmbedding(
    id: int,
    db: AsyncSession = Depends(getDb),
) -> None:
    """手动同步单个类向量：服务端按 PG 当前数据重新生成 embedding 并覆盖 Milvus。"""
    await _ontologyService.syncClassEmbedding(db, id)


@router.get(
    "/search",
    response_model=list[OntologySearchResult],
    status_code=status.HTTP_200_OK,
)
async def searchOntology(
    q: str = Query(..., min_length=1, max_length=200, description=MSG_PARAM_SEARCH_QUERY),
    topK: int = Query(10, ge=1, le=50, description=MSG_PARAM_SEARCH_TOP_K),
    type: str | None = Query(
        None, pattern=r"^(class|property|metric)$", description=MSG_PARAM_SEARCH_ENTITY_TYPE
    ),
) -> list[OntologySearchResult]:
    """本体语义检索：关键词 -> embedding -> Milvus 近邻搜索。

    依赖 ontology_embeddings 集合中已同步的向量（通过 /embeddings/sync 写入）。
    """
    return await _ontologyService.searchByKeyword(q, topK=topK, typeFilter=type)
