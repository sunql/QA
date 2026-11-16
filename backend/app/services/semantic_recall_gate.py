"""召回侧的语义钉住闸门（P3d D30）。

`classifyAndRecall` 的整条召回链（全量类 → 关键词/子集筛选 → DIM 补拉 →
join 扩边）在**钉住了发布**时改读那一版快照；没钉住/钉不住 ⇒ `None` ⇒
召回照旧直读治理表（与现状一致）。

**为什么是整条链而不是只切 DIM**：混合口径（DIM 从快照、其它从活表）会
静默破坏「按发布读」的合同——同一个问句里两半用了两个版本的本体。

**为什么视图必须可变**：召回管线会往类对象上写诊断字段
（`cls._recall_score = ...`，chat_recall 扩边后回填）；P2 渲染视图
（`SnapshotClass`）是 frozen 的 ⇒ 召回侧用**可变**的 `RecallClass`
（字段名与 ORM 逐字对齐 ⇒ 层判定/ODS 过滤/排序一视同仁）。

**降级即回落（G4）**：那一版被删 / 完整性失败 / 畸形 / 意外异常 ⇒
`None` + 留痕，召回降级直读（问答不因语义面失败）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.domain.exceptions import DomainError
from app.services.semantic_schema_adapter import (
    resolveSchemaRelease,
    restoreSnapshotSchema,
)

logger = logging.getLogger(__name__)


@dataclass
class RecallClass:
    """召回视图的类（**可变**：管线要写 `_recall_score`）。

    字段名与 ORM `OntologyClass` 逐字对齐（`_getClassLayer` / `_isOdsBusinessTable` /
    排序键都按这些字段读）。
    """

    id: int
    class_name: str
    class_alias: str | None
    description: str | None
    source_table: str | None
    object_type: str | None
    object_owner: str | None
    parent_class_id: int | None


@dataclass
class RecallJoinView:
    """召回视图的 join（字段名与 ORM `OntologyJoin` 对齐；扩边只读两端 id）。"""

    id: int
    source_class_id: int | None
    target_class_id: int | None


@dataclass(frozen=True)
class RecallOntology:
    """召回视图：全量类 + 全量 join（同一版快照 ⇒ 同一口径）。"""

    releaseId: int
    version: str
    classes: list[RecallClass]
    joins: list[RecallJoinView]


def _buildViews(
    classes: list[Any], joins: list[Any]
) -> tuple[list[RecallClass], list[RecallJoinView]]:
    recallClasses = [
        RecallClass(
            id=c.id,
            class_name=c.class_name,
            class_alias=c.class_alias,
            description=c.description,
            source_table=c.source_table,
            object_type=c.object_type,
            object_owner=c.object_owner,
            parent_class_id=c.parent_class_id,
        )
        for c in classes
    ]
    recallJoins = [
        RecallJoinView(
            id=j.id,
            source_class_id=j.source_class_id,
            target_class_id=j.target_class_id,
        )
        for j in joins
    ]
    return recallClasses, recallJoins


async def loadRecallOntology(
    session: Any,
    *,
    pinnedReleaseId: int | None,
    registry: Any = None,
) -> RecallOntology | None:
    """按钉住的发布取召回视图；钉不住 ⇒ `None`（调用方直读治理表）。"""
    if pinnedReleaseId is None:
        return None
    try:
        snapshot, releaseId = await resolveSchemaRelease(
            session, releaseId=pinnedReleaseId, registry=registry
        )
        if snapshot is None or releaseId is None:
            return None
        manifest = snapshot.manifest
        classNames = [c["className"] for c in manifest.get("classes", [])]
        classes, joins = restoreSnapshotSchema(manifest, classNames)
        recallClasses, recallJoins = _buildViews(classes, joins)
        return RecallOntology(
            releaseId=releaseId,
            version=snapshot.version,
            classes=recallClasses,
            joins=recallJoins,
        )
    except DomainError as exc:
        logger.warning(
            "钉住的语义发布内容不可用（releaseId=%s），召回降级直读治理表：%s",
            pinnedReleaseId,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - 语义面任何意外都不许炸召回
        logger.warning(
            "解析钉住的语义发布失败（releaseId=%s），召回降级直读治理表：%s: %s",
            pinnedReleaseId,
            type(exc).__name__,
            exc,
        )
        return None
