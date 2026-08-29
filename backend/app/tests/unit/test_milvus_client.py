"""milvus_client 索引补齐回归测试。

修复点：`_ensureCollection` 对**已存在但从未建索引**的集合（如早期版本遗留的
ontology_embeddings：0 实体、indexes=[]）不建索引，搜索时抛 index not found
（code=700），语义检索恒回退全量。修复后无论新建还是已有集合，都保证 embedding
字段有索引；已有索引时不重复建（幂等）。已加载集合建索引前先 release（load 状态
服务端持久，容器重启后仍在；Milvus 对已加载集合建索引可能被拒或不生效）。
"""

from __future__ import annotations

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
        assert fake.createdIndexParams[0]["index_type"] == "IVF_FLAT"
        assert fake.createdIndexParams[0]["metric_type"] == "L2"  # 与 searchByEmbedding 一致
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


class TestListAllEmbeddings:
    """listAllEmbeddings 全量读取（cleanup 去重重建的前置）。"""

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
