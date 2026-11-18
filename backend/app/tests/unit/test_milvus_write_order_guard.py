"""Milvus 写路径的「先删后插 + 两次 flush」顺序守卫（2026-11-17 flush 配额事故）。

Milvus 出厂把**每集合**的 flush 限死在 0.1 qps（每 10 秒 1 次）⇒
「删一次 + 插一次」= 同集合连续 2 次 flush ⇒ 第二次可能被服务端拒绝 ⇒
2026-11-17 生产事故（``Harness/changes/2026-11-17-fix-milvus-flush-quota/summary.md``）。

本文件钉死两件事：

1. **两次 flush 的顺序不能合并**：Milvus 不保证「同一次 flush 里 delete 与
   insert 的先后」⇒ 合并成一次 flush 时，delete 表达式有可能把同一次 flush
   里新插入的那一行一起删掉 ⇒ 向量静默消失（比现在更难查）。本地 Milvus
   （19530）是生产数据面、禁写测试 ⇒ 这种顺序语义无法在本仓库实测 ⇒ 不做
   无法验证的语义改动 ⇒ 用测试把现状钉住。
2. **集合名只有一个来源**：写入锁按集合名取键（``milvus_write_lock``）⇒
   ``milvus_collections`` / ``milvus_dual_write`` / ``milvus_query_helpers``
   三处的集合名必须一致，否则锁会锁错集合。
"""

from __future__ import annotations

import pytest

# ⚠️ 先 import milvus_client：它在**文件末尾** re-export milvus_dual_write 的同名
# 函数；若先 import milvus_dual_write ⇒ 它回头 import milvus_client ⇒ milvus_client
# 末尾又 import 尚未定义完的 milvus_dual_write ⇒ ImportError（见
# milvus_dual_write.py 头部注释与 kpi_vector_graph_sync.py 的同款说明）。
from app.infrastructure import milvus_client as _milvusClient  # noqa: F401
from app.infrastructure import (
    milvus_client,
    milvus_collections,
    milvus_dual_write,
    milvus_query_helpers,
    milvus_search,
)


