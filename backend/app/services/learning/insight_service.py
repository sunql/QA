"""Graph Insights 扫描 + 解读 + 落库（Phase 3 orchestrator）。

执行流程：
1. 装配图快照 → 重算 Louvain 社区（前置依赖）
2. 拓扑扫描三类洞察（无 LLM）
3. 对意外连接 / 桥接节点逐条调 LLM（带 cache 命中跳过）
4. 全量替换 ``wiki_graph_insight``：先删旧（同事务），再插新
5. 返回本轮结果给 API 层

LLM 失败容错：单条解读失败不影响其它条目，整轮仍能落库。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_graph_insight import WikiGraphInsight
from app.services.knowledge_graph.community import (
    detectCommunities,
)
from app.services.knowledge_graph.graph_data import (
    loadKnowledgeGraph,
)
from app.services.knowledge_graph.insights import (
    scanGraph,
)
from app.services.learning.insight_explainer import (
    GraphInsightExplainer,
    InsightExplanation,
)
from app.services.learning.llm_invoker import LearningLLMInvoker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InsightScanResult:
    """一次扫描的最终结果（含已落库的最新解读）。"""

    surprisingConnections: tuple[dict, ...]
    knowledgeGaps: tuple[dict, ...]
    bridgeNodes: tuple[dict, ...]
    # LLM 调用相关指标：本次跑了几条、有几条失败
    explanationAttempts: int
    explanationFailures: int


def _surprisingKey(conn) -> str:
    # 两端 page_id 排序后拼接，避免 A→B 与 B→A 算两条
    a, b = sorted([conn.sourcePageId, conn.targetPageId])
    return f"{a}|{b}"


def _bridgeKey(node) -> str:
    return node.pageId


def _gapKey(gap) -> str:
    """生成 gap 在 DB 里的唯一键。

    **kind 必须入 key**：同一个 page 既可能是 ``ISOLATED_PAGE``（度≤1）
    也可能是 ``MISSING_DIMENSION``（无 dimension）—— 两种诊断角度不同，
    是两条独立 insight。如果只用 pageId 作 key，第二次扫描会把第
    一种覆盖为第二种，丢失信息。
    """
    if gap.kind == "SPARSE_COMMUNITY":
        return f"SPARSE_COMMUNITY|{gap.communityKey or ''}"
    return f"{gap.kind}|{gap.pageId or ''}"


def _surprisingToDict(conn, explanation: str) -> dict:
    return {
        "key": _surprisingKey(conn),
        "headline": conn.headline(),
        "sourcePageId": conn.sourcePageId,
        "sourceTitle": conn.sourceTitle,
        "sourceCommunity": conn.sourceCommunity,
        "sourceDimension": conn.sourceDimension,
        "targetPageId": conn.targetPageId,
        "targetTitle": conn.targetTitle,
        "targetCommunity": conn.targetCommunity,
        "targetDimension": conn.targetDimension,
        "relationType": conn.relationType,
        "explanation": explanation,
    }


def _gapToDict(gap, explanation: str, *, communityTopic: str | None = None) -> dict:
    base = {
        "key": _gapKey(gap),
        "kind": gap.kind,
        "headline": gap.headline(),
        "explanation": explanation,  # 永远空字符串（无 LLM）
    }
    if gap.kind == "SPARSE_COMMUNITY":
        return {
            **base,
            "communityKey": gap.communityKey,
            "communityName": gap.communityName,
            "pageCount": gap.pageCount,
            "cohesionScore": gap.cohesionScore,
            "topic": communityTopic,  # Phase 5.5.3：已有命名时回显
        }
    return {
        **base,
        "pageId": gap.pageId,
        "title": gap.title,
        "degree": gap.degree,
    }


def _bridgeToDict(node, explanation: str) -> dict:
    return {
        "key": _bridgeKey(node),
        "pageId": node.pageId,
        "title": node.title,
        "degree": node.degree,
        "communities": list(node.communities),
        "explanation": explanation,
    }


async def _loadExplanationsFromCache(
    session: AsyncSession, kind: str, keys: list[str]
) -> dict[str, str]:
    if not keys:
        return {}
    rows = (
        await session.execute(
            select(WikiGraphInsight.key, WikiGraphInsight.explanation).where(
                WikiGraphInsight.kind == kind,
                WikiGraphInsight.key.in_(keys),
            )
        )
    ).all()
    return {key: explanation for key, explanation in rows}


async def _upsertInsightRow(
    session: AsyncSession,
    *,
    kind: str,
    key: str,
    headline: str,
    explanation: str,
    payload: dict,
    modelId: int | None,
) -> None:
    row = (
        await session.execute(
            select(WikiGraphInsight).where(
                WikiGraphInsight.kind == kind,
                WikiGraphInsight.key == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = WikiGraphInsight(
            kind=kind,
            key=key,
            payload=payload,
            headline=headline,
            explanation=explanation,
            model_id=modelId,
            explanation_chars=len(explanation),
        )
        session.add(row)
    else:
        row.payload = payload
        row.headline = headline
        row.explanation = explanation
        row.model_id = modelId
        row.explanation_chars = len(explanation)


async def rescanGraphInsights(
    session: AsyncSession,
    *,
    modelId: int,
    invoker: LearningLLMInvoker | None = None,
) -> InsightScanResult:
    """一次完整扫描：重算社区 + 拓扑扫描 + LLM 解读 + 落库。调用方负责 commit。

    ``invoker=None`` 时跳过 LLM 调用（适合测试 / 无模型可用场景）。拓扑照常算。
    """
    data = await loadKnowledgeGraph(session)
    communities = detectCommunities(data)
    insights = scanGraph(data, communities)

    # 把每条记录转 dict 形态，并附 explanation（优先 cache）
    surprisingCache = await _loadExplanationsFromCache(
        session, "SURPRISING_CONNECTION", [_surprisingKey(c) for c in insights.surprisingConnections]
    )
    bridgeCache = await _loadExplanationsFromCache(
        session, "BRIDGE_NODE", [_bridgeKey(n) for n in insights.bridgeNodes]
    )

    explainer = GraphInsightExplainer(invoker) if invoker is not None else None
    attempts = 0
    failures = 0

    surprisingOut: list[dict] = []
    for conn in insights.surprisingConnections:
        key = _surprisingKey(conn)
        explanation = surprisingCache.get(key, "")
        if not explanation and explainer is not None:
            attempts += 1
            result: InsightExplanation = await explainer.explainSurprisingConnection(conn, modelId=modelId)
            explanation = result.explanation
            if not result.ok:
                failures += 1
        surprisingOut.append(_surprisingToDict(conn, explanation))
        await _upsertInsightRow(
            session,
            kind="SURPRISING_CONNECTION",
            key=key,
            headline=conn.headline(),
            explanation=explanation,
            payload={
                "sourcePageId": conn.sourcePageId,
                "targetPageId": conn.targetPageId,
                "relationType": conn.relationType,
                "sourceCommunity": conn.sourceCommunity,
                "targetCommunity": conn.targetCommunity,
                "sourceDimension": conn.sourceDimension,
                "targetDimension": conn.targetDimension,
            },
            modelId=modelId if explanation else None,
        )

    gapOut: list[dict] = []
    # Phase 5.5.3：SPARSE_COMMUNITY gap 回显社区已有 topic（人工/LLM 命名后）
    sparseCommunityTopics: dict[str, str | None] = {}
    sparseKeys = [g.communityKey for g in insights.knowledgeGaps if g.kind == "SPARSE_COMMUNITY" and g.communityKey]
    if sparseKeys:
        from sqlalchemy import select
        from app.domain.wiki_models import KnowledgeCommunity
        rows = (
            await session.execute(
                select(KnowledgeCommunity.community_key, KnowledgeCommunity.topic).where(
                    KnowledgeCommunity.community_key.in_(sparseKeys)
                )
            )
        ).all()
        sparseCommunityTopics = {key: topic for key, topic in rows}

    for gap in insights.knowledgeGaps:
        topic = sparseCommunityTopics.get(gap.communityKey) if gap.kind == "SPARSE_COMMUNITY" else None
        gapOut.append(_gapToDict(gap, "", communityTopic=topic))
        await _upsertInsightRow(
            session,
            kind=f"KNOWLEDGE_GAP_{gap.kind}",
            key=_gapKey(gap),
            headline=gap.headline(),
            explanation="",
            payload={
                "kind": gap.kind,
                "pageId": gap.pageId,
                "communityKey": gap.communityKey,
                "degree": gap.degree,
                "topic": topic,
            },
            modelId=None,
        )

    bridgeOut: list[dict] = []
    for node in insights.bridgeNodes:
        key = _bridgeKey(node)
        explanation = bridgeCache.get(key, "")
        if not explanation and explainer is not None:
            attempts += 1
            result = await explainer.explainBridgeNode(node, modelId=modelId)
            explanation = result.explanation
            if not result.ok:
                failures += 1
        bridgeOut.append(_bridgeToDict(node, explanation))
        await _upsertInsightRow(
            session,
            kind="BRIDGE_NODE",
            key=key,
            headline=node.title,
            explanation=explanation,
            payload={
                "pageId": node.pageId,
                "title": node.title,
                "degree": node.degree,
                "communities": list(node.communities),
            },
            modelId=modelId if explanation else None,
        )

    # 清理陈旧行：本次 scan 不再存在的 (kind, key) 行删除，否则已修复的 gap
    # （如 MISSING_DIMENSION 已被用户补上 dimension）会一直留在表里误导前端。
    await _runPurge(session, insights)

    return InsightScanResult(
        surprisingConnections=tuple(surprisingOut),
        knowledgeGaps=tuple(gapOut),
        bridgeNodes=tuple(bridgeOut),
        explanationAttempts=attempts,
        explanationFailures=failures,
    )


async def _runPurge(
    session: AsyncSession,
    insights: GraphInsights,
) -> int:
    """在 scan 落库后调一次：删除不再存在于本次快照的 insight 行。"""
    return await purgeStaleInsights(
        session,
        surprisingKeys={_surprisingKey(c) for c in insights.surprisingConnections},
        gapKeys={_gapKey(g) for g in insights.knowledgeGaps},
        bridgeKeys={_bridgeKey(n) for n in insights.bridgeNodes},
    )


async def purgeStaleInsights(
    session: AsyncSession,
    *,
    surprisingKeys: set[str],
    gapKeys: set[str],
    bridgeKeys: set[str],
) -> int:
    """删除当前图快照中不再存在的 kind+key 行（重算清理）。

    返回删除行数。orchestrator 在 rescan 末尾调用一次：

    - ``surprisingKeys`` / ``gapKeys`` / ``bridgeKeys`` 是**本次 scan 算出来的**
      存活 key 集合（不是 DB 里已有的 key 集合）。
    - 任何当前 DB 里存在但不在对应集合里的行都被删除 —— 否则旧 gap 行
      （如已经被用户操作消除的 MISSING_DIMENSION）会永远留在表里，
      前端 list 接口会把它们误读成「还有这个缺口」。
    """
    staleConditions = [
        delete(WikiGraphInsight).where(
            WikiGraphInsight.kind == "SURPRISING_CONNECTION",
            WikiGraphInsight.key.notin_(surprisingKeys),
        ),
        delete(WikiGraphInsight).where(
            WikiGraphInsight.kind.like("KNOWLEDGE_GAP_%"),
            WikiGraphInsight.key.notin_(gapKeys),
        ),
        delete(WikiGraphInsight).where(
            WikiGraphInsight.kind == "BRIDGE_NODE",
            WikiGraphInsight.key.notin_(bridgeKeys),
        ),
    ]
    total = 0
    for stmt in staleConditions:
        result = await session.execute(stmt)
        total += result.rowcount or 0
    if total:
        logger.info("purgeStaleInsights: 清理 %s 条陈旧 insight", total)
    return total


__all__ = [
    "InsightScanResult",
    "rescanGraphInsights",
    "purgeStaleInsights",
]
