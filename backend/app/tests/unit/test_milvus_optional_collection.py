"""KPI 集合是**可选集合**：不存在时读路径优雅降级（既有三集合仍严格）。

背景：`kpi_catalog_embeddings` 在第一次 KPI 写入（``ensureKpiCollection``）之前
根本不存在，而 pymilvus 的 ``Collection(name, using=...)`` 在集合不存在时抛
``SchemaNotReadyException("Collection not exist")`` —— **不是**返回空句柄。

全局读路径会扫所有集合（``searchEmbeddingsByTypeRouted(typeFilter=None)`` 的
扇出、``listEmbeddingsAcross3Collections``），于是「还没有 KPI 向量」这种完全
正常的中间态，会让整条读路径失败：``/ontology/search``（不带 type）、
``/vectors/embeddings``、``/vectors/stats``、``reconcileMissingVectorEmbeddings``
全部中招。

修法：``milvus_client.hasEmbeddingCollection`` 只读探针 + 读路径对 **KPI 段**
跳过。**既有三集合（class/property/metric）的严格语义刻意不变**（缺失仍抛）
—— 本文件含反向守卫锁住这一点。

全部 fake：本地 19530 是生产 Milvus，禁跑真连接（跑测时另加
``MILVUS_URI='http://127.0.0.1:1'`` 兜底）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pymilvus
import pytest
from pymilvus.exceptions import SchemaNotReadyException

from app.infrastructure import milvus_client
from app.infrastructure import milvus_query_helpers as helpers
from app.infrastructure import milvus_search as search

_CLASS = "ontology_class_embeddings"
_PROPERTY = "ontology_property_embeddings"
_METRIC = "ontology_metric_embeddings"
_KPI = "kpi_catalog_embeddings"

_EXISTING_THREE = {_CLASS, _PROPERTY, _METRIC}


class _FakeHit:
    def __init__(self, row: dict, distance: float) -> None:
        self.distance = distance
        self.entity = SimpleNamespace(get=lambda k: row.get(k))


class _FakeCollection:
    """同时支持检索与全量读的假集合（按集合名给出可区分的行）。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def load(self) -> None:
        pass

    def search(self, *, data, anns_field, param, limit, output_fields):
        row = {
            "ontology_id": 1000 + len(self.name),
            "type": self.name,
            "name": "n",
            "alias": "",
            "description": "",
        }
        return [[_FakeHit(row, 0.1)]]

    def query(self, expr: str, output_fields: list[str], limit: int) -> list[dict]:
        return [{
            "ontology_id": 1000 + len(self.name),
            "type": self.name,
            "name": "n",
            "external_id": "",
            "alias": "",
            "description": "",
        }]


def _collectionFactory(existing: set[str], constructed: list[str]):
    """假 Collection 工厂：存在的集合给句柄，缺失的抛「集合不存在」。"""

    def _factory(name: str, using: str | None = None) -> _FakeCollection:
        constructed.append(name)
        if name not in existing:
            raise SchemaNotReadyException(message=f"Collection not exist: {name}")
        return _FakeCollection(name)

    return _factory


def _installSearchFakes(
    monkeypatch: pytest.MonkeyPatch,
    existing: set[str],
    constructed: list[str],
) -> None:
    """打桩检索路径：连接、存在性探针、Collection 构造三者全离线。

    探针用 ``raising=False`` 打桩：反向守卫用例（既有集合缺失仍抛）必须在
    「探针还不存在」的实现前也成立 —— 那时它只是被忽略的多余属性。
    """
    monkeypatch.setattr(search, "_connect", lambda: None)
    monkeypatch.setattr(
        search, "hasEmbeddingCollection", lambda name: name in existing, raising=False
    )
    monkeypatch.setattr(search, "Collection", _collectionFactory(existing, constructed))


def _installQueryFakes(
    monkeypatch: pytest.MonkeyPatch,
    existing: set[str],
    constructed: list[str],
) -> None:
    """打桩全量读路径。

    ``helpers._ensureCollectionByName`` 内部是**函数内** ``from pymilvus import
    Collection``（遮蔽模块级导入），故必须打桩 ``pymilvus.Collection`` 本身才
    能拦住它。
    """
    monkeypatch.setattr(helpers, "_connect", lambda: None)
    monkeypatch.setattr(
        helpers, "hasEmbeddingCollection", lambda name: name in existing, raising=False
    )
    monkeypatch.setattr(pymilvus, "Collection", _collectionFactory(existing, constructed))


# ---------------------------------------------------------------------------
# (a) KPI 集合缺失 + typeFilter=None ⇒ 不抛，返回既有集合的命中
# ---------------------------------------------------------------------------

