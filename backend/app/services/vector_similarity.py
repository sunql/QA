"""向量相似度换算单源（H6）。

Milvus 用 L2 距离（越小越相似），前端展示需要「相似度」。本模块是全库唯一的
换算口径：`score = 1 / (1 + distance)` 把 [0, ∞) 映射到 (0, 1]。

此前该公式在 embedding_service / ontology_service / rag_service /
wiki_vector_service 四处各写一遍且口径不同（仅 rag 无 max(0) ⇒ 负距离得 > 1；
仅 wiki_vector 无 round ⇒ 同一向量在不同页面精度不同），故收敛到此处。
"""

from __future__ import annotations


def distanceToSimilarity(distance: float) -> float:
    """Milvus L2 距离 → [0,1] 相似度（距离越小越相似；负数按 0 处理防除零）。"""
    return round(1.0 / (1.0 + max(float(distance), 0.0)), 4)
