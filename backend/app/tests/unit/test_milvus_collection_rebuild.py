"""rebuildOntologyCollections：drop + 重建 3 个本体向量集合。

背景（2026-09-30）：scripts/backfill_milvus_embeddings.py --cleanup 自称
「删集重建 + 整批插入」，但它调的是 `milvus.dropCollection()` / `ensureCollection()`
—— Task 14 丢弃旧 ontology_embeddings 后这两个已是 **no-op**，于是 cleanup 退化成
纯 append：每跑一次就多一整套向量（实测 32 类 / 3642 属性被堆成 3 倍，
正是检索 topK 窗口被吃光的根源）。本模块提供真正的 drop 重建原语。
"""

from __future__ import annotations

import pytest

import app.infrastructure.milvus_dual_write as dual_write
from app.infrastructure.milvus_dual_write import rebuildOntologyCollections

_EXPECTED_COLLECTIONS = [
    "ontology_class_embeddings",
    "ontology_property_embeddings",
    "ontology_metric_embeddings",
]


@pytest.fixture()
def fakeMilvus(monkeypatch: pytest.MonkeyPatch):
    """隔离 Milvus：记录 drop 与 ensure 的调用顺序，has_collection 可控。"""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(dual_write, "_connect", lambda: None)

    def _ensure(name: str, collectionName: str):
        def _fn() -> None:
            calls.append(("ensure", collectionName))
        return _fn

    for attr, coll in (
        ("ensureClassCollection", _EXPECTED_COLLECTIONS[0]),
        ("ensurePropertyCollection", _EXPECTED_COLLECTIONS[1]),
        ("ensureMetricCollection", _EXPECTED_COLLECTIONS[2]),
    ):
        monkeypatch.setattr(dual_write, attr, _ensure(attr, coll))

    return calls, monkeypatch


def _setHasCollection(monkeypatch, exists: bool) -> None:
    monkeypatch.setattr(
        dual_write.utility, "has_collection", lambda name, using=None: exists
    )


def test_drops_then_recreates_all_three_collections(fakeMilvus) -> None:
    """三个集合都存在 → 逐个 drop，再逐个 ensure（顺序：drop 全部先于 ensure 全部）。"""
    calls, monkeypatch = fakeMilvus
    _setHasCollection(monkeypatch, True)
    monkeypatch.setattr(
        dual_write.utility,
        "drop_collection",
        lambda name, using=None: calls.append(("drop", name)),
    )

    rebuildOntologyCollections()

    assert calls == [
        ("drop", "ontology_class_embeddings"),
        ("drop", "ontology_property_embeddings"),
        ("drop", "ontology_metric_embeddings"),
        ("ensure", "ontology_class_embeddings"),
        ("ensure", "ontology_property_embeddings"),
        ("ensure", "ontology_metric_embeddings"),
    ]


def test_ensures_without_dropping_when_collection_absent(fakeMilvus) -> None:
    """集合不存在（首次运行）→ 不 drop，但仍 ensure（幂等）。"""
    calls, monkeypatch = fakeMilvus
    _setHasCollection(monkeypatch, False)
    monkeypatch.setattr(
        dual_write.utility,
        "drop_collection",
        lambda name, using=None: calls.append(("drop", name)),
    )

    rebuildOntologyCollections()

    assert calls == [
        ("ensure", "ontology_class_embeddings"),
        ("ensure", "ontology_property_embeddings"),
        ("ensure", "ontology_metric_embeddings"),
    ]
