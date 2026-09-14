"""知识图谱数据装配：从 PG 装载「已确认关系」图为 networkx 对象。

图的定义（SSOT，与 plan Phase 2 一致）：
- 节点 = WikiPage（排除 EXPIRED：过期知识不应再参与知识发现）
- 边 = KnowledgeRelation 且 confirmed=True、rejected_at IS NULL、
  downstream_type='PAGE' —— 只有 Page↔Page 关系构成知识网络拓扑；
  指向本体类/指标的关系是「知识-业务锚点」，不参与社区检测。
- 未确认候选（autoDetected && !confirmed）刻意不入图：审核机制的意义
  就是挡住低置信度误连，图分析若把候选也算进去，审核就形同虚设。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import Evidence, KnowledgeClaim, KnowledgeRelation, WikiPage

# 图的节点属性 key
NODE_TITLE = "title"
NODE_DIMENSION = "dimension"


@dataclass(frozen=True)
class KnowledgeGraphData:
    """一次性装配的图快照。frozen：装配后不变，相关性/社区计算共享同一快照。"""

    graph: nx.Graph
    # page_id -> 该页所有 claim 的 evidence.source_id 去重集合（信号 2 原料）
    sourceIds: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def pageIds(self) -> list[str]:
        return list(self.graph.nodes)


async def loadKnowledgeGraph(session: AsyncSession) -> KnowledgeGraphData:
    """装配图快照。数据量假设：万级页面 / 十万级关系以内，内存计算足够。"""
    pages = (
        await session.execute(
            select(WikiPage.page_id, WikiPage.title, WikiPage.dimension).where(
                WikiPage.status != "EXPIRED"
            )
        )
    ).all()

    graph = nx.Graph()
    for pageId, title, dimension in pages:
        graph.add_node(pageId, **{NODE_TITLE: title, NODE_DIMENSION: dimension})

    relations = (
        await session.execute(
            select(
                KnowledgeRelation.upstream_page_id,
                KnowledgeRelation.downstream_id,
                KnowledgeRelation.relation_type,
            ).where(
                KnowledgeRelation.confirmed.is_(True),
                KnowledgeRelation.rejected_at.is_(None),
                KnowledgeRelation.downstream_type == "PAGE",
            )
        )
    ).all()
    pageIdSet = set(graph.nodes)
    for upstreamId, downstreamId, relationType in relations:
        # 指向已过期/已删除页的关系边跳过（节点不存在就不该有边）
        if upstreamId in pageIdSet and downstreamId in pageIdSet:
            graph.add_edge(upstreamId, downstreamId, relationType=relationType)

    sourceRows = (
        await session.execute(
            select(KnowledgeClaim.page_id, Evidence.source_id)
            .join(Evidence, Evidence.claim_id == KnowledgeClaim.id)
            .where(Evidence.source_id.isnot(None))
        )
    ).all()
    sourceIds: dict[str, set[str]] = {}
    for pageId, sourceId in sourceRows:
        sourceIds.setdefault(pageId, set()).add(sourceId)

    return KnowledgeGraphData(
        graph=graph,
        sourceIds={pid: frozenset(ids) for pid, ids in sourceIds.items()},
    )
