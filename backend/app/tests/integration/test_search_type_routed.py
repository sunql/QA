"""M0-P0.4 M12-prep: type-routed searchEmbeddingsByTypeRouted (RED → GREEN).

Real Milvus integration test: searchEmbeddingsByTypeRouted must route to
the 3 type-specific collections (class/property/metric), preserve the
old searchByEmbedding output schema, and merge results when typeFilter=None.

Seeds 1 row per type into the 3 new collections via insertEmbeddingsDual.
Embeddings are orthogonal basis vectors for deterministic L2 distances.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.infrastructure.milvus_client import (
    insertEmbeddingsDual,
    searchEmbeddingsByTypeRouted,
)


_EMBEDDING_DIM = 1024


def _makeOrthogonalBasisVector(coordinate: int) -> list[float]:
    """Build a 1024-dim unit vector with 1.0 at the given coordinate, 0.0 elsewhere.

    Coordinate must be in [0, 1023]. Using orthogonal basis vectors gives
    deterministic L2 distances:
      - distance(v, v) = 0
      - distance(v, w) = sqrt(2) for coordinate(v) != coordinate(w)
    """
    assert 0 <= coordinate < _EMBEDDING_DIM
    vec = [0.0] * _EMBEDDING_DIM
    vec[coordinate] = 1.0
    return vec


def _seedOneRowPerType(milvusCleanClient) -> None:
    """Seed 1 class + 1 property + 1 metric row into the 3 new collections.

    Uses orthogonal basis vectors so query==class_vec yields distance 0 for
    the class row and distance sqrt(2) for the property/metric rows.
    insertEmbeddingsDual writes to BOTH old and new collections (M3 dual-write).
    """
    insertEmbeddingsDual([
        {"ontology_id": 2001, "type": "class", "name": "Class1",
         "alias": "", "description": "", "embedding": _makeOrthogonalBasisVector(0)},
        {"ontology_id": 2002, "type": "property", "name": "Property1",
         "alias": "", "description": "", "embedding": _makeOrthogonalBasisVector(1)},
        {"ontology_id": 2003, "type": "metric", "name": "Metric1",
         "alias": "", "description": "", "embedding": _makeOrthogonalBasisVector(2)},
    ])


@pytest.mark.integration
def test_search_type_routed_class_filter_returns_only_class_rows(milvusCleanClient):
    """typeFilter='class' must hit only ontology_class_embeddings."""
    _seedOneRowPerType(milvusCleanClient)

    queryVec = _makeOrthogonalBasisVector(0)  # == class row's embedding
    hits = searchEmbeddingsByTypeRouted(queryVec, topK=5, typeFilter="class")

    assert all(h["type"] == "class" for h in hits)
    assert len(hits) <= 1  # only one class row exists
    assert hits[0]["ontology_id"] == 2001
    assert hits[0]["distance"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.integration
def test_search_type_routed_property_filter_returns_only_property_rows(milvusCleanClient):
    """typeFilter='property' must hit only ontology_property_embeddings."""
    _seedOneRowPerType(milvusCleanClient)

    queryVec = _makeOrthogonalBasisVector(0)
    hits = searchEmbeddingsByTypeRouted(queryVec, topK=5, typeFilter="property")

    assert all(h["type"] == "property" for h in hits)
    assert len(hits) <= 1  # only one property row exists
    assert hits[0]["ontology_id"] == 2002


@pytest.mark.integration
def test_search_type_routed_no_filter_merges_3_collections(milvusCleanClient):
    """typeFilter=None must search all 3 collections and merge by distance."""
    _seedOneRowPerType(milvusCleanClient)

    queryVec = _makeOrthogonalBasisVector(0)
    hits = searchEmbeddingsByTypeRouted(queryVec, topK=5, typeFilter=None)

    # 1 row from each collection → at least one hit per type, at most 3 total
    assert len(hits) <= 3
    typesSeen = {h["type"] for h in hits}
    assert typesSeen == {"class", "property", "metric"}
    # class row distance == 0, must come first
    assert hits[0]["type"] == "class"
    assert hits[0]["distance"] == pytest.approx(0.0, abs=1e-6)
    assert hits[0]["ontology_id"] == 2001

# ---------------------------------------------------------------------------
# P3a：语义注册表 RAG —— 带治理维度的 v2 召回（**双向**用例）
#
# 为什么放在这个文件：P3a 是「检索路径的第二次演进」（v1 无过滤 → v2 带治理维度
# 过滤），与本文件同属「检索路由」主题。**不新建 integration 文件** ——
# CI 的 integration 分片按「文件排序后下标 mod 4」分配，新增文件会把后续文件
# 换分片（假 net-new 失败，见 Harness/rules/测试规范.md）。
# ---------------------------------------------------------------------------

_REGISTRY_V2_DIM = 1024


@pytest.fixture(scope="module", autouse=True)
def ensureMilvusTestDatabase() -> None:
    """确保 Milvus 隔离库存在（分片归属会变，不能靠同分片文件先跑建库）。"""
    from app.tests.milvus_isolation import ensureIsolatedDatabaseForTest

    ensureIsolatedDatabaseForTest()


@pytest.fixture
def registryV2CleanClient():
    """v2 集合清理夹具：drop 前先过**隔离闸门**（默认库=生产，误删不可逆）。"""
    from app.infrastructure.milvus_client import _connAlias, hasEmbeddingCollection
    from app.infrastructure.milvus_registry_v2 import REGISTRY_V2_COLLECTION_NAMES
    from app.tests.milvus_isolation import guardIsolatedDatabase
    from pymilvus import utility

    def _dropRegistryV2Collections() -> None:
        for name in REGISTRY_V2_COLLECTION_NAMES.values():
            if hasEmbeddingCollection(name):
                utility.drop_collection(name, using=_connAlias())

    guardIsolatedDatabase()
    _dropRegistryV2Collections()
    yield
    _dropRegistryV2Collections()


def _seedRegistryV2Rows() -> list[float]:
    """种 3 行 v2 类向量：命中域 SUP / 命中域 MAT / **未归域**（空维度）。

    三行用同一个向量 ⇒ 未过滤时三者都会进 topK；过滤后只剩命中域的那行。
    """
    from app.infrastructure.milvus_registry_v2 import insertRegistryV2

    vec = [0.5] * _REGISTRY_V2_DIM
    insertRegistryV2([
        {"ontology_id": 9101, "type": "class", "name": "供应商", "alias": "",
         "description": "", "embedding": vec,
         "domain_codes": ("SUP",), "org_codes": ("org_sup",), "entity_type": "SUPPLIER"},
        {"ontology_id": 9102, "type": "class", "name": "物料", "alias": "",
         "description": "", "embedding": vec,
         "domain_codes": ("MAT",), "org_codes": ("org_mat",), "entity_type": "MATERIAL"},
        {"ontology_id": 9103, "type": "class", "name": "未归域类", "alias": "",
         "description": "", "embedding": vec},
    ])
    return vec


@pytest.mark.integration
def test_有权限能召回无权限不能召回(registryV2CleanClient):
    """双向用例（D19 验收口径）：域命中 ⇒ 召回；域不命中 ⇒ **不**召回。

    另外钉死 fail-closed：**未归域**（空维度）的行在域过滤下被排除
    （空数组行不会被 ``array_contains`` 命中）。
    """
    from app.domain.registry_filter import RegistryFilter
    from app.infrastructure.milvus_registry_v2 import (
        buildRegistryFilterExpr,
        searchRegistryV2,
    )

    vec = _seedRegistryV2Rows()

    expr = buildRegistryFilterExpr(RegistryFilter(domainCodes=("SUP",)))
    assert expr is not None
    hits = searchRegistryV2(vec, topK=10, typeFilter="class", expr=expr)
    ids = {h["ontology_id"] for h in hits}
    assert 9101 in ids, "命中域的行必须能召回"
    assert 9102 not in ids, "未命中域的行不得被召回（越权召回=泄漏）"
    assert 9103 not in ids, "未归域的行在域过滤下必须被排除（fail-closed）"

    exprMat = buildRegistryFilterExpr(RegistryFilter(domainCodes=("MAT",)))
    assert exprMat is not None
    idsMat = {h["ontology_id"] for h in
              searchRegistryV2(vec, topK=10, typeFilter="class", expr=exprMat)}
    assert idsMat == {9102}, "换一个域 ⇒ 只召回该域的行"


@pytest.mark.integration
def test_闸门按维度收窄且不回落到无过滤结果(registryV2CleanClient, monkeypatch):
    """闸门（有作用域）必须读 v2 并带过滤；v2 不可用时也不许回落成无过滤的 v1。"""
    from app.domain.registry_filter import RegistryFilter
    from app.services import semantic_registry_rag_gate as gate

    vec = _seedRegistryV2Rows()
    monkeypatch.setattr(
        gate, "getSettings", lambda: SimpleNamespace(enableSemanticRegistryRag=True)
    )

    result = gate.searchRegistryEmbeddings(
        vec, topK=10, typeFilter="class",
        registryFilter=RegistryFilter(domainCodes=("SUP",)),
    )

    assert result.usedRegistryV2 is True
    assert result.degradedReason is None
    assert {h["ontology_id"] for h in result.hits} == {9101}
