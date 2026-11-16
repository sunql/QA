"""P4 图谱路（GRAPH）：从**发布快照**召回类 / 属性 / JOIN。

语义源：语义层（类 / 属性 / JOIN 关系）—— 不是业务实体子图
（业务实体子图没有发布版本，只直读 Neo4j 主图 ⇒ 与「引用可回溯」冲突，
已登记为遗留 L-P4-6）。

召回口径
--------
1. **先切域**：只有请求域的条目参与召回（见 `domain_slice`）。
2. **再匹配**：类级词项 = 类名 / 类别名 / 各属性的名字与别名；
   属性级词项 = 属性名 / 属性别名 / 业务别名（`businessAliases`）。
3. **JOIN 附带**：命中类所参与的 JOIN 也进证据（两端都在本域切片内）。
4. **上限**：`topK` 限制本路返回的**证据条数**（超出按得分截断）。
5. **发布面不可用 ⇒ 显式降级**（`DEGRADED` + 原因），**绝不假装是发布读**。

本模块是**纯函数**（不吃 DB、不吃 session）⇒ 可直接单测。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNEL_GRAPH,
    KNOWLEDGE_ROUTE_DEGRADED,
    KNOWLEDGE_ROUTE_EMPTY,
    KNOWLEDGE_ROUTE_OK,
    KNOWLEDGE_SOURCE_RELEASE,
    KnowledgeEvidenceRead,
)
from app.services.knowledge.domain_slice import DomainSlice, sliceManifestByDomain
from app.services.knowledge.matching import (
    classTerms,
    clipText,
    matchTerms,
    propertyTerms,
    sortScored,
    textOrNone,
)
from app.services.knowledge.messages import MSG_RELEASE_UNAVAILABLE
from app.services.knowledge.release_context import ResolvedRelease
from app.services.knowledge.route_input import RouteInput
from app.services.knowledge.route_outcome import RouteOutcome

#: 证据种类。
EVIDENCE_KIND_CLASS = "CLASS"
EVIDENCE_KIND_PROPERTY = "PROPERTY"
EVIDENCE_KIND_JOIN = "JOIN"

#: 片段文本上限（响应体不该被单个长描述撑爆）。
MAX_SNIPPET_LENGTH = 240


def _propertySummary(cls: Mapping[str, Any]) -> str:
    """没有描述时的兜底摘要：「属性：A、B」。"""
    properties = cls.get("properties")
    if not isinstance(properties, list) or not properties:
        return ""
    names: list[str] = []
    for prop in properties:
        if not isinstance(prop, Mapping):
            continue
        label = textOrNone(prop.get("propertyAlias")) or textOrNone(prop.get("propertyName"))
        if label:
            names.append(label)
    if not names:
        return ""
    return "属性：" + "、".join(names)


def _evidence(
    *,
    kind: str,
    ref: str,
    title: str,
    snippet: str,
    release: ResolvedRelease,
) -> KnowledgeEvidenceRead:
    return KnowledgeEvidenceRead(
        channel=KNOWLEDGE_CHANNEL_GRAPH,
        kind=kind,
        ref=ref,
        title=title,
        snippet=clipText(snippet) if snippet else None,
        release_id=release.release_id,
        release_version=release.version,
        source=KNOWLEDGE_SOURCE_RELEASE,
    )


async def runGraphRoute(inp: RouteInput) -> RouteOutcome:
    """图谱路召回。

    Args:
        inp: 编排层给的统一输入（问题 / 域 / 发布面 / 权限口径 / 上限）。

    Returns:
        路由结果（`OK` / `EMPTY` / `DEGRADED`）。
    """
    question = inp.question
    release = inp.release
    domainCode = inp.domain_code
    topK = inp.top_k
    if release.degraded or release.manifest is None:
        return RouteOutcome(
            channel=KNOWLEDGE_CHANNEL_GRAPH,
            status=KNOWLEDGE_ROUTE_DEGRADED,
            degraded_reason=release.reason or MSG_RELEASE_UNAVAILABLE,
        )

    slice_: DomainSlice = sliceManifestByDomain(
        release.manifest, domainCode, entityTypes=inp.entity_filter
    )
    scored: list[tuple[int, str, Any]] = []
    for cls in slice_.classes:
        matched = matchTerms(question, classTerms(cls))
        if not matched:
            continue
        score = sum(item.weight for item in matched)
        scored.append((score, str(cls.get("className")), (cls, matched)))

    evidence: list[KnowledgeEvidenceRead] = []
    matchedClassNames: set[str] = set()
    for _score, className, payload in sortScored(scored):
        if len(evidence) >= topK:
            break
        cls, matched = payload
        matchedClassNames.add(className)
        description = textOrNone(cls.get("description"))
        summary = description or _propertySummary(cls)
        evidence.append(
            _evidence(
                kind=EVIDENCE_KIND_CLASS,
                ref=className,
                title=textOrNone(cls.get("classAlias")) or className,
                snippet=summary or "命中：" + "、".join(item.text for item in matched),
                release=release,
            )
        )
        for prop in cls.get("properties") or []:
            if len(evidence) >= topK:
                break
            if not isinstance(prop, Mapping):
                continue
            propertyMatched = matchTerms(question, propertyTerms(prop))
            if not propertyMatched:
                continue
            propertyName = textOrNone(prop.get("propertyName"))
            if not propertyName:
                continue
            evidence.append(
                _evidence(
                    kind=EVIDENCE_KIND_PROPERTY,
                    ref=f"{className}.{propertyName}",
                    title=textOrNone(prop.get("propertyAlias")) or propertyName,
                    snippet=textOrNone(prop.get("description"))
                    or "命中：" + "、".join(item.text for item in propertyMatched),
                    release=release,
                )
            )

    for join in slice_.joins:
        if len(evidence) >= topK:
            break
        source = textOrNone(join.get("sourceClassName"))
        target = textOrNone(join.get("targetClassName"))
        if source not in matchedClassNames and target not in matchedClassNames:
            continue
        joinKey = textOrNone(join.get("joinKey"))
        if not joinKey:
            continue
        evidence.append(
            _evidence(
                kind=EVIDENCE_KIND_JOIN,
                ref=joinKey,
                title=f"{source} → {target}",
                snippet=textOrNone(join.get("description"))
                or f"{textOrNone(join.get('joinType')) or 'JOIN'} / "
                f"{textOrNone(join.get('relationType')) or '-'}",
                release=release,
            )
        )

    status = KNOWLEDGE_ROUTE_OK if evidence else KNOWLEDGE_ROUTE_EMPTY
    return RouteOutcome(channel=KNOWLEDGE_CHANNEL_GRAPH, status=status, evidence=tuple(evidence))


__all__ = [
    "EVIDENCE_KIND_CLASS",
    "EVIDENCE_KIND_JOIN",
    "EVIDENCE_KIND_PROPERTY",
    "runGraphRoute",
]
