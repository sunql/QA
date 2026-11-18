"""Milvus 向量集合名的唯一来源（SSOT）。

为什么单独一个模块：写入锁按**集合名**取键
（``app.infrastructure.milvus_write_lock.milvusWriteLock``），所以「type → 集合名」
的映射必须**只有一处**定义 —— 一旦这里和 ``milvus_dual_write`` /
``milvus_query_helpers`` / ``milvus_search`` / ``milvus_client`` 各写一份字面量，
改名时就会漏改一处 ⇒ **锁会锁错集合**（写侧不再互斥 ⇒ 撞 Milvus 的集合级 flush
配额）或**读侧查错集合**（向量「写成功但召回不到」）
（2026-11-17 生产事故，见
``Harness/changes/2026-11-17-fix-milvus-flush-quota/summary.md``）。

本模块**不 import 任何 milvus 模块** ⇒ 无循环依赖（``milvus_client`` 与
``milvus_dual_write`` 互相 import，只能有一个叶子模块承载这份映射）。
"""

from __future__ import annotations


CLASS_COLLECTION_NAME = "ontology_class_embeddings"
PROPERTY_COLLECTION_NAME = "ontology_property_embeddings"
METRIC_COLLECTION_NAME = "ontology_metric_embeddings"
KPI_COLLECTION_NAME = "kpi_catalog_embeddings"


_COLLECTION_BY_TYPE: dict[str, str] = {
    "class": CLASS_COLLECTION_NAME,
    "property": PROPERTY_COLLECTION_NAME,
    "metric": METRIC_COLLECTION_NAME,
    "kpi": KPI_COLLECTION_NAME,
}


def embeddingCollectionForType(type_: str) -> str:
    """type（class/property/metric/kpi）→ 集合名。

    写入锁的键用它 ⇒ 未知 type 直接抛（宁可失败，也不要把锁加到错误的集合上）。

    Raises:
        ValueError: type 不在 class/property/metric/kpi 之内。
    """
    collection = _COLLECTION_BY_TYPE.get(type_)
    if collection is None:
        raise ValueError(f"unknown embedding type: {type_!r}")
    return collection
