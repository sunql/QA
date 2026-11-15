"""Agent NL2SQL 工具读本体的**钉住闸门**（P3d D29）。

工具（`agent_tools_nl2sql` 的 5 个 handler）原本直读治理表；本闸门让
**钉住了发布**的工具调用改读那一版快照（复用 P2 的复原层），没钉住/钉不住
⇒ `None` ⇒ 调用方直读（与现状逐字一致）。钉住来源 = 编排 run 的
`semantic_release_id`（P3c D27 创建时冻结）。

**降级即回落（G4「随时可回落」）**：那一版被删 / id 非法 / 完整性失败 /
内容畸形 / 意外异常 ⇒ 一律 `None` + 留痕，工具直读治理表 —— 工具**绝不**
因为语义面炸（问答可用性优先，与 `semantic_schema_adapter` 的降级口径一致）。

**为什么全量复原**：工具要的是整张本体（list_tables / list_joins 都是全量），
不是召回子集。快照类名列表取自 manifest 自身 ⇒ 「陈旧」检查天然通过
（陈旧只对「外部给的子集」才有意义），畸形（同名类重复）仍会被抓。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.domain.exceptions import DomainError
from app.services.semantic_schema_adapter import (
    SnapshotClass,
    SnapshotJoin,
    resolveSchemaRelease,
    restoreSnapshotSchema,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolOntologyView:
    """工具用的本体视图：全量类 + 全量 join（快照侧）。

    `SnapshotClass` / `SnapshotJoin` 的字段名与 ORM 逐字对齐 ⇒
    工具渲染函数（`_renderEdge` 等）对两种对象**一视同仁**。
    """

    releaseId: int
    version: str
    classes: tuple[SnapshotClass, ...]
    joins: tuple[SnapshotJoin, ...]


async def loadToolOntology(
    session: Any,
    *,
    pinnedReleaseId: int | None,
    registry: Any = None,
) -> ToolOntologyView | None:
    """按钉住的发布取工具本体视图；钉不住 ⇒ `None`（调用方直读）。"""
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
        return ToolOntologyView(
            releaseId=releaseId,
            version=snapshot.version,
            classes=tuple(classes),
            joins=tuple(joins),
        )
    except DomainError as exc:
        logger.warning(
            "钉住的语义发布内容不可用（releaseId=%s），工具降级直读治理表：%s",
            pinnedReleaseId,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - 语义面任何意外都不许炸工具
        logger.warning(
            "解析钉住的语义发布失败（releaseId=%s），工具降级直读治理表：%s: %s",
            pinnedReleaseId,
            type(exc).__name__,
            exc,
        )
        return None
