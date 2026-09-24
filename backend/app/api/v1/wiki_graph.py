"""知识图谱分析 API（Phase 2：4-Signal 相关性 + Louvain 社区检测）。

- GET  /wiki/graph/relevance              两页相关性（4 信号分解）
- GET  /wiki/graph/communities            社区列表（已落库形态）
- POST /wiki/graph/communities/recompute  全量重算社区（幂等：同图同结果）
- GET  /wiki/graph/view                   图可视化载荷（节点 + 边 + 相关性得分）

图的定义见 ``services/knowledge_graph/graph_data.py``：节点 = 非 EXPIRED 的
WikiPage，边 = 已确认且未打回的 Page↔Page KnowledgeRelation。

整组路由默认要求认证（与 wiki.py 同约定）。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getCurrentUser, getDb

# 注册 WikiGraphInsight 模型到 Base.metadata（供运行时反射 / Alembic 探测）
from app.domain import wiki_graph_insight  # noqa: F401
from app.domain.wiki_models import KnowledgeCommunity, KnowledgeCommunityMember
from app.domain.wiki_schemas import (
    BridgeNodeRead,
    CommunityRecomputeRead,
    GraphEdgeRead,
    GraphInsightsRead,
    GraphInsightsRescanRead,
    GraphNodeRead,
    GraphRelevanceRead,
    GraphViewRead,
    KnowledgeCommunityRead,
    KnowledgeGapRead,
    SurprisingConnectionRead,
    WikiCommunityTopicSuggestRead,
    WikiCommunityTopicUpdateRead,
    WikiCommunityUpdateRequest,
)
from app.infrastructure.rate_limit import limiter, rateLimitValue
from app.services.knowledge_graph import (
    computeRelevance,
    listCommunities,
    loadKnowledgeGraph,
    recomputeCommunities,
)
from app.services.knowledge_graph.graph_data import NODE_DIMENSION, NODE_TITLE

router = APIRouter(
    prefix="/wiki/graph",
    # 整组路由默认要求认证（与 wiki.py 同约定，见该文件头注释）
    dependencies=[Depends(getCurrentUser)],
)

# 可视化载荷的节点上限：force layout 超过这个量级前端会卡，后端全量算分也慢。
# 截断策略：按度数降序取前 N 个节点，边只保留两端都在集合内的。
_MAX_GRAPH_NODES = 500


@router.get(
    "/relevance",
    response_model=GraphRelevanceRead,
    status_code=status.HTTP_200_OK,
)
async def getRelevance(
    pageA: str = Query(..., min_length=1, max_length=64),
    pageB: str = Query(..., min_length=1, max_length=64),
    db: AsyncSession = Depends(getDb),
) -> GraphRelevanceRead:
    """两页相关性（4-Signal 分解）。节点不存在/相同 → 全零分，不 404。"""
    data = await loadKnowledgeGraph(db)
    score = computeRelevance(data, pageA, pageB)
    return GraphRelevanceRead(
        page_a=pageA,
        page_b=pageB,
        total=score.total,
        direct_link=score.directLink,
        source_overlap=score.sourceOverlap,
        adamic_adar=score.adamicAdar,
        type_affinity=score.typeAffinity,
    )


@router.get(
    "/communities",
    response_model=list[KnowledgeCommunityRead],
    status_code=status.HTTP_200_OK,
)
async def getCommunities(db: AsyncSession = Depends(getDb)) -> list[KnowledgeCommunityRead]:
    """社区列表（当前落库形态，按规模降序）。"""
    communities = await listCommunities(db)
    return [KnowledgeCommunityRead.model_validate(c) for c in communities]


@router.post(
    "/communities/recompute",
    response_model=CommunityRecomputeRead,
    status_code=status.HTTP_200_OK,
)
@limiter.limit(rateLimitValue)
async def recomputeGraphCommunities(
    request: Request,
    db: AsyncSession = Depends(getDb),
) -> CommunityRecomputeRead:
    """全量重算 Louvain 社区（幂等：同图同结果，seed 固定）。

    计算在内存中一次完成（万级节点毫秒~秒级），结果全量替换落库。
    限流与 detect/discover 同档：虽是纯计算无 LLM，但全量扫表 + 删写
    不应被连点放大。
    """
    data = await loadKnowledgeGraph(db)
    results = await recomputeCommunities(db, data)
    await db.commit()
    return CommunityRecomputeRead(
        community_count=len(results),
        member_page_count=sum(len(r.pageIds) for r in results),
    )


@router.post(
    "/communities/{communityKey}/topic-suggest",
    response_model=WikiCommunityTopicSuggestRead,
    status_code=status.HTTP_200_OK,
)
@limiter.limit(rateLimitValue)
async def suggestCommunityTopic(
    request: Request,
    communityKey: str,
    db: AsyncSession = Depends(getDb),
) -> WikiCommunityTopicSuggestRead:
    """Phase 5.5：SPARSE_COMMUNITY 缺口的两步预览。

    读社区内 page 标题列表 → 调 LLM 一次生成主题建议 → **不写库**，
    把建议 + 标题清单返给前端 Modal；用户确认后再走
    ``PATCH /communities/{communityKey}`` 真正落库到
    ``knowledge_community.topic``。

    与 ``/communities/recompute`` 的关键差异：
    - /recompute 是「重新跑 Louvain 全量重算」——会把 topic 也一起覆盖
      （重算会全量删旧写新 knowledge_community 行），所以用户的 topic
      命名会丢
    - /topic-suggest 只读不改：与已有「自动检测的机器编号 name」并行不悖

    **限流**：LLM 调用入口，按 detect/discover 同档配置。
    """
    from fastapi import HTTPException
    from app.domain.exceptions import LLMUnavailableError
    from app.services.learning.community_topic_suggester import (
        CommunityTopicSuggester,
        loadCommunityTitles,
    )
    from app.services.learning.llm_invoker import LearningLLMInvoker
    from app.services.model_config_service import ModelConfigService

    # 早期 fail-fast：communityKey 不存在直接 404，避免后面空列表走到 LLM
    # 又返「无可建议」造成混淆。
    community = (
        await db.execute(
            select(KnowledgeCommunity).where(
                KnowledgeCommunity.community_key == communityKey
            )
        )
    ).scalar_one_or_none()
    if community is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="社区不存在"
        )

    activeModels = await ModelConfigService().list(db, activeOnly=True)
    if not activeModels:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="系统未配置任何可用 LLM 模型",
        )
    invoker = LearningLLMInvoker(db, primaryModelId=activeModels[0].id)
    try:
        await invoker.preflight()
    except LLMUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    suggester = CommunityTopicSuggester(invoker)
    suggestion = await suggester.suggest(db, communityKey)

    # 把社区实际加载到的标题列表也回给前端 —— Modal 展示「这是基于以下
    # 条目得出的主题」让用户判断可信度。
    titles = await loadCommunityTitles(db, communityKey)
    return WikiCommunityTopicSuggestRead(
        topic=suggestion.topic,
        page_count=suggestion.pageCount,
        page_titles=titles,
    )


@router.patch(
    "/communities/{communityKey}",
    response_model=WikiCommunityTopicUpdateRead,
    status_code=status.HTTP_200_OK,
)
async def updateCommunityTopic(
    communityKey: str,
    dto: WikiCommunityUpdateRequest,
    db: AsyncSession = Depends(getDb),
) -> WikiCommunityTopicUpdateRead:
    """Phase 5.5：人工确认 LLM 主题建议（或直接改名）后的落库动作。

    ``topic`` 显式 null = 清空（恢复成「未命名」状态）。
    ``topic`` 不传 = 不变更（避免误触覆盖）。
    """
    from fastapi import HTTPException

    community = (
        await db.execute(
            select(KnowledgeCommunity).where(
                KnowledgeCommunity.community_key == communityKey
            )
        )
    ).scalar_one_or_none()
    if community is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="社区不存在"
        )

    # 区分「未传该字段」与「传 null」：Pydantic v2 默认两者都给 None，
    # 要看 model_fields_set —— 没传则不触库，传了 null = 清空。
    if "topic" in dto.model_fields_set:
        community.topic = dto.topic  # 允许 None（清空）或字符串（改名）
        await db.commit()
        await db.refresh(community)

    return WikiCommunityTopicUpdateRead(
        community=KnowledgeCommunityRead.model_validate(community)
    )


@router.get(
    "/view",
    response_model=GraphViewRead,
    status_code=status.HTTP_200_OK,
)
async def getGraphView(
    includeIsolated: bool = Query(default=False),
    db: AsyncSession = Depends(getDb),
) -> GraphViewRead:
    """图可视化载荷。默认只返回有边节点（孤立页在覆盖度视图看更合适）。

    边得分 = 4-Signal 相关性（前端映射边宽/透明度）。节点按度数降序截断
    到 ``_MAX_GRAPH_NODES``，``truncated=True`` 提示前端展示「已截断」。
    """
    data = await loadKnowledgeGraph(db)
    graph = data.graph

    nodeIds = [
        pid
        for pid in graph.nodes
        if includeIsolated or graph.degree(pid) > 0
    ]
    nodeIds.sort(key=lambda pid: graph.degree(pid), reverse=True)
    truncated = len(nodeIds) > _MAX_GRAPH_NODES
    nodeIds = nodeIds[:_MAX_GRAPH_NODES]
    nodeSet = set(nodeIds)

    # 社区归属：批量查成员表一次，O(1) 回填（逐节点查库会放大成 N 次 IO）
    communityByPage: dict[str, str] = {}
    if nodeIds:
        memberRows = (
            await db.execute(
                select(
                    KnowledgeCommunityMember.page_id,
                    KnowledgeCommunity.community_key,
                ).join(
                    KnowledgeCommunity,
                    KnowledgeCommunity.id == KnowledgeCommunityMember.community_id,
                ).where(KnowledgeCommunityMember.page_id.in_(nodeIds))
            )
        ).all()
        communityByPage = {pageId: key for pageId, key in memberRows}

    nodes: list[GraphNodeRead] = [
        GraphNodeRead(
            page_id=pid,
            title=graph.nodes[pid].get(NODE_TITLE),
            dimension=graph.nodes[pid].get(NODE_DIMENSION),
            degree=graph.degree(pid),
            community_key=communityByPage.get(pid),
        )
        for pid in nodeIds
    ]

    edges: list[GraphEdgeRead] = []
    for source, target, edgeData in graph.edges(data=True):
        if source in nodeSet and target in nodeSet:
            score = computeRelevance(data, source, target)
            edges.append(
                GraphEdgeRead(
                    source=source,
                    target=target,
                    relation_type=edgeData.get("relationType", ""),
                    score=round(score.total, 3),
                )
            )

    return GraphViewRead(nodes=nodes, edges=edges, truncated=truncated)


# ---------------------------------------------------------------------------
# Insights（Phase 3）
# ---------------------------------------------------------------------------


@router.get(
    "/insights",
    response_model=GraphInsightsRead,
    status_code=status.HTTP_200_OK,
)
async def getGraphInsights(db: AsyncSession = Depends(getDb)) -> GraphInsightsRead:
    """读最新一次扫描的 Graph Insights（拓扑 + LLM 解读）。

    若从未扫描过，三类列表均为空 —— 前端用空态引导「重算扫描」。
    不自动跑扫描：扫描涉及 LLM 调用，必须由用户显式触发（POST /insights/rescan）。
    """
    from sqlalchemy import select

    from app.domain.wiki_graph_insight import WikiGraphInsight

    rows = (
        await db.execute(
            select(WikiGraphInsight).order_by(WikiGraphInsight.updated_time.desc())
        )
    ).scalars().all()

    surprising: list[SurprisingConnectionRead] = []
    gaps: list[KnowledgeGapRead] = []
    bridges: list[BridgeNodeRead] = []
    latestUpdate = None
    for row in rows:
        if latestUpdate is None or row.updated_time > latestUpdate:
            latestUpdate = row.updated_time
        if row.kind == "SURPRISING_CONNECTION":
            payload = row.payload or {}
            surprising.append(
                SurprisingConnectionRead(
                    key=row.key,
                    headline=row.headline,
                    source_page_id=payload.get("sourcePageId", ""),
                    source_title=payload.get("sourceTitle", ""),
                    source_community=payload.get("sourceCommunity"),
                    source_dimension=payload.get("sourceDimension"),
                    target_page_id=payload.get("targetPageId", ""),
                    target_title=payload.get("targetTitle", ""),
                    target_community=payload.get("targetCommunity"),
                    target_dimension=payload.get("targetDimension"),
                    relation_type=payload.get("relationType", ""),
                    explanation=row.explanation,
                )
            )
        elif row.kind.startswith("KNOWLEDGE_GAP_"):
            payload = row.payload or {}
            gaps.append(
                KnowledgeGapRead(
                    key=row.key,
                    kind=payload.get("kind", row.kind.removeprefix("KNOWLEDGE_GAP_")),
                    headline=row.headline,
                    explanation=row.explanation,
                    community_key=payload.get("communityKey"),
                    page_count=payload.get("pageCount"),
                    cohesion_score=payload.get("cohesionScore"),
                    page_id=payload.get("pageId"),
                    title=None,
                    degree=payload.get("degree"),
                )
            )
        elif row.kind == "BRIDGE_NODE":
            payload = row.payload or {}
            bridges.append(
                BridgeNodeRead(
                    key=row.key,
                    page_id=payload.get("pageId", ""),
                    title=payload.get("title", ""),
                    degree=payload.get("degree", 0),
                    communities=payload.get("communities", []),
                    explanation=row.explanation,
                )
            )

    scannedAt = latestUpdate or datetime.utcnow()
    return GraphInsightsRead(
        surprising_connections=surprising,
        knowledge_gaps=gaps,
        bridge_nodes=bridges,
        scanned_at=scannedAt,
    )


@router.post(
    "/insights/rescan",
    response_model=GraphInsightsRescanRead,
    status_code=status.HTTP_200_OK,
)
@limiter.limit(rateLimitValue)
async def rescanGraphInsights(
    request: Request,
    db: AsyncSession = Depends(getDb),
) -> GraphInsightsRescanRead:
    """全量重算 Graph Insights（拓扑 + LLM 解读 + 落库）。

    与 /communities/recompute 一样限流：含 LLM 调用且扫全表，不应被连点。
    解读按 (kind, key) 缓存：相同拓扑重跑读 cache 不调 LLM。
    """
    from app.services.learning.insight_service import rescanGraphInsights as _rescan

    # 模型选择：复用导入向导的默认模型（同 claim_extractor 走 LearningLLMInvoker
    # 即可，invoker 内置主备模型 + 重试）。没有可用模型时 invoker 仍可构造，
    # 调用时若失败按 SKIPPED 处理 —— 这里允许 invoker=None 跳过 LLM，
    # 仅跑拓扑扫描 + 落库空解读（前端可见「尚未生成解读」）。
    from app.services.learning.llm_invoker import LearningLLMInvoker

    try:
        invoker = LearningLLMInvoker(db)
        # LearningLLMInvoker 构造时会校验模型；失败抛错，跳过 LLM
        modelId = invoker.primaryModelId or 0
    except Exception:
        invoker = None
        modelId = 0

    result = await _rescan(db, modelId=modelId, invoker=invoker)
    await db.commit()
    return GraphInsightsRescanRead(
        surprising_count=len(result.surprisingConnections),
        gap_count=len(result.knowledgeGaps),
        bridge_count=len(result.bridgeNodes),
        explanation_attempts=result.explanationAttempts,
        explanation_failures=result.explanationFailures,
    )
