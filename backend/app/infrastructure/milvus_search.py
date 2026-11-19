"""Milvus search for type-routed 4-collection embeddings.

M0-P0.4 Phase 2 (Task 15): Extracted from milvus_client.py.

Provides:
- searchEmbeddingsByTypeRouted / _searchCollection
"""

from __future__ import annotations

from typing import Any, Sequence

from pymilvus import Collection

from app.infrastructure.milvus_client import (
    VALID_EMBEDDING_TYPES,
    _connAlias,
    _connect,
    buildClassIdScopeExpr,
    hasEmbeddingCollection,
    hnswSearchParams,
)


# ---------------------------------------------------------------------------
# Collection name constants — 唯一来源 = milvus_collections.py（SSOT）。
# 不要在这里另写一份字面量：写锁按集合名取键（milvus_write_lock），读侧与写侧
# 集合名一旦漂移 ⇒ 写进 A 集合、查 B 集合 ⇒ 向量「写成功但召回不到」。
# ---------------------------------------------------------------------------
from app.infrastructure.milvus_collections import (
    CLASS_COLLECTION_NAME as _CLASS_COLLECTION_NAME,
)
from app.infrastructure.milvus_collections import (
    KPI_COLLECTION_NAME as _KPI_COLLECTION_NAME,
)
from app.infrastructure.milvus_collections import (
    METRIC_COLLECTION_NAME as _METRIC_COLLECTION_NAME,
)
from app.infrastructure.milvus_collections import (
    PROPERTY_COLLECTION_NAME as _PROPERTY_COLLECTION_NAME,
)

# 去重超取倍数：Milvus 的 limit 按「实体」而非「不同 ontology_id」计数，集合里
# 一旦残留同一 ontology_id 的多份向量（delete-then-insert 在批量负载下不可靠，
# 见 scripts/backfill_milvus_embeddings.py --cleanup 的背景），topK 窗口就会
# 被重复项吃光——2026-09-30 线上召回事故：15 个名额被 4 个不同的类占满，
# 业务需要的类进不了候选集。故按 _DEDUP_OVERFETCH 倍超取再按 ontology_id 去重。
# 8 是经验值：历史最坏观测为每类 4 份，留一倍余量；代价是 Milvus 多算几倍邻居。
_DEDUP_OVERFETCH = 8


# ---------------------------------------------------------------------------
# Public search API
# ---------------------------------------------------------------------------