def test_none_sweep_skips_missing_kpi_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """扇出遇到缺失的 KPI 集合要跳过，而不是把整条读路径带崩。"""
    constructed: list[str] = []
    _installSearchFakes(monkeypatch, _EXISTING_THREE, constructed)

    hits = search.searchEmbeddingsByTypeRouted([0.0], 5, None)

    assert _KPI not in constructed, "缺失的 KPI 集合不该被构造"
    assert len(hits) == 3, f"三个既有集合各一条命中，实际 {hits}"
    assert {h["type"] for h in hits} == {_CLASS, _PROPERTY, _METRIC}


# ---------------------------------------------------------------------------
# (b) KPI 集合缺失 + typeFilter="kpi" ⇒ 返回 []（不抛）
# ---------------------------------------------------------------------------

def test_explicit_kpi_filter_returns_empty_when_collection_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式查 KPI 而集合不存在 ⇒ 空结果，不是异常。"""
    constructed: list[str] = []
    _installSearchFakes(monkeypatch, {_CLASS}, constructed)

    assert search.searchEmbeddingsByTypeRouted([0.0], 5, "kpi") == []
    assert _KPI not in constructed


def test_explicit_kpi_filter_returns_hits_when_collection_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反向：集合存在时 KPI 分支照常检索（正向对照，防「一律返回空」）。"""
    constructed: list[str] = []
    _installSearchFakes(monkeypatch, {_KPI}, constructed)

    hits = search.searchEmbeddingsByTypeRouted([0.0], 5, "kpi")

    assert len(hits) == 1
    assert hits[0]["type"] == _KPI
    assert constructed == [_KPI]


# ---------------------------------------------------------------------------
# (c) listEmbeddingsAcross3Collections：KPI 段缺失时跳过，不抛、结果无 kpi 行
# ---------------------------------------------------------------------------

def test_list_across_collections_skips_missing_kpi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全量读在 KPI 集合缺失时跳过该段，其余集合照读。"""
    constructed: list[str] = []
    _installQueryFakes(monkeypatch, _EXISTING_THREE, constructed)

    rows = helpers.listEmbeddingsAcross3Collections()

    assert _KPI not in constructed, "缺失的 KPI 集合不该被读取"
    assert {r["type"] for r in rows} == {_CLASS, _PROPERTY, _METRIC}
    assert all(r["type"] != "kpi" for r in rows)


def test_list_across_collections_includes_kpi_when_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反向：KPI 集合存在时该段照读（正向对照）。"""
    constructed: list[str] = []
    _installQueryFakes(monkeypatch, _EXISTING_THREE | {_KPI}, constructed)

    rows = helpers.listEmbeddingsAcross3Collections()

    assert _KPI in constructed
    assert any(r["type"] == _KPI for r in rows)


# ---------------------------------------------------------------------------
# (d) 反向守卫：既有三集合缺失时**仍然抛**（严格语义不得被一起放松）
# ---------------------------------------------------------------------------

def test_missing_class_collection_still_raises_in_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既有集合缺失仍是异常 —— 只有 KPI 段是可选的。"""
    constructed: list[str] = []
    _installSearchFakes(monkeypatch, {_PROPERTY, _METRIC, _KPI}, constructed)

    with pytest.raises(SchemaNotReadyException):
        search.searchEmbeddingsByTypeRouted([0.0], 5, None)


def test_missing_property_collection_still_raises_in_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既有集合缺失仍是异常 —— 只有 KPI 段是可选的。"""
    constructed: list[str] = []
    _installSearchFakes(monkeypatch, {_CLASS, _METRIC, _KPI}, constructed)

    with pytest.raises(SchemaNotReadyException):
        search.searchEmbeddingsByTypeRouted([0.0], 5, None)


def test_missing_class_collection_still_raises_in_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全量读同理：既有集合缺失仍抛。"""
    constructed: list[str] = []
    _installQueryFakes(monkeypatch, {_PROPERTY, _METRIC, _KPI}, constructed)

    with pytest.raises(SchemaNotReadyException):
        helpers.listEmbeddingsAcross3Collections()


def test_explicit_filter_on_missing_existing_collection_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式查既有类型而集合缺失 ⇒ 仍抛（不被 KPI 的可选语义波及）。"""
    constructed: list[str] = []
    _installSearchFakes(monkeypatch, {_PROPERTY, _METRIC, _KPI}, constructed)

    with pytest.raises(SchemaNotReadyException):
        search.searchEmbeddingsByTypeRouted([0.0], 5, "class")


# ---------------------------------------------------------------------------
# 探针本身（只读、不建集合）
# ---------------------------------------------------------------------------

def test_probe_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """探针只查存在性：既不建集合也不删集合。"""
    probed: list[str] = []
    monkeypatch.setattr(milvus_client, "_connect", lambda: None)
    monkeypatch.setattr(
        milvus_client.utility,
        "has_collection",
        lambda name, using=None: probed.append(name) or name == _KPI,
    )

    assert milvus_client.hasEmbeddingCollection(_KPI) is True
    assert milvus_client.hasEmbeddingCollection(_CLASS) is False
    assert probed == [_KPI, _CLASS]
