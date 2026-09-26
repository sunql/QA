"""向量相似度换算单源（H6）。

Milvus 用 L2 距离（越小越相似），前端展示需要「相似度」。本模块是全库唯一的
换算口径：`score = 1 / (1 + distance)` 把 [0, ∞) 映射到 (0, 1]；非有限输入
（NaN / ±inf）按最不相似处理（0.0），不穿透值域。

此前该公式在 embedding_service / ontology_service / rag_service /
wiki_vector_service 四处各写一遍且口径不同（仅 rag 无 max(0) ⇒ 负距离得 > 1；
仅 wiki_vector 无 round ⇒ 同一向量在不同页面精度不同），故收敛到此处。
"""

from __future__ import annotations

import math


def distanceToSimilarity(distance: float) -> float:
    """Milvus L2 距离 → [0,1] 相似度（距离越小越相似；负数按 0 处理防除零）。"""
    value = float(distance)
    if not math.isfinite(value):
        # NaN / ±inf 不是「距离」，落到值域外会破坏调用方的展示与排序：
        #   NaN  → max(nan, 0.0) 返回 nan ⇒ score 是 NaN，前端渲染 NaN%，
        #          且含 NaN 的排序顺序不确定（可能把不相关文档排到前排）；
        #   -inf → 会算成 1.0，即把"无限远"当成"完全相同"（假完美命中）。
        # 按「最不相似」处理，守住 [0,1] 契约（不猜一个更好的值，也不抛错中断检索）。
        return 0.0
    return round(1.0 / (1.0 + max(value, 0.0)), 4)
