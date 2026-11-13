"""Milvus type-routed 第 4 个 collection：kpi_catalog_embeddings。

KPI 目录此前完全在向量库之外（模型文档明确排除）；本批把它纳入与本体三集合
同形的 type-routed 管线。全部用 fake double——本地 19530 是生产 Milvus，禁跑真连接。
"""

from __future__ import annotations

import pytest

from app.infrastructure import milvus_client
from app.infrastructure import milvus_dual_write as dual
from app.infrastructure import milvus_query_helpers as helpers
from app.infrastructure import milvus_search as search


class _FakeCollection:
    """记录 insert 数据与 delete 表达式的假集合。"""

    def __init__(self) -> None:
        self.inserted: list[list] = []
        self.deletedExprs: list[str] = []

    def insert(self, data: list) -> None:
        self.inserted.append(data)

    def delete(self, expr: str) -> None:
        self.deletedExprs.append(expr)

    def flush(self) -> None:
        pass


def test_kpi_type_is_valid() -> None:
    assert "kpi" in milvus_client.VALID_EMBEDDING_TYPES


def test_kpi_collection_constants() -> None:
    assert dual._KPI_COLLECTION_NAME == "kpi_catalog_embeddings"
    assert search._KPI_COLLECTION_NAME == "kpi_catalog_embeddings"
    assert helpers._KPI_COLLECTION_NAME == "kpi_catalog_embeddings"
    assert milvus_client._KPI_COLLECTION_NAME == "kpi_catalog_embeddings"


def test_kpi_fields_have_external_id_and_embedding() -> None:
    names = [f.name for f in dual._kpiFields()]
    assert "external_id" in names
    assert "embedding" in names
    assert "ontology_id" in names


def test_kpi_ensure_is_callable() -> None:
    """callable() 不触发实际 Milvus 连接。"""
    assert callable(dual.ensureKpiCollection)
    assert callable(milvus_client.ensureKpiCollection)


def test_ensure_kpi_collection_uses_kpi_name_and_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ensureKpiCollection 用 KPI 集合名 + 含 external_id 的 schema 建集。"""
    seen: list[tuple[str, list]] = []
    fake = _FakeCollection()
    monkeypatch.setattr(
        dual,
        "_ensureCollection",
        lambda name, fields: seen.append((name, fields)) or fake,
    )

    assert dual.ensureKpiCollection() is fake
    assert seen[0][0] == "kpi_catalog_embeddings"
    fieldNames = [f.name for f in seen[0][1]]
    assert "external_id" in fieldNames
    assert "embedding" in fieldNames
    assert "ontology_id" in fieldNames


def test_insert_routes_kpi_type(monkeypatch: pytest.MonkeyPatch) -> None:
    routed: list[str] = []
    monkeypatch.setattr(
        dual, "_insertIntoNewCollection", lambda name, records: routed.append(name)
    )
    # type="kpi" 不再 raise（此前不在 VALID_EMBEDDING_TYPES 会 ValueError）
    dual.insertEmbeddingsDual([
        {"ontology_id": 1, "type": "kpi", "name": "n", "alias": "",
         "description": "", "embedding": [0.0], "external_id": "KPI_X"},
    ])
    dual.insertEmbeddingsDual([{"ontology_id": 1, "type": "kpi", "name": "n",
                                "alias": "", "description": "", "embedding": [0.0]}])
    assert routed == ["kpi_catalog_embeddings", "kpi_catalog_embeddings"]


def test_insert_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        dual.insertEmbeddingsDual([{"ontology_id": 1, "type": "bogus",
                                    "name": "n", "embedding": [0.0]}])


def test_external_id_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """_insertIntoNewCollection 必须透传 external_id（KPI 用 kpi_code），缺省给 ""。"""
    fake = _FakeCollection()
    monkeypatch.setattr(dual, "ensureKpiCollection", lambda: fake)
    dual._insertIntoNewCollection(dual._KPI_COLLECTION_NAME, [
        {"ontology_id": 7, "type": "kpi", "name": "a", "alias": "",
         "description": "", "embedding": [0.1], "external_id": "KPI_A"},
        {"ontology_id": 8, "type": "kpi", "name": "b", "alias": "",
         "description": "", "embedding": [0.2]},
    ])
    externalColumn = fake.inserted[0][6]
    assert externalColumn == ["KPI_A", ""]


def test_delete_routes_kpi_type(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCollection()
    monkeypatch.setattr(dual, "ensureKpiCollection", lambda: fake)
    dual.deleteByOntologyIdDual(7, "kpi")
    assert fake.deletedExprs == ["ontology_id == 7"]


def test_delete_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        dual.deleteByOntologyIdDual(7, "bogus")


def test_search_kpi_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fakeSearch(collectionName, q, topK):
        seen.append(collectionName)
        return []

    monkeypatch.setattr(search, "_searchCollection", fakeSearch)
    monkeypatch.setattr(search, "_connect", lambda: None)
    monkeypatch.setattr(search, "hasEmbeddingCollection", lambda name: True)
    search.searchEmbeddingsByTypeRouted([0.0], 5, "kpi")
    assert seen == ["kpi_catalog_embeddings"]


def test_search_none_sweep_covers_4_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        search, "_searchCollection", lambda name, q, k: seen.append(name) or []
    )
    monkeypatch.setattr(search, "_connect", lambda: None)
    monkeypatch.setattr(search, "hasEmbeddingCollection", lambda name: True)
    search.searchEmbeddingsByTypeRouted([0.0], 5, None)
    assert seen == [
        "ontology_class_embeddings",
        "ontology_property_embeddings",
        "ontology_metric_embeddings",
        "kpi_catalog_embeddings",
    ]


def test_query_helpers_kpi_read(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        helpers, "_queryAllRowsFromCollection", lambda name: called.append(name) or []
    )
    monkeypatch.setattr(helpers, "hasEmbeddingCollection", lambda name: True)
    assert helpers.queryKpiEmbeddings() == []
    assert called == ["kpi_catalog_embeddings"]


def test_query_helpers_sweep_covers_4(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        helpers, "_queryAllRowsFromCollection", lambda name: called.append(name) or []
    )
    monkeypatch.setattr(helpers, "_connect", lambda: None)
    monkeypatch.setattr(helpers, "hasEmbeddingCollection", lambda name: True)
    helpers.listEmbeddingsAcross3Collections()
    assert "kpi_catalog_embeddings" in called
    assert len(called) == 4
