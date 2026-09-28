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
    """deleteByOntologyId 按 type 作用域删除（回归：类/属性 id 碰撞曾互删）。

    背景：ontology_class 与 ontology_property 共用 id 序列，Milvus 的 ontology_id
    非跨类型唯一（如 类 id=10 与 ItemMaster 属性 物料类型代码 id=10 并存）。若删除
    只按 ontology_id 匹配，重同步某属性会把同 id 的类向量误删（全量回填已触发，
    8 个类向量丢失）。修复后删除表达式必须带 type 过滤。
    """

    def test_expr_scoped_by_type(self, fakeEnv) -> None:
        fake = _configure(fakeEnv, collectionExists=True, indexFields=["embedding"])
        fakeEnv.setattr(milvus_client, "ensureCollection", lambda: fake)
        milvus_client.deleteByOntologyId(10, "property")
        assert fake.deletedExprs == ['ontology_id == 10 and type == "property"']

    def test_class_and_property_same_oid_do_not_clobber(self, fakeEnv) -> None:
        """同一 oid 下类/属性各有向量时，删除属性只删属性行，类行保留。"""
        fake = _configure(fakeEnv, collectionExists=True, indexFields=["embedding"])
        fakeEnv.setattr(milvus_client, "ensureCollection", lambda: fake)
        milvus_client.deleteByOntologyId(10, "class")
        milvus_client.deleteByOntologyId(10, "property")
        assert fake.deletedExprs == [
            'ontology_id == 10 and type == "class"',
            'ontology_id == 10 and type == "property"',
        ]

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
    """listAllEmbeddings 全量读取（cleanup 去重重建的前置）。

    M9：单次 `query(limit=16384)` 的服务端上限是**静默截断** —— 截断结果拿去
    `--cleanup` 删集重建会永久丢掉窗口外的向量，而对账把仍在的行反复判为缺失、
    永不收敛。故全量读取改走 `query_iterator` 分批。
    """

    def test_returns_full_rows_with_embedding(self, fakeEnv) -> None:
        fake = _configure(fakeEnv, collectionExists=True, indexFields=["embedding"])
        fakeEnv.setattr(milvus_client, "ensureCollection", lambda: fake)
        fake.queryResults = [
            {"id": 1, "ontology_id": 10, "type": "property", "embedding": [0.1]},
            {"id": 2, "ontology_id": 10, "type": "class", "embedding": [0.2]},
        ]
        rows = milvus_client.listAllEmbeddings()
        assert len(rows) == 2
        assert rows[0]["ontology_id"] == 10
        assert rows[0]["type"] == "property"

    def test_iterates_every_batch_without_loss_or_reorder(self, fakeEnv) -> None:
        """跨批次取全量：行数与顺序都不丢（正是截断会破坏的两件事）。"""
        fake = _configure(fakeEnv, collectionExists=True, indexFields=["embedding"])
        fakeEnv.setattr(milvus_client, "ensureCollection", lambda: fake)
        batches = [
            [{"id": 1, "ontology_id": 10}, {"id": 2, "ontology_id": 11}],
            [{"id": 3, "ontology_id": 12}],
            [{"id": 4, "ontology_id": 13}, {"id": 5, "ontology_id": 14}],
        ]
        iterator = _FakeQueryIterator(batches)
        fakeEnv.setattr(
            milvus_client, "ensureCollection", lambda: _IteratorCollection(iterator)
        )
        rows = milvus_client.listAllEmbeddings()
        assert [r["id"] for r in rows] == [1, 2, 3, 4, 5]
        assert iterator.closed, "迭代器必须 close（否则泄漏 cache 与游标 checkpoint 文件）"

    def test_iterator_contract_expr_fields_and_batch_size(self, fakeEnv) -> None:
        """契约不变：仍是「全量」expr + 同一字段集；批大小为具名常量（非散落字面量）。"""
        iterator = _FakeQueryIterator([[{"id": 1}]])
        collection = _IteratorCollection(iterator)
        fakeEnv.setattr(milvus_client, "ensureCollection", lambda: collection)
        milvus_client.listAllEmbeddings()
        assert collection.kwargs["expr"] == "id >= 0"
        assert collection.kwargs["output_fields"] == [
            "id",
            "ontology_id",
            "type",
            "name",
            "alias",
            "description",
            "embedding",
        ]
        assert collection.kwargs["batch_size"] == milvus_client._MILVUS_QUERY_PAGE

    def test_empty_collection_returns_empty_list(self, fakeEnv) -> None:
        iterator = _FakeQueryIterator([[]])
        fakeEnv.setattr(
            milvus_client, "ensureCollection", lambda: _IteratorCollection(iterator)
        )
        assert milvus_client.listAllEmbeddings() == []
        assert iterator.closed

    def test_closes_iterator_when_next_raises(self, fakeEnv) -> None:
        """`next()` 抛错也要 close（服务端进程内不泄漏游标资源）。"""
        from pymilvus.exceptions import MilvusException

        iterator = _FakeQueryIterator([[{"id": 1}]])
        iterator.error = MilvusException(message="boom")
        fakeEnv.setattr(
            milvus_client, "ensureCollection", lambda: _IteratorCollection(iterator)
        )
        with pytest.raises(MilvusException):
            milvus_client.listAllEmbeddings()
        assert iterator.closed

    def test_warns_when_row_count_is_exact_page_multiple(
        self, fakeEnv, monkeypatch, caplog
    ) -> None:
        """总行数恰为批大小整数倍 → 记 warning（可能是巧合，也可能是分页边界提前收尾）。"""
        monkeypatch.setattr(milvus_client, "_MILVUS_QUERY_PAGE", 2)
        iterator = _FakeQueryIterator([[{"id": 1}, {"id": 2}], [{"id": 3}, {"id": 4}]])
        fakeEnv.setattr(
            milvus_client, "ensureCollection", lambda: _IteratorCollection(iterator)
        )
        with caplog.at_level(logging.WARNING, logger="app.infrastructure.milvus_client"):
            rows = milvus_client.listAllEmbeddings()
        assert len(rows) == 4
        assert any("整数倍" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]

    def test_no_warning_when_row_count_is_not_page_multiple(
        self, fakeEnv, monkeypatch, caplog
    ) -> None:
        monkeypatch.setattr(milvus_client, "_MILVUS_QUERY_PAGE", 2)
        iterator = _FakeQueryIterator([[{"id": 1}, {"id": 2}], [{"id": 3}]])
        fakeEnv.setattr(
            milvus_client, "ensureCollection", lambda: _IteratorCollection(iterator)
        )
        with caplog.at_level(logging.WARNING, logger="app.infrastructure.milvus_client"):
            assert len(milvus_client.listAllEmbeddings()) == 3
        assert not [r for r in caplog.records if "整数倍" in r.getMessage()]

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
    """dropCollection 删本体集合重建；绝不触碰 query_embeddings。"""

    def test_drops_ontology_collection(self, fakeEnv) -> None:
        fake = _configure(fakeEnv, collectionExists=True, indexFields=["embedding"])
        fakeEnv.setattr(milvus_client.utility, "has_collection", lambda name, **kw: name == "ontology_embeddings")
        dropped: list[str] = []
        fakeEnv.setattr(
            milvus_client, "Collection", lambda name, **kw: SimpleNamespace(drop=lambda: dropped.append(name))
        )
        milvus_client.dropCollection()
        assert dropped == ["ontology_embeddings"]

    def test_missing_collection_is_noop(self, fakeEnv) -> None:
        fakeEnv.setattr(milvus_client.utility, "has_collection", lambda *a, **kw: False)
        milvus_client.dropCollection()
        # 未调用 Collection，无异常即通过


class TestSyncEmbeddingTypeScope:
    """syncEmbedding 把实体类型透传给删除，类/属性 id 碰撞时不互删。"""

    def test_passes_type_to_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.infrastructure.milvus_client as milvus_client
        from app.services.ontology_service import OntologyService

        calls: list[tuple[int, str]] = []
        monkeypatch.setattr(
            milvus_client, "deleteByOntologyId",
            lambda oid, type: calls.append((oid, type)),
        )
        monkeypatch.setattr(milvus_client, "insertEmbeddings", lambda records: None)

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
