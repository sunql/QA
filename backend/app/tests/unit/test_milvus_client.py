"""milvus_client 索引补齐回归测试。

修复点：`_ensureCollection` 对**已存在但从未建索引**的集合（如早期版本遗留的
ontology_embeddings：0 实体、indexes=[]）不建索引，搜索时抛 index not found
（code=700），语义检索恒回退全量。修复后无论新建还是已有集合，都保证 embedding
字段有索引；已有索引时不重复建（幂等）。已加载集合建索引前先 release（load 状态
服务端持久，容器重启后仍在；Milvus 对已加载集合建索引可能被拒或不生效）。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import app.infrastructure.milvus_client as milvus_client
from app.infrastructure.milvus_client import _ensureCollection


class FakeCollection:
    """最小 Collection 替身：只实现 _ensureCollection 用到的成员。"""

    def __init__(self, indexFields: list[str]) -> None:
        self._indexFields = list(indexFields)
        self.name = "fake_collection"
        self.createdIndexFields: list[str] = []
        self.createdIndexParams: list[dict] = []
        self.loaded = False
        self.released = False
        self.deletedExprs: list[str] = []
        self.queryResults: list[dict] = []
        self.iteratorKwargs: dict = {}
        self.dropped = False

    @property
    def indexes(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(field_name=f) for f in self._indexFields]

    def create_index(self, field_name: str, index_params: dict | None = None, **kwargs) -> None:
        self.createdIndexFields.append(field_name)
        self.createdIndexParams.append(index_params or {})
        self._indexFields.append(field_name)

    def release(self) -> None:
        self.released = True

    def load(self) -> None:
        self.loaded = True

    def delete(self, expr: str) -> None:
        self.deletedExprs.append(expr)

    def flush(self) -> None:
        pass

    def query(self, expr: str, output_fields: list[str], limit: int) -> list[dict]:
        return self.queryResults

    def query_iterator(self, **kwargs) -> _FakeQueryIterator:
        """listAllEmbeddings 走分批迭代取全量（M9）；返回单批假迭代器。"""
        self.iteratorKwargs = kwargs
        return _FakeQueryIterator([self.queryResults])

    def drop(self) -> None:
        self.dropped = True


@pytest.fixture()
def fakeEnv(monkeypatch: pytest.MonkeyPatch):
    """隔离网络与 Milvus 服务端：_connect no-op，has_collection/Collection 可控。"""
    monkeypatch.setattr(milvus_client, "_connect", lambda: None)
    monkeypatch.setattr(milvus_client, "CollectionSchema", lambda *a, **kw: SimpleNamespace())
    return monkeypatch


def _configure(fakeEnv, *, collectionExists: bool, indexFields: list[str]) -> FakeCollection:
    fakeEnv.setattr(
        milvus_client.utility, "has_collection", lambda *a, **kw: collectionExists
    )
    fake = FakeCollection(indexFields)
    fakeEnv.setattr(milvus_client, "Collection", lambda *a, **kw: fake)
    return fake


class TestEnsureCollection:
    def test_existing_collection_without_index_creates_index(self, fakeEnv) -> None:
        """已有集合无 embedding 索引 -> 先 release 再补齐索引（回归：搜索曾抛 index not found）。"""
        fake = _configure(fakeEnv, collectionExists=True, indexFields=[])
        result = _ensureCollection("ontology_embeddings", [])
        assert result is fake
        assert fake.released  # 已加载集合需先 release 再建索引
        assert fake.createdIndexFields == ["embedding"]
        # HNSW 而非 IVF_FLAT：nlist=128 对数百~数千向量桶分布失效，最佳匹配
        # 落进未搜桶被错过（dist=0.85 真匹配 → 返 dist=1.25 错配类）
        assert fake.createdIndexParams[0]["index_type"] == "HNSW"
        assert fake.createdIndexParams[0]["metric_type"] == "L2"  # 与 searchByEmbedding 一致
        assert fake.createdIndexParams[0]["params"]["M"] == 16
        assert fake.loaded

    def test_existing_collection_with_index_skips_create(self, fakeEnv) -> None:
        """已有 embedding 索引 -> 不重复建索引、不 release（幂等）。"""
        fake = _configure(fakeEnv, collectionExists=True, indexFields=["embedding"])
        _ensureCollection("ontology_embeddings", [])
        assert fake.createdIndexFields == []
        assert not fake.released
        assert fake.loaded

    def test_new_collection_creates_index(self, fakeEnv) -> None:
        """新建集合 -> 创建后建索引，再加载。"""
        fake = _configure(fakeEnv, collectionExists=False, indexFields=[])
        _ensureCollection("ontology_embeddings", [])
        assert fake.createdIndexFields == ["embedding"]
        assert fake.createdIndexParams[0]["metric_type"] == "L2"
        assert fake.loaded


class TestDeleteByOntologyId:
    """deleteByOntologyId 是 deleteByOntologyIdDual 的薄包装（Task 14 后的 wrapper）。

    类型白名单仍由 wrapper 自身把关（fail-fast）；实际删除由 Dual 版本走新 collection。
    """

    def test_expr_scoped_by_type(self, fakeEnv) -> None:
        fake = _configure(fakeEnv, collectionExists=True, indexFields=["embedding"])
        fakeEnv.setattr(milvus_client, "ensureCollection", lambda: fake)
        fakeEnv.setattr(milvus_client, "ensureClassCollection", lambda: fake)
        fakeEnv.setattr(milvus_client, "ensurePropertyCollection", lambda: fake)
        fakeEnv.setattr(milvus_client, "ensureMetricCollection", lambda: fake)
        milvus_client.deleteByOntologyId(10, "property")
        # Wrapper now delegates to deleteByOntologyIdDual which deletes from new
        # type-routed collection (no longer scoped by type in expr because
        # the new collection is already per-type).
        assert fake.deletedExprs == ["ontology_id == 10"]

    def test_class_and_property_same_oid_do_not_clobber(self, fakeEnv) -> None:
        """同一 oid 下类/属性各有向量时，分别走各自的 type-routed collection 不互删。"""
        fake = _configure(fakeEnv, collectionExists=True, indexFields=["embedding"])
        fakeEnv.setattr(milvus_client, "ensureCollection", lambda: fake)
        fakeEnv.setattr(milvus_client, "ensureClassCollection", lambda: fake)
        fakeEnv.setattr(milvus_client, "ensurePropertyCollection", lambda: fake)
        fakeEnv.setattr(milvus_client, "ensureMetricCollection", lambda: fake)
        milvus_client.deleteByOntologyId(10, "class")
        milvus_client.deleteByOntologyId(10, "property")
        assert fake.deletedExprs == ["ontology_id == 10", "ontology_id == 10"]

    def test_rejects_unknown_type(self, fakeEnv) -> None:
        fake = _configure(fakeEnv, collectionExists=True, indexFields=["embedding"])
        fakeEnv.setattr(milvus_client, "ensureCollection", lambda: fake)
        with pytest.raises(ValueError):
            milvus_client.deleteByOntologyId(10, "other")


class TestSearchByEmbeddingTypeFilter:
    """searchByEmbedding 的 typeFilter 与 deleteByOntologyId 同一白名单校验。"""

    def test_rejects_unknown_type_filter(self, fakeEnv) -> None:
        fake = _configure(fakeEnv, collectionExists=True, indexFields=["embedding"])
        fakeEnv.setattr(milvus_client, "ensureCollection", lambda: fake)
        with pytest.raises(ValueError):
            milvus_client.searchByEmbedding([0.0], topK=5, typeFilter="other")


class _FakeQueryIterator:
    """假 `query_iterator`：按预置批次逐次吐出，取尽后返回空列表。"""

    def __init__(self, batches: list[list[dict]]) -> None:
        self._batches = [list(b) for b in batches]
        self.nextCalls = 0
        self.closed = False
        self.error: Exception | None = None

    def next(self) -> list[dict]:
        self.nextCalls += 1
        if self.error is not None:
            raise self.error
        return self._batches.pop(0) if self._batches else []

    def close(self) -> None:
        self.closed = True


class TestListAllEmbeddings:
    """listAllEmbeddings 现在是 listEmbeddingsAcross3Collections 的薄包装（Task 14 后）。

    老 API surface 保留以便测试 fixture / monkeypatch 继续工作；M9 分批迭代语义
    已迁移到新 collection（queryClassEmbeddings / queryPropertyEmbeddings /
    queryMetricEmbeddings 各自分批读取），具体实现见 queryClass/Property/Metric 单测。
    """

    def test_delegates_to_3collection_reader(self, fakeEnv) -> None:
        fakeEnv.setattr(
            milvus_client,
            "listEmbeddingsAcross3Collections",
            lambda: [
                {"id": 1, "ontology_id": 10, "type": "class", "embedding": [0.1]},
                {"id": 2, "ontology_id": 11, "type": "property", "embedding": [0.2]},
            ],
        )
        rows = milvus_client.listAllEmbeddings()
        assert len(rows) == 2
        assert rows[0]["ontology_id"] == 10
        assert rows[0]["type"] == "class"

    def test_empty_when_all_collections_empty(self, fakeEnv) -> None:
        fakeEnv.setattr(milvus_client, "listEmbeddingsAcross3Collections", lambda: [])
        assert milvus_client.listAllEmbeddings() == []

    def test_batch_size_constant_is_the_milvus_query_cap(self) -> None:
        """常量即 Milvus `query` 的 limit 服务端上限（16384），三处读取共用同一来源。"""
        assert milvus_client._MILVUS_QUERY_PAGE == 16384


class _IteratorCollection:
    """只提供 query_iterator 的最小集合替身（记录调用 kwargs）。"""

    def __init__(self, iterator: _FakeQueryIterator) -> None:
        self._iterator = iterator
        self.kwargs: dict = {}
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def query_iterator(self, **kwargs) -> _FakeQueryIterator:
        self.kwargs = kwargs
        return self._iterator


class TestDropCollection:
    """dropCollection 现在是 no-op（Task 14 已 drop ontology_embeddings collection）。

    老 API surface 保留以便测试 fixture / monkeypatch 继续工作；不再触碰 Milvus。
    """

    def test_noop_does_not_call_drop(self, fakeEnv) -> None:
        fake = _configure(fakeEnv, collectionExists=True, indexFields=["embedding"])
        fakeEnv.setattr(milvus_client.utility, "has_collection", lambda name, **kw: name == "ontology_embeddings")
        dropped: list[str] = []
        fakeEnv.setattr(
            milvus_client, "Collection", lambda name, **kw: SimpleNamespace(drop=lambda: dropped.append(name))
        )
        milvus_client.dropCollection()
        # 不应再调用 drop（collection 在 Task 14 已删除；wrapper no-op）
        assert dropped == []

    def test_missing_collection_is_noop(self, fakeEnv) -> None:
        fakeEnv.setattr(milvus_client.utility, "has_collection", lambda *a, **kw: False)
        milvus_client.dropCollection()
        # 未调用 Collection，无异常即通过


class TestSyncEmbeddingTypeScope:
    """syncEmbedding 直接调 deleteByOntologyIdDual（Task 13+），不经过 wrapper。

    通过 monkeypatch deleteByOntologyIdDual 验证调用语义；类/属性 id 碰撞时不互删。
    """

    def test_passes_type_to_dual_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.infrastructure.milvus_client as milvus_client
        from app.services.ontology_service import OntologyService

        calls: list[tuple[int, str]] = []
        monkeypatch.setattr(
            milvus_client, "deleteByOntologyIdDual",
            lambda oid, type: calls.append((oid, type)),
        )
        monkeypatch.setattr(milvus_client, "insertEmbeddingsDual", lambda records: None)

        svc = OntologyService()
        svc.syncEmbedding(
            ontologyId=10, type="class", name="ArrivalNotice",
            alias=None, description=None, embedding=[0.1],
        )
        svc.syncEmbedding(
            ontologyId=10, type="property", name="物料类型代码",
            alias=None, description=None, embedding=[0.2],
        )
        assert calls == [(10, "class"), (10, "property")]
