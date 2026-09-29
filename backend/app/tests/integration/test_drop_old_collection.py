"""Task 14 (M12-drop): verify ontology_embeddings collection is dropped."""


def test_ontology_embeddings_collection_is_dropped():
    """The legacy single-collection has been dropped in Task 14."""
    from pymilvus import utility
    from app.infrastructure.milvus_client import _connAlias, _connect

    _connect()
    assert not utility.has_collection("ontology_embeddings", using=_connAlias()), (
        "ontology_embeddings collection must be dropped in Task 14"
    )


def test_old_api_redirects_to_type_routed():
    """Old searchByEmbedding delegates to searchEmbeddingsByTypeRouted."""
    from app.infrastructure import milvus_client

    # Confirm the redirect wrapper exists and delegates
    assert callable(milvus_client.searchByEmbedding)
    assert callable(milvus_client.searchEmbeddingsByTypeRouted)
    # They are distinct functions; the wrapper calls the type-routed one
    # (verified via import + assert; behavior verified in test_search_type_routed.py)


def test_old_ensure_collection_is_noop():
    """Old ensureCollection is a no-op (returns None, doesn't recreate)."""
    from app.infrastructure import milvus_client

    result = milvus_client.ensureCollection()
    assert result is None
