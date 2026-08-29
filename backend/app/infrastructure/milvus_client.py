"""Milvus 向量数据库客户端。

负责：连接管理、ontology_embeddings / query_embeddings 集合的创建/插入/搜索。

embedding 生成由调用方负责（LLM / EmbeddingService），此处只做向量存储与检索。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from app.config import getSettings
from app.domain.error_messages import (
    MSG_PARAM_MILVUS_DATASOURCE_ID,
    MSG_PARAM_MILVUS_ONTOLOGY_ID,
)

logger = logging.getLogger(__name__)

_COLLECTION_NAME = "ontology_embeddings"
_QUERY_COLLECTION_NAME = "query_embeddings"
_DIM = 1024  # 默认 embedding 维度（bge-m3 输出 1024 维；改模型需同步重建集合，见 scripts/backfill_milvus_embeddings.py）

# 合法 embedding 类型。ontology_id 在 Milvus 中非跨类型唯一（类/属性共用 id 序列），
# 删除与检索均须按 type 作用域，非法值在拼接表达式前 fail-fast。
VALID_EMBEDDING_TYPES = frozenset(("class", "property", "metric"))


def getEmbeddingDimension() -> int:
    """返回当前 Milvus 集合的向量维度（供 embedding 服务维度守卫校验）。"""
    return _DIM


def checkHealth() -> None:
    """连通性检查：建立（或复用）连接并列出集合；失败抛异常。

    供服务状态看板探测 Milvus 是否可达。复用 _connect 的既有轻量健康检查路径
    （list_collections），避免依赖已加载集合等较重的操作。
    """
    _connect()
    utility.list_collections(using=_connAlias())


def _connAlias() -> str:
    return "ontology_milvus"


def _getUri() -> str:
    return getSettings().milvusUri


def _connect() -> None:
    """建立（或复用）Milvus 连接；已注册但失效时自动重连。

    has_connection 只检查连接别名是否注册，不保证连接存活，
    故额外用 list_collections 做轻量健康检查。
    """
    if connections.has_connection(_connAlias()):
        try:
            utility.list_collections(using=_connAlias())
            return
        except Exception:
            connections.disconnect(alias=_connAlias())
            logger.warning("Milvus 连接已失效，重新连接")
    uri = _getUri()
    parsed = urlparse(uri if "://" in uri else f"//{uri}")
    host = parsed.hostname or "localhost"
    port = parsed.port or 19530
    connections.connect(alias=_connAlias(), host=host, port=port)


def _hasEmbeddingIndex(collection: Collection) -> bool:
    """embedding 字段是否已有索引（幂等判断用）。"""
    return any(getattr(idx, "field_name", "") == "embedding" for idx in collection.indexes)


def _ensureEmbeddingIndex(collection: Collection) -> None:
    """为集合补齐 embedding 字段索引（幂等）。

    早期版本建的集合可能从未建索引，搜索会抛 index not found（code=700）；
    新建集合同样经此路径建索引。已有索引时直接跳过。
    """
    if _hasEmbeddingIndex(collection):
        return
    collection.create_index(
        "embedding",
        index_params={"index_type": "IVF_FLAT", "metric_type": "L2", "params": {"nlist": 128}},
    )
    logger.info("Milvus collection '%s' 补齐 embedding 索引", collection.name)


def _ensureCollection(name: str, fields: list[FieldSchema]) -> Collection:
    """确保指定集合存在；已有集合缺索引则补齐，不存在则创建并建索引。"""
    _connect()
    if utility.has_collection(name, using=_connAlias()):
        collection = Collection(name, using=_connAlias())
        if not _hasEmbeddingIndex(collection):
            # 旧集合可能已处于 loaded 状态（load 状态服务端持久，容器重启后仍在）。
            # Milvus 对已加载集合建索引可能被拒或索引不生效，需先 release 再建索引，
            # 之后统一 load。
            collection.release()
            _ensureEmbeddingIndex(collection)
        collection.load()
        return collection

    schema = CollectionSchema(fields=fields, description=f"{name} for semantic search")
    collection = Collection(name=name, schema=schema, using=_connAlias())
    _ensureEmbeddingIndex(collection)
    collection.load()
    logger.info("Milvus collection '%s' created (dim=%d)", name, _DIM)
    return collection


def _ontologyFields() -> list[FieldSchema]:
    return [
        FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema(name="ontology_id", dtype=DataType.INT64, description=MSG_PARAM_MILVUS_ONTOLOGY_ID),
        FieldSchema(name="type", dtype=DataType.VARCHAR, max_length=20, description="class|property|metric"),
        FieldSchema(name="name", dtype=DataType.VARCHAR, max_length=200),
        FieldSchema(name="alias", dtype=DataType.VARCHAR, max_length=200),
        FieldSchema(name="description", dtype=DataType.VARCHAR, max_length=2000),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=_DIM),
    ]


def ensureCollection() -> Collection:
    """确保 ontology_embeddings 集合存在（不存在则创建）。"""
    return _ensureCollection(_COLLECTION_NAME, _ontologyFields())


def insertEmbeddings(records: list[dict[str, Any]]) -> None:
    """批量插入 embedding 记录。

    Args:
        records: 每条记录包含 ontology_id, type, name, alias, description, embedding (list[float])
    """
    collection = ensureCollection()
    data = [
        [r["ontology_id"] for r in records],
        [r["type"] for r in records],
        [r["name"] for r in records],
        [r.get("alias") or "" for r in records],
        [r.get("description") or "" for r in records],
        [r["embedding"] for r in records],
    ]
    collection.insert(data)
    collection.flush()
    logger.info("Inserted %d embeddings into Milvus", len(records))


def searchByEmbedding(
    queryEmbedding: list[float],
    topK: int = 5,
    typeFilter: str | None = None,
) -> list[dict[str, Any]]:
    """向量相似度搜索。

    Args:
        queryEmbedding: 查询向量
        topK: 返回条数
        typeFilter: 可选，限定类型（class/property/metric）

    Returns:
        匹配的记录列表，含 ontology_id, type, name, alias, description, distance
    """
    collection = ensureCollection()

    expr = None
    if typeFilter is not None:
        if typeFilter not in VALID_EMBEDDING_TYPES:
            raise ValueError(f"unknown embedding type filter: {typeFilter!r}")
        expr = f'type == "{typeFilter}"'
    results = collection.search(
        data=[queryEmbedding],
        anns_field="embedding",
        param={"metric_type": "L2", "params": {"nprobe": 10}},
        limit=topK,
        output_fields=["ontology_id", "type", "name", "alias", "description"],
        expr=expr,
    )

    hits: list[dict[str, Any]] = []
    for result in results:
        for hit in result:
            hits.append({
                "ontology_id": hit.entity.get("ontology_id"),
                "type": hit.entity.get("type"),
                "name": hit.entity.get("name"),
                "alias": hit.entity.get("alias"),
                "description": hit.entity.get("description"),
                "distance": float(hit.distance),
            })
    return hits


def deleteByOntologyId(ontologyId: int, type: str) -> None:
    """删除指定 ontology_id 与类型的向量记录（type 作用域）。

    背景：ontology_class 与 ontology_property 共用 id 序列，Milvus 的 ontology_id
    非跨类型唯一（类 id=10 与属性 物料类型代码 id=10 可并存）。若只按 ontology_id
    删除，重同步某实体会把同 id 的其他类型向量误删；故表达式必须带 type 过滤。
    """
    if type not in VALID_EMBEDDING_TYPES:
        raise ValueError(f"unknown embedding type: {type!r}")
    collection = ensureCollection()
    expr = f'ontology_id == {ontologyId} and type == "{type}"'
    collection.delete(expr)
    collection.flush()
    logger.info("Deleted Milvus records for ontology_id=%d type=%s", ontologyId, type)


def listAllEmbeddings() -> list[dict[str, Any]]:
    """返回 ontology_embeddings 全量行（含 id/ontology_id/type/name/alias/description/embedding）。

    供一次性数据修复（scripts/backfill_milvus_embeddings.py --cleanup）做全量
    去重后删集重建。query 的 limit 上限为 16384，本集合量级远低于此。
    """
    collection = ensureCollection()
    collection.load()
    return collection.query(
        expr="id >= 0",
        output_fields=[
            "id",
            "ontology_id",
            "type",
            "name",
            "alias",
            "description",
            "embedding",
        ],
        limit=16384,
    )


def dropCollection() -> None:
    """删除 ontology_embeddings 集合（删集重建的确定性收敛用）。

    只允许删除本体集合常量；绝不触碰 query_embeddings（历史查询向量）。
    集合不存在时 no-op。
    """
    _connect()
    if not utility.has_collection(_COLLECTION_NAME, using=_connAlias()):
        return
    Collection(_COLLECTION_NAME, using=_connAlias()).drop()
    logger.info("Dropped Milvus collection '%s'", _COLLECTION_NAME)


# =============================================================================
# Phase 5: query_embeddings（历史查询向量）
# =============================================================================


def _queryFields() -> list[FieldSchema]:
    return [
        FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema(name="session_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="question", dtype=DataType.VARCHAR, max_length=2000),
        FieldSchema(name="sql", dtype=DataType.VARCHAR, max_length=4000),
        FieldSchema(name="datasource_id", dtype=DataType.INT64, description=MSG_PARAM_MILVUS_DATASOURCE_ID),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=_DIM),
    ]


def ensureQueryCollection() -> Collection:
    """确保 query_embeddings 集合存在（不存在则创建）。"""
    return _ensureCollection(_QUERY_COLLECTION_NAME, _queryFields())


def insertQueryEmbedding(
    *,
    sessionId: str,
    question: str,
    sql: str,
    embedding: list[float],
    datasourceId: int | None = None,
) -> None:
    """插入一条历史查询向量（fire-and-forget 由调用方保证异常不扩散）。"""
    collection = ensureQueryCollection()
    collection.insert([
        [sessionId],
        [question],
        [sql],
        [datasourceId if datasourceId is not None else 0],
        [embedding],
    ])
    collection.flush()
    logger.info("Inserted query embedding session=%s", sessionId)


def searchQueryEmbedding(
    queryEmbedding: list[float],
    *,
    topK: int = 5,
    datasourceId: int | None = None,
) -> list[dict[str, Any]]:
    """按向量相似度检索历史查询，返回含 session_id/question/sql/distance 的命中列表。"""
    collection = ensureQueryCollection()
    expr = f"datasource_id == {datasourceId}" if datasourceId is not None else None
    results = collection.search(
        data=[queryEmbedding],
        anns_field="embedding",
        param={"metric_type": "L2", "params": {"nprobe": 10}},
        limit=topK,
        output_fields=["session_id", "question", "sql"],
        expr=expr,
    )

    hits: list[dict[str, Any]] = []
    for result in results:
        for hit in result:
            hits.append({
                "session_id": hit.entity.get("session_id"),
                "question": hit.entity.get("question"),
                "sql": hit.entity.get("sql"),
                "distance": float(hit.distance),
            })
    return hits


def closeConnection() -> None:
    """断开 Milvus 连接（幂等；未连接时 no-op）。"""
    if connections.has_connection(_connAlias()):
        connections.disconnect(alias=_connAlias())
