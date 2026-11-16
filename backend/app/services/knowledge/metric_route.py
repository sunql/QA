"""P4 指标路（METRIC）：从**发布快照**召回指标 / KPI。

语义源：指标与 KPI 的**发布口径**（`metrics[]` / `kpis[]` 分区）。

为什么按发布读而不是直读 `kpi_catalog`
--------------------------------------
治理表是**原地 UPDATE** 的活数据（`status` / `owner` / `dimensionDefaults` 随时在变）
⇒ 直读拿到的内容**没有版本可引** ⇒ 「引用可回溯到发布版本」这条 G5 判据就不成立。
⇒ 指标路读**发布时刻**的口径（这正是「可回溯」的意义）；
治理最新态仍由治理界面提供（本批不提供治理视图，属已知边界 L-P4-4）。

召回口径
--------
1. **先切域**：只有请求域的指标 / KPI 参与召回（见 `domain_slice`）。
2. **指标词项**：指标名 / 指标别名 / 关联 KPI 码 / 目标类名。
3. **KPI 词项**：KPI 码 / KPI 名 / 语义关键词 / 所属指标名。
4. **上限**：`topK` 限制本路返回的**证据条数**。

本模块是**纯函数**（不吃 DB、不吃 session）⇒ 可直接单测。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNEL_METRIC,
    KNOWLEDGE_ROUTE_DEGRADED,
    KNOWLEDGE_ROUTE_EMPTY,
    KNOWLEDGE_ROUTE_OK,
    KNOWLEDGE_SOURCE_RELEASE,
    KnowledgeEvidenceRead,
)
from app.services.knowledge.domain_slice import sliceManifestByDomain
from app.services.knowledge.matching import (
    WEIGHT_ALIAS,
    WEIGHT_NAME,
    TermSpec,
    clipText,
    matchTerms,
    sortScored,
    textOrNone,
)
from app.services.knowledge.messages import MSG_RELEASE_UNAVAILABLE
from app.services.knowledge.release_context import ResolvedRelease
from app.services.knowledge.route_input import RouteInput
from app.services.knowledge.route_outcome import RouteOutcome

#: 证据种类。
EVIDENCE_KIND_METRIC = "METRIC"
EVIDENCE_KIND_KPI = "KPI"

#: 片段文本上限。
MAX_SNIPPET_LENGTH = 240


def _metricTerms(metric: Mapping[str, Any]) -> tuple[TermSpec, ...]:
    """指标的可匹配词项：指标名 / 别名 / 关联 KPI 码 / 目标类名。"""
    terms: list[TermSpec] = []
    for key, weight in (
        ("metricName", WEIGHT_NAME),
        ("metricAlias", WEIGHT_ALIAS),
        ("targetClassName", WEIGHT_NAME),
    ):
        value = textOrNone(metric.get(key))
        if value:
            terms.append(TermSpec(value, weight))
    kpiCodes = metric.get("kpiCodes")
    if isinstance(kpiCodes, list):
        for code in kpiCodes:
            codeText = textOrNone(code)
            if codeText:
                terms.append(TermSpec(codeText, WEIGHT_NAME))
    return tuple(terms)


def _kpiTerms(kpi: Mapping[str, Any]) -> tuple[TermSpec, ...]:
    """KPI 的可匹配词项：KPI 码 / KPI 名 / 语义关键词 / 所属指标名。"""
    terms: list[TermSpec] = []
    for key, weight in (("kpiCode", WEIGHT_NAME), ("kpiName", WEIGHT_ALIAS)):
        value = textOrNone(kpi.get(key))
        if value:
            terms.append(TermSpec(value, weight))
    keywords = kpi.get("semanticKeywords")
    if isinstance(keywords, list):
        for keyword in keywords:
            keywordText = textOrNone(keyword)
            if keywordText:
                terms.append(TermSpec(keywordText, WEIGHT_ALIAS))
    metricName = textOrNone(kpi.get("metricName"))
    if metricName:
        terms.append(TermSpec(metricName, WEIGHT_NAME))
    return tuple(terms)


def _evidence(
    *,
    kind: str,
    ref: str,
    title: str,
    snippet: str,
    release: ResolvedRelease,
) -> KnowledgeEvidenceRead:
    return KnowledgeEvidenceRead(
        channel=KNOWLEDGE_CHANNEL_METRIC,
        kind=kind,
        ref=ref,
        title=title,
        snippet=clipText(snippet) if snippet else None,
        release_id=release.release_id,
        release_version=release.version,
        source=KNOWLEDGE_SOURCE_RELEASE,
    )


async def runMetricRoute(inp: RouteInput) -> RouteOutcome:
    """指标路召回。

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
            channel=KNOWLEDGE_CHANNEL_METRIC,
            status=KNOWLEDGE_ROUTE_DEGRADED,
            degraded_reason=release.reason or MSG_RELEASE_UNAVAILABLE,
        )

    slice_ = sliceManifestByDomain(
        release.manifest, domainCode, entityTypes=inp.entity_filter
    )

    metricScored: list[tuple[int, str, Any]] = []
    for metric in slice_.metrics:
        matched = matchTerms(question, _metricTerms(metric))
        if not matched:
            continue
        score = sum(item.weight for item in matched)
        metricScored.append((score, str(metric.get("metricName")), (metric, matched)))

    kpiScored: list[tuple[int, str, Any]] = []
    for kpi in slice_.kpis:
        matched = matchTerms(question, _kpiTerms(kpi))
        if not matched:
            continue
        score = sum(item.weight for item in matched)
        kpiScored.append((score, str(kpi.get("kpiCode")), (kpi, matched)))

    evidence: list[KnowledgeEvidenceRead] = []
    for _score, _metricName, payload in sortScored(metricScored):
        if len(evidence) >= topK:
            break
        metric, matched = payload
        metricName = str(metric.get("metricName"))
        snippet = (
            textOrNone(metric.get("formula"))
            or textOrNone(metric.get("filterExpression"))
            or "命中：" + "、".join(item.text for item in matched)
        )
        evidence.append(
            _evidence(
                kind=EVIDENCE_KIND_METRIC,
                ref=metricName,
                title=textOrNone(metric.get("metricAlias")) or metricName,
                snippet=snippet,
                release=release,
            )
        )

    for _score, _kpiCode, payload in sortScored(kpiScored):
        if len(evidence) >= topK:
            break
        kpi, matched = payload
        kpiCode = str(kpi.get("kpiCode"))
        snippet = (
            textOrNone(kpi.get("businessDefinition"))
            or textOrNone(kpi.get("formula"))
            or textOrNone(kpi.get("unit"))
            or "命中：" + "、".join(item.text for item in matched)
        )
        evidence.append(
            _evidence(
                kind=EVIDENCE_KIND_KPI,
                ref=kpiCode,
                title=textOrNone(kpi.get("kpiName")) or kpiCode,
                snippet=snippet,
                release=release,
            )
        )

    status = KNOWLEDGE_ROUTE_OK if evidence else KNOWLEDGE_ROUTE_EMPTY
    return RouteOutcome(
        channel=KNOWLEDGE_CHANNEL_METRIC, status=status, evidence=tuple(evidence)
    )


__all__ = [
    "EVIDENCE_KIND_KPI",
    "EVIDENCE_KIND_METRIC",
    "runMetricRoute",
]
