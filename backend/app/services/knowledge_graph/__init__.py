"""知识图谱分析（Phase 2）：4-Signal 相关性 + Louvain 社区检测。

- ``graph_data``：从 PG 装配「已确认关系」图快照
- ``relevance``：4-Signal 相关性纯函数
- ``community``：Louvain 社区检测与全量替换落库
"""

from app.services.knowledge_graph.community import (
    CommunityResult,
    communityOfPage,
    detectCommunities,
    listCommunities,
    recomputeCommunities,
)
from app.services.knowledge_graph.graph_data import (
    KnowledgeGraphData,
    loadKnowledgeGraph,
)
from app.services.knowledge_graph.insights import (
    BridgeNode,
    GraphInsights,
    KnowledgeGap,
    SurprisingConnection,
    scanGraph,
)
from app.services.knowledge_graph.relevance import (
    RelevanceScore,
    computeRelevance,
)

__all__ = [
    "BridgeNode",
    "CommunityResult",
    "GraphInsights",
    "KnowledgeGap",
    "KnowledgeGraphData",
    "RelevanceScore",
    "SurprisingConnection",
    "communityOfPage",
    "computeRelevance",
    "detectCommunities",
    "listCommunities",
    "loadKnowledgeGraph",
    "recomputeCommunities",
    "scanGraph",
]
