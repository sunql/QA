"""Enterprise Semantic Layer — 三臂语义拆解（feat-research-entry）。

硬约束（设计 §4.6）：
1. 不调 LLM（置信度信号来自检索器原始分数）；
2. 不写库（只读 ontology / wiki 域）；
3. 无状态（构造注入依赖，extract 纯函数式）。
后续优化钩子：LLM 二次精化层放在 ESL 之外（Checkpoint #1 之前），不进本模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import logging

logger = logging.getLogger(__name__)

_METRIC_AMBIGUOUS_GAP = 0.1   # 双源候选分差 < 0.1 ⇒ 歧义冲突
_WIKI_DISAGREE_SCORE = 0.5    # ≥2 篇 score ≥ 0.5 视为共主题
_BO_TOP_K = 5
_METRIC_TOP_K = 5
_WIKI_TOP_K = 5
_TOP_K = {"business_object": _BO_TOP_K, "metric": _METRIC_TOP_K, "knowledge": _WIKI_TOP_K}


@dataclass(frozen=True)
class BusinessObjectRef:
    classId: int
    className: str
    sourceTable: str
    matchedAlias: str
    confidence: float


@dataclass(frozen=True)
class MetricRef:
    metricId: int | None
    kpiCode: str | None
    displayName: str
    formula: str | None
    confidence: float


@dataclass(frozen=True)
class KnowledgeRef:
    pageId: int | None
    title: str
    snippet: str
    semanticScore: float


@dataclass(frozen=True)
class ESLConflict:
    kind: str      # metric_ambiguous | wiki_disagree | bo_join_missing
    arm: str       # business_object | metric | knowledge
    detail: str
    candidates: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ESLExtraction:
    businessObjects: list[BusinessObjectRef]
    metrics: list[MetricRef]
    knowledge: list[KnowledgeRef]
    confidenceByArm: dict[str, float]
    conflicts: list[ESLConflict]


class EmptyResearchScopeError(ValueError):
    """三臂全空：转 Checkpoint #1 强制用户改写问题。"""


BoSearcher = Callable[..., Awaitable[list[dict[str, Any]]]]
KpiMatcher = Callable[..., Awaitable[list[dict[str, Any]]]]
WikiSearcher = Callable[..., Awaitable[list[dict[str, Any]]]]


class EnterpriseSemanticLayer:
    """三臂编排器；依赖全部构造注入，便于 fake 测试。"""

    def __init__(self, *, boSearcher: BoSearcher, kpiMatcher: KpiMatcher,
                 wikiSearcher: WikiSearcher) -> None:
        self._boSearcher = boSearcher
        self._kpiMatcher = kpiMatcher
        self._wikiSearcher = wikiSearcher

    async def extract(self, question: str, *, intent: object | None = None) -> ESLExtraction:
        bos = await self._extractArm("business_object", self._boSearcher, question,
                                     self._toBoRef)
        metrics = await self._extractArm("metric", self._kpiMatcher, question,
                                         self._toMetricRef)
        knowledge = await self._extractArm("knowledge", self._wikiSearcher, question,
                                           self._toKnowledgeRef)
        if not bos and not metrics and not knowledge:
            raise EmptyResearchScopeError("三臂检索全空，需要用户改写问题")
        conflicts = self._detectConflicts(bos, metrics, knowledge)
        return ESLExtraction(
            businessObjects=bos, metrics=metrics, knowledge=knowledge,
            confidenceByArm={
                "business_object": bos[0].confidence if bos else 0.0,
                "metric": metrics[0].confidence if metrics else 0.0,
                "knowledge": knowledge[0].semanticScore if knowledge else 0.0,
            },
            conflicts=conflicts,
        )

    async def _extractArm(self, arm: str, searcher: Callable[..., Awaitable[list]],
                          question: str, toRef: Callable[[dict], Any]) -> list:
        try:
            raw = await searcher(question, topK=_TOP_K[arm])
        except Exception:
            logger.warning("ESL %s 臂检索失败，降级为空臂", arm, exc_info=True)
            return []
        return [toRef(item) for item in raw]

    def _detectConflicts(self, bos, metrics, knowledge) -> list[ESLConflict]:
        conflicts: list[ESLConflict] = []
        if len(metrics) >= 2 and (metrics[0].confidence - metrics[1].confidence) < _METRIC_AMBIGUOUS_GAP:
            conflicts.append(ESLConflict(
                kind="metric_ambiguous", arm="metric",
                detail=f"前两名 metric 分差 < {_METRIC_AMBIGUOUS_GAP}",
                candidates=[{"kpiCode": m.kpiCode, "displayName": m.displayName,
                             "confidence": m.confidence} for m in metrics[:2]]))
        strong = [k for k in knowledge if k.semanticScore >= _WIKI_DISAGREE_SCORE]
        if len(strong) >= 2:
            conflicts.append(ESLConflict(
                kind="wiki_disagree", arm="knowledge",
                detail=f"{len(strong)} 篇高相关 wiki 主题重叠，表述可能冲突",
                candidates=[{"pageId": k.pageId, "title": k.title} for k in strong]))
        return conflicts

    def _toBoRef(self, item: dict) -> BusinessObjectRef:
        return BusinessObjectRef(
            classId=int(item["classId"]), className=item["className"],
            sourceTable=item.get("sourceTable") or "",
            matchedAlias=item.get("matchedAlias") or "",
            confidence=float(item.get("confidence") or 0.0))

    def _toMetricRef(self, item: dict) -> MetricRef:
        return MetricRef(
            metricId=item.get("metricId"), kpiCode=item.get("kpiCode"),
            displayName=item.get("displayName") or "",
            formula=item.get("formula"), confidence=float(item.get("confidence") or 0.0))

    def _toKnowledgeRef(self, item: dict) -> KnowledgeRef:
        return KnowledgeRef(
            pageId=item.get("pageId"), title=item.get("title") or "",
            snippet=(item.get("snippet") or "")[:280],
            semanticScore=float(item.get("score") or 0.0))
