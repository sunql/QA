"""M0-P0.4: 3-collection schema + external_id field (RED → GREEN).

Pure field-schema assertions; does NOT require real Milvus.
"""
from __future__ import annotations

from app.infrastructure.milvus_client import (
    _classFields,
    _propertyFields,
    _metricFields,
    ensureClassCollection,
    ensurePropertyCollection,
    ensureMetricCollection,
)


class TestClassFields:
    def test_has_external_id(self) -> None:
        names = [f.name for f in _classFields()]
        assert "external_id" in names

    def test_has_embedding(self) -> None:
        names = [f.name for f in _classFields()]
        assert "embedding" in names

    def test_has_ontology_id(self) -> None:
        names = [f.name for f in _classFields()]
        assert "ontology_id" in names


class TestPropertyFields:
    def test_has_external_id(self) -> None:
        names = [f.name for f in _propertyFields()]
        assert "external_id" in names


class TestMetricFields:
    def test_has_external_id(self) -> None:
        names = [f.name for f in _metricFields()]
        assert "external_id" in names


class TestEnsureFunctions:
    """ensure*Collection() 应只注册函数，不实际连接 Milvus。"""

    def test_ensure_class_collection_is_callable(self) -> None:
        assert callable(ensureClassCollection)

    def test_ensure_property_collection_is_callable(self) -> None:
        assert callable(ensurePropertyCollection)

    def test_ensure_metric_collection_is_callable(self) -> None:
        assert callable(ensureMetricCollection)
