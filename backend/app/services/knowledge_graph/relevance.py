"""4-Signal 相关性模型（迁移自 llm_wiki-main，适配 qa-system 数据模型）。

总公式（与 llm_wiki-main 同权重，信号语义已映射到 qa-system 概念）：

    relevance = 显式关系×3 + 共享证据源×4 + Adamic-Adar×1.5 + 同维度亲和×1

信号语义映射：
- 显式关系（direct link）：两页间存在已确认 KnowledgeRelation —— 人工/审核
  背书的连接，权重高。
- 共享证据源（source overlap）：两页的 claim 引用了同一个
  ``Evidence.source_id``（同一份外部文档/工单/邮件）—— 同源是「没被任何人
  显式连接但客观上相关」的最强信号，所以权重最高（×4）。
- Adamic-Adar：在已确认关系图上的共同邻居指数 —— 拓扑相似性，权重中。
- 同维度亲和（type affinity）：两页同属一个知识维度（OBJECT/RULE/...），
  弱信号，只作微调。

模块为纯函数：输入图快照 + 两个 page_id，输出各信号分量与总分。
不触库，便于单测与复用（前端 viz 边打分、后续推荐排序共用）。
"""
from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from app.services.knowledge_graph.graph_data import NODE_DIMENSION, KnowledgeGraphData

DIRECT_LINK_WEIGHT = 3.0
SOURCE_OVERLAP_WEIGHT = 4.0
ADAMIC_ADAR_WEIGHT = 1.5
TYPE_AFFINITY_WEIGHT = 1.0


@dataclass(frozen=True)
class RelevanceScore:
    """两页相关性的信号分解。``total`` 为加权和，未归一化（保序即可）。"""

    total: float
    directLink: bool
    sourceOverlap: bool
    adamicAdar: float
    typeAffinity: bool


def computeRelevance(data: KnowledgeGraphData, pageA: str, pageB: str) -> RelevanceScore:
    """计算两页相关性。不存在的节点按「无任何信号」处理（得分 0），不抛错。"""
    graph = data.graph
    if pageA not in graph or pageB not in graph or pageA == pageB:
        return RelevanceScore(0.0, False, False, 0.0, False)

    directLink = graph.has_edge(pageA, pageB)
    sourceOverlap = bool(
        data.sourceIds.get(pageA, frozenset()) & data.sourceIds.get(pageB, frozenset())
    )
    adamicAdar = _adamicAdar(graph, pageA, pageB)
    typeAffinity = (
        graph.nodes[pageA].get(NODE_DIMENSION) is not None
        and graph.nodes[pageA].get(NODE_DIMENSION)
        == graph.nodes[pageB].get(NODE_DIMENSION)
    )

    total = (
        DIRECT_LINK_WEIGHT * directLink
        + SOURCE_OVERLAP_WEIGHT * sourceOverlap
        + ADAMIC_ADAR_WEIGHT * adamicAdar
        + TYPE_AFFINITY_WEIGHT * typeAffinity
    )
    return RelevanceScore(total, directLink, sourceOverlap, adamicAdar, typeAffinity)


def _adamicAdar(graph: nx.Graph, pageA: str, pageB: str) -> float:
    """单对节点的 Adamic-Adar 指数。无共同邻居时 networkx 返回 0。"""
    scores = nx.adamic_adar_index(graph, [(pageA, pageB)])
    return next(score for _, _, score in scores)