class _FakeCollection:
    """最小 pymilvus Collection 替身：只记 insert/delete/flush 的**先后**。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def insert(self, data: list) -> None:
        self.calls.append("insert")

    def delete(self, expression: str) -> None:
        self.calls.append(f"delete:{expression}")

    def flush(self) -> None:
        self.calls.append("flush")


@pytest.fixture()
def fakeCollection(monkeypatch: pytest.MonkeyPatch) -> _FakeCollection:
    """把所有 ensure*Collection 换成同一个替身（只关心调用序列，不关心集合名）。"""
    collection = _FakeCollection()
    for name in (
        "ensureClassCollection",
        "ensurePropertyCollection",
        "ensureMetricCollection",
        "ensureKpiCollection",
    ):
        monkeypatch.setattr(milvus_dual_write, name, lambda: collection)
    return collection


def _record(type_: str = "property") -> dict:
    return {
        "ontology_id": 7707,
        "type": type_,
        "name": "creator_name",
        "alias": None,
        "description": None,
        "embedding": [0.1, 0.2],
    }


def test_delete_flushes_once(fakeCollection: _FakeCollection) -> None:
    """删除自带一次 flush（delete 的可见性靠它）。"""
    milvus_dual_write.deleteByOntologyIdDual(7707, "property")

    assert fakeCollection.calls == ["delete:ontology_id == 7707", "flush"]


def test_insert_flushes_once(fakeCollection: _FakeCollection) -> None:
    """插入自带一次 flush。"""
    milvus_dual_write.insertEmbeddingsDual([_record()])

    assert fakeCollection.calls == ["insert", "flush"]


def test_delete_then_insert_keeps_two_ordered_flushes(
    fakeCollection: _FakeCollection,
) -> None:
    """写路径 = 「删+flush」然后「插+flush」：**顺序不能被合并**。

    合并成一次 flush 时，Milvus 不保证 delete 表达式与新插入行的先后 ⇒
    可能把新行一起删掉 ⇒ 向量静默消失。
    """
    milvus_dual_write.deleteByOntologyIdDual(7707, "property")
    milvus_dual_write.insertEmbeddingsDual([_record()])

    assert fakeCollection.calls == [
        "delete:ontology_id == 7707",
        "flush",
        "insert",
        "flush",
    ]


def test_embedding_collection_for_type_maps_each_type() -> None:
    """type → 集合名只有一个来源（写锁按集合名取 ⇒ 映射必须一致）。"""
    assert (
        milvus_collections.embeddingCollectionForType("class")
        == "ontology_class_embeddings"
    )
    assert (
        milvus_collections.embeddingCollectionForType("property")
        == "ontology_property_embeddings"
    )
    assert (
        milvus_collections.embeddingCollectionForType("metric")
        == "ontology_metric_embeddings"
    )
    assert (
        milvus_collections.embeddingCollectionForType("kpi")
        == "kpi_catalog_embeddings"
    )


def test_embedding_collection_for_type_rejects_unknown_type() -> None:
    """未知 type 必须报错（不能静默落到某个集合 ⇒ 锁会锁错集合）。"""
    with pytest.raises(ValueError):
        milvus_collections.embeddingCollectionForType("unknown")


def test_collection_names_are_shared_across_modules() -> None:
    """五处集合名必须同源 —— 各自写一份字面量 ⇒ 改名漏一处 ⇒ 锁错集合 / 召回不到。"""
    assert milvus_dual_write._CLASS_COLLECTION_NAME == (
        milvus_collections.CLASS_COLLECTION_NAME
    )
    assert milvus_dual_write._PROPERTY_COLLECTION_NAME == (
        milvus_collections.PROPERTY_COLLECTION_NAME
    )
    assert milvus_dual_write._METRIC_COLLECTION_NAME == (
        milvus_collections.METRIC_COLLECTION_NAME
    )
    assert milvus_dual_write._KPI_COLLECTION_NAME == (
        milvus_collections.KPI_COLLECTION_NAME
    )
    assert milvus_query_helpers._CLASS_COLLECTION_NAME == (
        milvus_collections.CLASS_COLLECTION_NAME
    )
    assert milvus_query_helpers._PROPERTY_COLLECTION_NAME == (
        milvus_collections.PROPERTY_COLLECTION_NAME
    )
    assert milvus_query_helpers._METRIC_COLLECTION_NAME == (
        milvus_collections.METRIC_COLLECTION_NAME
    )
    assert milvus_query_helpers._KPI_COLLECTION_NAME == (
        milvus_collections.KPI_COLLECTION_NAME
    )
    # 读侧（search）与聚合入口（client）也必须同源 ⇒ 否则「写进 A 集合、查 B 集合」。
    assert milvus_search._CLASS_COLLECTION_NAME == (
        milvus_collections.CLASS_COLLECTION_NAME
    )
    assert milvus_search._PROPERTY_COLLECTION_NAME == (
        milvus_collections.PROPERTY_COLLECTION_NAME
    )
    assert milvus_search._METRIC_COLLECTION_NAME == (
        milvus_collections.METRIC_COLLECTION_NAME
    )
    assert milvus_search._KPI_COLLECTION_NAME == (
        milvus_collections.KPI_COLLECTION_NAME
    )
    assert milvus_client._CLASS_COLLECTION_NAME == (
        milvus_collections.CLASS_COLLECTION_NAME
    )
    assert milvus_client._PROPERTY_COLLECTION_NAME == (
        milvus_collections.PROPERTY_COLLECTION_NAME
    )
    assert milvus_client._METRIC_COLLECTION_NAME == (
        milvus_collections.METRIC_COLLECTION_NAME
    )
    assert milvus_client._KPI_COLLECTION_NAME == (
        milvus_collections.KPI_COLLECTION_NAME
    )
