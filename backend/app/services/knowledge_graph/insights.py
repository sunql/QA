"""Graph Insights 拓扑计算（Phase 3.1）。

三类洞察（与 llm_wiki-main Graph Insights 对齐，适配 qa-system 数据模型）：

- 意外连接（Surprising Connections）：已确认关系中两端的社区归属
  不同（A∈C1, B∈C2）—— 同图布局下「本不该连却连上了」的桥。
  进一步按维度交叉做：RULE × PROCESS 这种跨维度边也是「有意思的连接」。
  LLM 解读单独走，不在本模块。

- 知识缺口（Knowledge Gaps）：
  - 孤立页：度 ≤ 1（无邻居或仅 1 条边，**没真正进入**任何知识域）
  - 稀疏社区：社区内聚度 < 0.15（成员很多但彼此几乎不连）
  - 无维度页：dimension IS NULL（机制 1 没分出来的知识）

- 桥接节点（Bridge Nodes）：某页连出的边跨 3+ 个不同社区（去重后）——
  它是把分散知识域缝在一起的关键节点。llm_wiki-main 的阈值是 3，
  这里直接沿用。

拓扑计算全部基于图快照（``KnowledgeGraphData`` + 社区归属），不触库，
便于单测。LLM 解读在 ``learning.insight`` 中做（Phase 3.2）。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.knowledge_graph.community import CommunityResult
from app.services.knowledge_graph.graph_data import NODE_DIMENSION, KnowledgeGraphData

# 度数 ≤ 此值视为「未真正进入知识域」。0 = 绝对孤立；1 = 只挂了一条边且
# 没有下游，实质还是孤岛。
_ISOLATED_DEGREE = 1

# 社区内聚度阈值。cohesion = 内边 / 可能边（社区规模 n 时最多 n*(n-1)/2），
# 0.15 = 5 人社区只有 1 条内连，表示成员没有真正形成群组。
_SPARSE_COHESION_THRESHOLD = 0.15

# 桥接节点的「连接社区数」阈值。
_BRIDGE_COMMUNITY_THRESHOLD = 3


@dataclass(frozen=True)
class SurprisingConnection:
    """一条意外连接：两端社区（或维度）不同的已确认边。"""

    sourcePageId: str
    sourceTitle: str
    sourceCommunity: str | None
    sourceDimension: str | None
    targetPageId: str
    targetTitle: str
    targetCommunity: str | None
    targetDimension: str | None
    relationType: str

    def headline(self) -> str:
        """给人一句话理解的标题（API / 前端直接展示）。"""
        sComm = self.sourceCommunity or "孤立"
        tComm = self.targetCommunity or "孤立"
        if sComm != tComm:
            return f"{sComm} ↔ {tComm}：{self.sourceTitle} 与 {self.targetTitle}"
        return (
            f"跨维度 {self.sourceDimension}/{self.targetDimension}："
            f"{self.sourceTitle} ↔ {self.targetTitle}"
        )


@dataclass(frozen=True)
class KnowledgeGap:
    """知识缺口，按 kind 区分。"""

    kind: str  # ISOLATED_PAGE | SPARSE_COMMUNITY | MISSING_DIMENSION
    pageId: str | None = None
    title: str | None = None
    communityKey: str | None = None
    communityName: str | None = None
    pageCount: int | None = None
    cohesionScore: float | None = None
    degree: int | None = None

    def headline(self) -> str:
        if self.kind == "ISOLATED_PAGE":
            return f"孤立：{self.title}（度={self.degree}）"
        if self.kind == "MISSING_DIMENSION":
            return f"无维度：{self.title}"
        return f"稀疏社区 {self.communityKey}：{self.pageCount} 条 / 内聚度 {self.cohesionScore}"


@dataclass(frozen=True)
class BridgeNode:
    """桥接节点。``communities`` 是去重后的社区 key 列表。"""

    pageId: str
    title: str
    degree: int
    communities: tuple[str, ...]


@dataclass(frozen=True)
class GraphInsights:
    """一次拓扑扫描的全部结果。"""

    surprisingConnections: tuple[SurprisingConnection, ...]
    knowledgeGaps: tuple[KnowledgeGap, ...]
    bridgeNodes: tuple[BridgeNode, ...]

    @property
    def empty(self) -> bool:
        return (
            not self.surprisingConnections
            and not self.knowledgeGaps
            and not self.bridgeNodes
        )


def _pageCommunity(
    communityByPage: dict[str, str], pageId: str
) -> str | None:
    return communityByPage.get(pageId)


def computeSurprisingConnections(
    data: KnowledgeGraphData,
    communityByPage: dict[str, str],
    *,
    limit: int = 50,
) -> tuple[SurprisingConnection, ...]:
    """扫描已确认边，找两端「社区不同」或「维度不同」的连接。

    同一边先判社区归属，再判维度 —— 跨社区是更强的「意外」（同一个
    知识域内本该自然相连），跨维度次之（属于同社区但跨越业务面）。
    """
    graph = data.graph
    out: list[SurprisingConnection] = []
    for source, target, edgeData in graph.edges(data=True):
        sourceComm = _pageCommunity(communityByPage, source)
        targetComm = _pageCommunity(communityByPage, target)
        sourceDim = graph.nodes[source].get(NODE_DIMENSION)
        targetDim = graph.nodes[target].get(NODE_DIMENSION)

        commDiffers = (sourceComm != targetComm) and (sourceComm and targetComm)
        dimDiffers = (
            sourceDim is not None
            and targetDim is not None
            and sourceDim != targetDim
        )
        if not commDiffers and not dimDiffers:
            continue

        out.append(
            SurprisingConnection(
                sourcePageId=source,
                sourceTitle=graph.nodes[source].get("title") or source,
                sourceCommunity=sourceComm,
                sourceDimension=sourceDim,
                targetPageId=target,
                targetTitle=graph.nodes[target].get("title") or target,
                targetCommunity=targetComm,
                targetDimension=targetDim,
                relationType=edgeData.get("relationType", ""),
            )
        )
        if len(out) >= limit:
            break
    return tuple(out)


def computeKnowledgeGaps(
    data: KnowledgeGraphData,
    communityByPage: dict[str, str],
    communities: list[CommunityResult],
) -> tuple[KnowledgeGap, ...]:
    """扫描三类缺口：孤立页 / 稀疏社区 / 无维度页。"""
    gaps: list[KnowledgeGap] = []
    graph = data.graph

    for pageId in graph.nodes:
        degree = graph.degree(pageId)
        title = graph.nodes[pageId].get("title") or pageId
        dimension = graph.nodes[pageId].get(NODE_DIMENSION)
        if degree <= _ISOLATED_DEGREE:
            gaps.append(
                KnowledgeGap(
                    kind="ISOLATED_PAGE",
                    pageId=pageId,
                    title=title,
                    degree=degree,
                )
            )
        if dimension is None:
            gaps.append(
                KnowledgeGap(
                    kind="MISSING_DIMENSION",
                    pageId=pageId,
                    title=title,
                )
            )

    for community in communities:
        if (
            community.pageIds
            and len(community.pageIds) >= 3
            and community.cohesionScore < _SPARSE_COHESION_THRESHOLD
        ):
            gaps.append(
                KnowledgeGap(
                    kind="SPARSE_COMMUNITY",
                    communityKey=community.communityKey,
                    communityName=community.name,
                    pageCount=len(community.pageIds),
                    cohesionScore=community.cohesionScore,
                )
            )
    return tuple(gaps)


def computeBridgeNodes(
    data: KnowledgeGraphData,
    communityByPage: dict[str, str],
    *,
    limit: int = 20,
) -> tuple[BridgeNode, ...]:
    """某页连接 3+ 个不同社区（去重后）。所有邻居都在同一社区的页不算桥。"""
    graph = data.graph
    candidates: list[BridgeNode] = []
    for pageId in graph.nodes:
        neighborCommunities = {
            communityByPage.get(neighbor)
            for neighbor in graph.neighbors(pageId)
        }
        neighborCommunities.discard(None)
        if len(neighborCommunities) >= _BRIDGE_COMMUNITY_THRESHOLD:
            candidates.append(
                BridgeNode(
                    pageId=pageId,
                    title=graph.nodes[pageId].get("title") or pageId,
                    degree=graph.degree(pageId),
                    communities=tuple(sorted(neighborCommunities)),
                )
            )
    candidates.sort(key=lambda n: (len(n.communities), n.degree), reverse=True)
    return tuple(candidates[:limit])


def scanGraph(
    data: KnowledgeGraphData,
    communities: list[CommunityResult],
) -> GraphInsights:
    """一次扫描：拓扑计算 + 社区归属投影。无 LLM 调用。"""
    communityByPage: dict[str, str] = {
        pid: c.communityKey
        for c in communities
        for pid in c.pageIds
    }
    return GraphInsights(
        surprisingConnections=computeSurprisingConnections(data, communityByPage),
        knowledgeGaps=computeKnowledgeGaps(data, communityByPage, communities),
        bridgeNodes=computeBridgeNodes(data, communityByPage),
    )
