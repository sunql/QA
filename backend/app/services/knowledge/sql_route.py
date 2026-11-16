"""P4 SQL 路（SQL）：从**发布快照**渲染命中候选类的**物理 schema**。

语义源：物理层（表 / 列 / 类型 / JOIN 键）。

为什么按发布读
--------------
物理 schema 是**模型写 SQL 的依据**；口径变了（表重建、列改名、JOIN 键换掉），
旧的 SQL 就悄悄错了 ⇒ 与图谱路 / 指标路同口径，**按发布读**，引用可回溯。

为什么不走 `semantic_schema_gate.renderSchemaWithRelease`
-------------------------------------------------------
那个闸门的开关 `ENABLE_SEMANTIC_REGISTRY_NL2SQL` 是 **NL2SQL 特性开关**（默认关），
它的降级语义是「直读治理表」（那边本来就在读治理表，降级不改变可见性）；
知识服务这边一旦直读治理表，**引用就无法回溯到发布版本**（D33 的发布面口径）。

⇒ 本路改为：**从 manifest 复原渲染视图**（`restoreSnapshotSchema`，P2 的复原层）
⇒ 用**仓内唯一的渲染器**渲染（`nl2sql_schema.buildSchemaText`）
⇒ 渲染结果**天生出自那一版发布** ⇒ 不需要任何开关。

复用的是**复原层 + 渲染器**（不重复实现），不复用的是**闸门的策略**（开关 / 直读降级）。

召回口径
--------
1. **先切域**：只有请求域的类参与（见 `domain_slice`）。
2. **再匹配**：与图谱路同一套词项口径（`matching.classTerms`）⇒ 两路命中一致。
3. **只渲染命中候选类**：不渲染整域 schema（整域 schema 是 NL2SQL prompt 的事，见 L-P4-5）。
4. **上限**：`topK` 限制本路返回的证据条数（类块 + JOIN 键共用这个上限）。

本模块是**纯函数**（不吃 DB、不吃 session）⇒ 可直接单测。
"""

from __future__ import annotations

import logging
from typing import Any

from app.domain.exceptions import DomainError
from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNEL_SQL,
    KNOWLEDGE_ROUTE_DEGRADED,
    KNOWLEDGE_ROUTE_EMPTY,
    KNOWLEDGE_ROUTE_OK,
    KNOWLEDGE_SOURCE_RELEASE,
    KnowledgeEvidenceRead,
)
from app.services.knowledge.domain_slice import sliceManifestByDomain
from app.services.knowledge.matching import classTerms, clipText, matchTerms, sortScored
from app.services.knowledge.messages import MSG_RELEASE_UNAVAILABLE, MSG_SQL_RENDER_FAILED
from app.services.knowledge.release_context import ResolvedRelease
from app.services.knowledge.route_input import RouteInput
from app.services.knowledge.route_outcome import RouteOutcome
from app.services.nl2sql_schema import buildSchemaText
from app.services.semantic_schema_adapter import restoreSnapshotSchema

logger = logging.getLogger(__name__)

#: 证据种类。
EVIDENCE_KIND_TABLE = "TABLE"
EVIDENCE_KIND_JOIN = "JOIN"

#: 片段文本上限（schema 块比描述长 ⇒ 给得比图谱路宽，但仍不许撑爆响应体）。
MAX_SNIPPET_LENGTH = 800


def _blockName(header: str) -> str:
    """从 `### Supplier (供应商): table=X` 里取出类名。"""
    rest = header[4:].strip() if header.startswith("### ") else header.strip()
    for separator in (" ", "(", ":"):
        index = rest.find(separator)
        if index > 0:
            rest = rest[:index]
    return rest


def _splitBlocks(text: str) -> dict[str, str]:
    """把渲染文本按 `### ` 标题切成块（块首行含标题）。"""
    blocks: dict[str, str] = {}
    name: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("### "):
            if name is not None:
                blocks[name] = "\n".join(lines)
            name = _blockName(line)
            lines = [line]
            continue
        if name is not None:
            lines.append(line)
    if name is not None:
        blocks[name] = "\n".join(lines)
    return blocks


def _evidence(
    *,
    kind: str,
    ref: str,
    title: str,
    snippet: str,
    release: ResolvedRelease,
) -> KnowledgeEvidenceRead:
    return KnowledgeEvidenceRead(
        channel=KNOWLEDGE_CHANNEL_SQL,
        kind=kind,
        ref=ref,
        title=title,
        snippet=clipText(snippet, MAX_SNIPPET_LENGTH) if snippet else None,
        release_id=release.release_id,
        release_version=release.version,
        source=KNOWLEDGE_SOURCE_RELEASE,
    )


