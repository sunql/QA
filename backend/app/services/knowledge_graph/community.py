"""Louvain 社区检测与结果持久化（Phase 2.2）。

检测在「已确认关系图」快照上进行（见 ``graph_data.loadKnowledgeGraph``），
使用 python-louvain（``community_louvain.best_partition``）。

持久化语义：**全量替换**。每次重算删除全部旧社区（成员随 DB 级联清理），
写入新社区，同事务提交由调用方负责。Louvain 社区编号跨次运行不稳定，
跨次对齐不做（YAGNI）。

孤立页（度=0，不属于任何有边社区）也参与分区 —— Louvain 会把每个孤立
点各划为一个单点社区，这些社区无业务意义，落库时**过滤掉单点社区**，
否则前端社区列表会被噪声淹没。
"""
from __future__ import annotations

from dataclasses import dataclass

import community as community_louvain
import networkx as nx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import (
    KnowledgeCommunity,
    KnowledgeCommunityMember,
)
from app.services.knowledge_graph.graph_data import (
    NODE_TITLE,
    KnowledgeGraphData,
)

# top_pages 取每个社区度数最高的前 N 页
_TOP_PAGES_PER_COMMUNITY = 5


@dataclass(frozen=True)
class CommunityResult:
    """一次检测出的单个社区（内存形态，未落库）。"""

    communityKey: str
    name: str
    cohesionScore: float
    pageIds: tuple[str, ...]
    topPages: tuple[dict, ...]


def detectCommunities(data: KnowledgeGraphData) -> list[CommunityResult]:
    """Louvain 分区 → 社区列表（按规模降序）。单点社区已过滤。"""
    graph = data.graph
    if graph.number_of_nodes() == 0:
        return []
    # seed 固定：同一图两次重算结果一致，避免「什么都没改社区却变了」的困扰
    partition: dict[str, int] = community_louvain.best_partition(graph, random_state=42)

    groups: dict[int, list[str]] = {}
    for pageId, communityIdx in partition.items():
        groups.setdefault(communityIdx, []).append(pageId)

    results: list[CommunityResult] = []
    # 规模降序，大社区拿小编号（C001 最大），前端展示顺序稳定
    ordered = sorted(groups.values(), key=len, reverse=True)
    seq = 0
    for members in ordered:
        if len(members) < 2:
            continue  # 单点社区 = 孤立页，噪声
        seq += 1
        results.append(_buildCommunity(data, seq, members))
    return results


def _buildCommunity(
    data: KnowledgeGraphData, seq: int, members: list[str]
) -> CommunityResult:
    graph = data.graph
    subgraph = graph.subgraph(members)
    pageCount = len(members)
    cohesion = _cohesion(subgraph, pageCount)
    topPages = _topPages(graph, members)
    key = f"C{seq:03d}"
    # 社区名取度数最高页的标题 —— 给人一个「这社区讲什么」的直觉锚点
    name = topPages[0]["title"] if topPages else key
    return CommunityResult(
        communityKey=key,
        name=name,
        cohesionScore=cohesion,
        pageIds=tuple(members),
        topPages=tuple(topPages),
    )


def _cohesion(subgraph: nx.Graph, pageCount: int) -> float:
    """内聚度 = 社区内实际边数 / 理论最大边数。2 点社区只有 0 或 1。"""
    maxEdges = pageCount * (pageCount - 1) / 2
    if maxEdges == 0:
        return 0.0
    return round(subgraph.number_of_edges() / maxEdges, 3)


def _topPages(graph: nx.Graph, members: list[str]) -> list[dict]:
    """按图内度数降序取前 N 页，附标题。"""
    byDegree = sorted(members, key=lambda pid: graph.degree(pid), reverse=True)
    return [
        {
            "pageId": pid,
            "title": graph.nodes[pid].get(NODE_TITLE) or pid,
            "degree": graph.degree(pid),
        }
        for pid in byDegree[:_TOP_PAGES_PER_COMMUNITY]
    ]


async def recomputeCommunities(session: AsyncSession, data: KnowledgeGraphData) -> list[CommunityResult]:
    """检测并全量替换落库。调用方负责 commit。

    返回内存形态的检测结果（含 pageIds），便于 API 直接回包而不用重查。
    """
    results = detectCommunities(data)

    # 全量替换：先删（成员 DB 级联），flush 后再插，避免同表残留。
    await session.execute(delete(KnowledgeCommunity))
    await session.flush()

    for result in results:
        community = KnowledgeCommunity(
            community_key=result.communityKey,
            name=result.name,
            cohesion_score=result.cohesionScore,
            page_count=len(result.pageIds),
            top_pages=list(result.topPages),
        )
        session.add(community)
        await session.flush()  # 拿 community.id 供成员外键
        for pageId in result.pageIds:
            session.add(
                KnowledgeCommunityMember(community_id=community.id, page_id=pageId)
            )
    await session.flush()
    return results


async def listCommunities(session: AsyncSession) -> list[KnowledgeCommunity]:
    """读取当前已落库的社区（按规模降序）。"""
    result = await session.execute(
        select(KnowledgeCommunity).order_by(KnowledgeCommunity.page_count.desc())
    )
    return list(result.scalars().all())


async def communityOfPage(session: AsyncSession, pageId: str) -> int | None:
    """查某页当前所属社区 id；不属于任何社区（孤立/未重算）返回 None。"""
    result = await session.execute(
        select(KnowledgeCommunityMember.community_id).where(
            KnowledgeCommunityMember.page_id == pageId
        )
    )
    row = result.first()
    return row[0] if row else None