def searchEmbeddingsByTypeRouted(
    queryEmbedding: list[float],
    topK: int = 5,
    typeFilter: str | None = None,
    classOntologyIds: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """向量相似度搜索（type-routed；走 4 个 type-specific collection）。

    Args:
        queryEmbedding: 查询向量
        topK: 返回条数
        typeFilter: 可选，限定类型（class/property/metric/kpi）
        classOntologyIds: 可选，**本体类 id 作用域**（Gap A）：只保留这些本体类的
            向量。``None`` = 不限（与今天逐字节一致）；空集 = deny-all（恒空）。
            **只作用于 class 集合**——Milvus 的 ``ontology_id`` 在类/属性之间
            共用 id 序列，套到 property 集合上会误伤。

    Returns:
        匹配的记录列表（含 ontology_id / type / name / alias / description / distance），
        按 distance 升序。

    Notes:
        - typeFilter is None → 跨 4 collection 各取 topK，合并排序取 topK
        - typeFilter == "class" → 只查 ontology_class_embeddings
        - typeFilter == "property" → 只查 ontology_property_embeddings
        - typeFilter == "metric" → 只查 ontology_metric_embeddings
        - typeFilter == "kpi" → 只查 kpi_catalog_embeddings

    可选集合（kpi_catalog_embeddings）：集合不存在（第一次 KPI 写入之前）
    等价于「还没有 KPI 向量」⇒ 显式查 kpi 返回空、None 扇出跳过该集合，
    都不抛。**既有三集合的严格语义不变**：缺失仍由 ``Collection()`` 抛
    ``SchemaNotReadyException``。
    """
    if typeFilter is not None and typeFilter not in VALID_EMBEDDING_TYPES:
        raise ValueError(f"unknown embedding type filter: {typeFilter!r}")

    # 自建连接：pymilvus 的 ORM 接口不认未注册的连接别名，而检索是进程里最先碰
    # Milvus 的路径之一。把建连留给「别的调用方碰巧先跑过」的代价是：容器/进程
    # 重建后**首次**检索必失败（ConnectionNotExistException → chat 召回落
    # fallback →「数据表智能召回暂不可用」），后续又正常，间歇症状极易被放过。
    # 每个调用只连一次（typeFilter=None 时下面要扫 4 个集合，逐集合建连等于 4 遍
    # 健康检查）。不写成 `if not has_connection(...)`：别名在而连接已死的场景
    # 仍须经 _connect() 的失效重连路径。
    _connect()

    # 本体类 id 作用域 → Milvus expr（None = 不限；空集 = deny-all）。
    classScopeExpr = buildClassIdScopeExpr(classOntologyIds)

    if typeFilter == "class":
        return _searchCollection(
            _CLASS_COLLECTION_NAME, queryEmbedding, topK, expr=classScopeExpr
        )
    if typeFilter == "property":
        return _searchCollection(_PROPERTY_COLLECTION_NAME, queryEmbedding, topK)
    if typeFilter == "metric":
        return _searchCollection(_METRIC_COLLECTION_NAME, queryEmbedding, topK)
    if typeFilter == "kpi":
        # 可选集合：KPI 尚未写入 ⇒ 集合不存在 ⇒ 「还没有 KPI 向量」，返回空而非抛
        if not hasEmbeddingCollection(_KPI_COLLECTION_NAME):
            return []
        return _searchCollection(_KPI_COLLECTION_NAME, queryEmbedding, topK)

    # typeFilter is None: search all collections, merge, sort by distance, take topK
    perCollectionTopK = topK
    perHits: list[dict[str, Any]] = []
    # (集合名, 类型)：作用域只落 class 集合（id 序列按类型共享，见类注释）
    collectionTypes: list[tuple[str, str]] = [
        (_CLASS_COLLECTION_NAME, "class"),
        (_PROPERTY_COLLECTION_NAME, "property"),
        (_METRIC_COLLECTION_NAME, "metric"),
    ]
    # KPI 是可选集合：不存在就跳过（其余三集合缺失仍照旧抛，语义不变）
    if hasEmbeddingCollection(_KPI_COLLECTION_NAME):
        collectionTypes.append((_KPI_COLLECTION_NAME, "kpi"))

    for collectionName, type_ in collectionTypes:
        perHits.extend(_searchCollection(
            collectionName, queryEmbedding, perCollectionTopK,
            expr=classScopeExpr if type_ == "class" else None,
        ))

    perHits.sort(key=lambda h: h["distance"])
    return perHits[:topK]


def _searchCollection(
    collectionName: str,
    queryEmbedding: list[float],
    topK: int,
    expr: str | None = None,
) -> list[dict[str, Any]]:
    """Search one specific collection; returns hits with keys ontology_id/type/name/alias/description/distance.

    ``expr``：可选的服务端过滤表达式（Gap A 的本体类 id 作用域）。``None`` = 不过滤。

    结果按距离升序、按 ``ontology_id`` 去重（保留最近的一份），最后截断到 topK
    —— Milvus 的 ``limit`` 数的是实体，重复实体不该占调用方的 topK 名额
    （见 ``_DEDUP_OVERFETCH`` 的说明）。
    """
    collection = Collection(collectionName, using=_connAlias())
    collection.load()
    results = collection.search(
        data=[queryEmbedding],
        anns_field="embedding",
        param=hnswSearchParams(topK * _DEDUP_OVERFETCH),
        limit=topK * _DEDUP_OVERFETCH,
        output_fields=["ontology_id", "type", "name", "alias", "description"],
        expr=expr,
    )
    hits: list[dict[str, Any]] = []
    seenIds: set[Any] = set()
    for result in results:
        for hit in result:
            row = {
                "ontology_id": hit.entity.get("ontology_id"),
                "type": hit.entity.get("type"),
                "name": hit.entity.get("name"),
                "alias": hit.entity.get("alias"),
                "description": hit.entity.get("description"),
                "distance": float(hit.distance),
            }
            if row["ontology_id"] in seenIds:
                continue  # 同一实体只保留最近的一份（结果已按距离升序）
            seenIds.add(row["ontology_id"])
            hits.append(row)
            if len(hits) >= topK:
                return hits
    return hits