def _joinSnippet(
    join: Any,
    tableByClassId: dict[int, str],
) -> str | None:
    """JOIN 键的物理写法：`表A.列A = 表B.列B`。"""
    sourceTable = tableByClassId.get(join.source_class_id)
    targetTable = tableByClassId.get(join.target_class_id)
    if not sourceTable or not targetTable:
        return None
    sourceColumn = join.source_columns[0] if join.source_columns else "-"
    targetColumn = join.target_columns[0] if join.target_columns else "-"
    return f"{sourceTable}.{sourceColumn} = {targetTable}.{targetColumn}"


async def runSqlRoute(inp: RouteInput) -> RouteOutcome:
    """SQL 路召回（物理 schema 片段）。

    Args:
        inp: 编排层给的统一输入（问题 / 域 / 发布面 / 权限口径 / 上限）。

    Returns:
        路由结果（`OK` / `EMPTY` / `DEGRADED`）。
    """
    release = inp.release
    if release.degraded or release.manifest is None:
        return RouteOutcome(
            channel=KNOWLEDGE_CHANNEL_SQL,
            status=KNOWLEDGE_ROUTE_DEGRADED,
            degraded_reason=release.reason or MSG_RELEASE_UNAVAILABLE,
        )

    slice_ = sliceManifestByDomain(
        release.manifest, inp.domain_code, entityTypes=inp.entity_filter
    )
    scored: list[tuple[int, str, Any]] = []
    for cls in slice_.classes:
        matched = matchTerms(inp.question, classTerms(cls))
        if not matched:
            continue
        score = sum(item.weight for item in matched)
        scored.append((score, str(cls.get("className")), cls))

    ordered = sortScored(scored)
    if not ordered:
        return RouteOutcome(channel=KNOWLEDGE_CHANNEL_SQL, status=KNOWLEDGE_ROUTE_EMPTY)

    classNames = [className for _score, className, _cls in ordered][: inp.top_k]
    try:
        classes, joins = restoreSnapshotSchema(release.manifest, classNames)
        text = buildSchemaText(classes, joins=joins)
    except DomainError as exc:
        logger.warning("SQL 路：发布快照不可复原，本次降级：%s", exc)
        return RouteOutcome(
            channel=KNOWLEDGE_CHANNEL_SQL,
            status=KNOWLEDGE_ROUTE_DEGRADED,
            degraded_reason=MSG_SQL_RENDER_FAILED,
        )
    except Exception as exc:  # noqa: BLE001 —— 渲染失败不许炸整轮查询
        logger.warning("SQL 路：schema 渲染失败，本次降级：%s", exc, exc_info=True)
        return RouteOutcome(
            channel=KNOWLEDGE_CHANNEL_SQL,
            status=KNOWLEDGE_ROUTE_DEGRADED,
            degraded_reason=MSG_SQL_RENDER_FAILED,
        )

    blocks = _splitBlocks(text)
    classByName = {cls.class_name: cls for cls in classes}
    # 类 id → 类名 / 物理表：**提到循环外**（每条 join 都重建一次表是白烧 CPU），
    # 且 JOIN 的标题必须给人看类名 ⇒ 不许回显内部 id。
    nameById = {cls.id: cls.class_name for cls in classes}
    tableById = {cls.id: cls.source_table or cls.class_name for cls in classes}

    evidence: list[KnowledgeEvidenceRead] = []
    for className in classNames:
        if len(evidence) >= inp.top_k:
            break
        cls = classByName.get(className)
        block = blocks.get(className)
        if cls is None or not block:
            continue
        evidence.append(
            _evidence(
                kind=EVIDENCE_KIND_TABLE,
                ref=className,
                title=cls.class_alias or className,
                snippet=block,
                release=release,
            )
        )

    for join in joins:
        if len(evidence) >= inp.top_k:
            break
        if join.source_class_id is None or join.target_class_id is None:
            continue
        snippet = _joinSnippet(join, tableById)
        if not snippet:
            continue
        sourceName = nameById.get(join.source_class_id, join.join_key)
        targetName = nameById.get(join.target_class_id, join.join_key)
        evidence.append(
            _evidence(
                kind=EVIDENCE_KIND_JOIN,
                ref=join.join_key,
                title=f"{sourceName} → {targetName}",
                snippet=snippet,
                release=release,
            )
        )

    if not evidence:
        return RouteOutcome(channel=KNOWLEDGE_CHANNEL_SQL, status=KNOWLEDGE_ROUTE_EMPTY)
    return RouteOutcome(
        channel=KNOWLEDGE_CHANNEL_SQL, status=KNOWLEDGE_ROUTE_OK, evidence=tuple(evidence)
    )


__all__ = [
    "EVIDENCE_KIND_JOIN",
    "EVIDENCE_KIND_TABLE",
    "runSqlRoute",
]
