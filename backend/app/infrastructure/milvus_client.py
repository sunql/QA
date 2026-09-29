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
_DOCUMENT_COLLECTION_NAME = "document_embeddings"
_WIKI_PAGE_COLLECTION_NAME = "wiki_page_embeddings"
_DIM = 1024  # 默认 embedding 维度（bge-m3 输出 1024 维；改模型需同步重建集合，见 scripts/backfill_milvus_embeddings.py）

# Milvus `query` 的 limit 服务端上限，同时用作 query_iterator 的批大小（M9）。
# 超过该值的 `query(limit=...)` 会被服务端**静默截断**（不报错）⇒ 任何「取全量」的读取
# 必须走 query_iterator 分批；`query` 只用于 expr 已收敛到小结果集的读取。
_MILVUS_QUERY_PAGE = 16384

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
    # HNSW 而非 IVF_FLAT：实测本体/查询集合体量在数百~数千条，IVF_FLAT
    # 的 nlist=128 把向量分到 128 桶、搜索 nprobe=10 只扫 10 桶——很多向量
    # 落进未搜桶被完全错过（dist=0.85 的真正最佳匹配返 dist=1.25 的错配类），
    # 表现为 chat 召回落 fallback。HNSW 自适应数据规模、无桶分布问题。
    collection.create_index(
        "embedding",
        index_params={"index_type": "HNSW", "metric_type": "L2", "params": {"M": 16, "efConstruction": 200}},
    )
    logger.info("Milvus collection '%s' 补齐 embedding 索引 (HNSW)", collection.name)


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
        param={"metric_type": "L2", "params": {"ef": 64}},
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


def searchEmbeddingsByTypeRouted(
    queryEmbedding: list[float],
    topK: int = 5,
    typeFilter: str | None = None,
) -> list[dict[str, Any]]:
    """向量相似度搜索（type-routed；走 3 个 type-specific collection）。

    Args:
        queryEmbedding: 查询向量
        topK: 返回条数
        typeFilter: 可选，限定类型（class/property/metric）

    Returns:
        匹配的记录列表（含 ontology_id / type / name / alias / description / distance），
        按 distance 升序。

    Notes:
        - typeFilter is None → 跨 3 collection 各取 topK，合并排序取 topK
        - typeFilter == "class" → 只查 ontology_class_embeddings
        - typeFilter == "property" → 只查 ontology_property_embeddings
        - typeFilter == "metric" → 只查 ontology_metric_embeddings
    """
    if typeFilter is not None and typeFilter not in VALID_EMBEDDING_TYPES:
        raise ValueError(f"unknown embedding type filter: {typeFilter!r}")

    if typeFilter == "class":
        return _searchCollection(_CLASS_COLLECTION_NAME, queryEmbedding, topK)
    if typeFilter == "property":
        return _searchCollection(_PROPERTY_COLLECTION_NAME, queryEmbedding, topK)
    if typeFilter == "metric":
        return _searchCollection(_METRIC_COLLECTION_NAME, queryEmbedding, topK)

    # typeFilter is None: search all 3 collections, merge, sort by distance, take topK
    perCollectionTopK = topK
    perHits: list[dict[str, Any]] = []
    for collectionName in (
        _CLASS_COLLECTION_NAME,
        _PROPERTY_COLLECTION_NAME,
        _METRIC_COLLECTION_NAME,
    ):
        perHits.extend(_searchCollection(collectionName, queryEmbedding, perCollectionTopK))

    perHits.sort(key=lambda h: h["distance"])
    return perHits[:topK]


def _searchCollection(
    collectionName: str,
    queryEmbedding: list[float],
    topK: int,
) -> list[dict[str, Any]]:
    """Search one specific collection; returns hits in same schema as old searchByEmbedding."""
    collection = Collection(collectionName, using=_connAlias())
    collection.load()
    results = collection.search(
        data=[queryEmbedding],
        anns_field="embedding",
        param={"metric_type": "L2", "params": {"ef": 64}},
        limit=topK,
        output_fields=["ontology_id", "type", "name", "alias", "description"],
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


def deleteByOntologyIds(ontologyIds: list[int], type: str) -> None:
    """批量删除多个 ontology_id 的同类型向量（对账脚本批量重生成用，单次 flush）。

    type 作用域理由同 deleteByOntologyId：ontology_id 跨类型不唯一。
    """
    if type not in VALID_EMBEDDING_TYPES:
        raise ValueError(f"unknown embedding type: {type!r}")
    if not ontologyIds:
        return
    collection = ensureCollection()
    expr = f'ontology_id in {ontologyIds} and type == "{type}"'
    collection.delete(expr)
    collection.flush()
    logger.info("Deleted Milvus records for %d ontology_ids type=%s", len(ontologyIds), type)


def listAllEmbeddings() -> list[dict[str, Any]]:
    """返回 ontology_embeddings **全量**行（含 id/ontology_id/type/name/alias/description/embedding）。

    供一次性数据修复（scripts/backfill_milvus_embeddings.py --cleanup）做全量去重后
    删集重建，也是 ontology_service 对账的读取入口。

    ⚠️ 必须走分批迭代（M9）：原实现 `query(limit=16384)` 在行数超过 `_MILVUS_QUERY_PAGE`
    时被服务端**静默截断**，而截断结果拿去「删集重建」会**永久丢掉**窗口外的向量
    （对账又把仍在的行反复判为缺失、永不收敛）。`query_iterator` 的 limit 默认
    UNLIMITED，一直取到 `next()` 返回空为止。
    """
    collection = ensureCollection()
    collection.load()
    return _queryAllRows(
        collection,
        expr="id >= 0",
        outputFields=[
            "id",
            "ontology_id",
            "type",
            "name",
            "alias",
            "description",
            "embedding",
        ],
    )


def _queryAllRows(
    collection: Collection,
    *,
    expr: str,
    outputFields: list[str],
    iteratorFactory: Any | None = None,
) -> list[dict[str, Any]]:
    """用 `query_iterator` 分批取全量行并拼成一个列表（不截断）。

    iteratorFactory 是**可注入接缝**（默认 `collection.query_iterator`）：该迭代器在本
    模块属净新用法，单测用假迭代器验证跨批次不丢行/不乱序，不依赖真 Milvus。

    `close()` 必须调用（放 finally）：迭代器持有 iterator cache 与游标 checkpoint 文件，
    `next()` 抛错时不 close 会泄漏 —— 服务端进程内尤其明显。返回空列表即表示取尽。

    pymilvus 3.x 把 ORM 风格 API 标了 deprecated（推荐 MilvusClient.query_iterator）；
    此处沿用 Collection 以与模块其余部分（connections/utility/ORM 集合）一致，迁移
    属独立改动。
    """
    makeIterator = iteratorFactory or collection.query_iterator
    iterator = makeIterator(
        batch_size=_MILVUS_QUERY_PAGE,
        expr=expr,
        output_fields=outputFields,
    )
    rows: list[dict[str, Any]] = []
    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            rows.extend(batch)
    finally:
        iterator.close()
    if rows and len(rows) % _MILVUS_QUERY_PAGE == 0:
        logger.warning(
            "Milvus 全量读取累计 %d 行恰为批大小 %d 的整数倍，疑似在分页边界提前收尾"
            "（expr=%s）；请核对集合实际行数",
            len(rows),
            _MILVUS_QUERY_PAGE,
            expr,
        )
    return rows


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
        param={"metric_type": "L2", "params": {"ef": 64}},
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


# =============================================================================
# Phase 5.2: document_embeddings（文档向量）
# =============================================================================


def _documentFields() -> list[FieldSchema]:
    """document_embeddings 的字段定义。

    ⚠️ 顺序即契约：``insertDocumentChunks`` 用位置列表写数据，增删字段必须
    同时改这里与那里的 data 列表。``id`` 是 auto_id 主键，不出现在 data 中。
    ⚠️ Milvus 2.4 标量字段不支持 NULL，空值用哨兵：页码/段号 -1，章节 ""。
    """
    return [
        FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema(name="document_id", dtype=DataType.VARCHAR, max_length=50),
        FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="chunk_text", dtype=DataType.VARCHAR, max_length=4000),
        FieldSchema(name="chunk_sequence", dtype=DataType.INT64),
        FieldSchema(name="effective_date", dtype=DataType.VARCHAR, max_length=20),
        FieldSchema(name="security_level", dtype=DataType.VARCHAR, max_length=10),
        FieldSchema(name="page_number", dtype=DataType.INT64),
        FieldSchema(name="section_name", dtype=DataType.VARCHAR, max_length=200),
        FieldSchema(name="paragraph_no", dtype=DataType.INT64),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=_DIM),
    ]


def ensureDocumentCollection() -> Collection:
    """确保 document_embeddings 集合存在（不存在则创建）。"""
    return _ensureCollection(_DOCUMENT_COLLECTION_NAME, _documentFields())


def _locatorInt(value: Any) -> int:
    """Milvus 标量不可为 NULL，缺失定位符写哨兵 -1（非合法页码/段号）。"""
    return int(value) if value is not None else -1


def insertDocumentChunks(records: list[dict[str, Any]]) -> None:
    """批量插入文档 chunk 向量记录。

    Args:
        records: 每条记录含 document_id, chunk_id, chunk_text, chunk_sequence,
                 effective_date, security_level, page_number, section_name,
                 paragraph_no, embedding。

                定位符为 None 时写哨兵（页码/段号 -1，章节 ""）。
                顺序必须与 ``_documentFields()`` 一致（id 除外）。
    """
    collection = ensureDocumentCollection()
    data = [
        [r["document_id"] for r in records],
        [r["chunk_id"] for r in records],
        [r["chunk_text"][:4000] for r in records],  # truncate to max_length
        [r["chunk_sequence"] for r in records],
        [r.get("effective_date") or "" for r in records],
        [r.get("security_level") or "" for r in records],
        [_locatorInt(r.get("page_number")) for r in records],
        [(r.get("section_name") or "")[:200] for r in records],
        [_locatorInt(r.get("paragraph_no")) for r in records],
        [r["embedding"] for r in records],
    ]
    collection.insert(data)
    collection.flush()
    logger.info("Inserted %d document chunks into Milvus", len(records))


def searchDocumentChunks(
    queryEmbedding: list[float],
    *,
    securityLevel: str | None = None,
    topK: int = 5,
    consistencyLevel: str | None = None,
) -> list[dict[str, Any]]:
    """向量相似度检索文档 chunks。

    Args:
        queryEmbedding: 查询向量
        securityLevel: 可选，按安全等级过滤（L1/L2/L3）
        topK: 返回条数
        consistencyLevel: 可选覆盖（``"Strong"`` 等）；见 ``queryWikiPageChunks`` 注释。
            生产检索路径不传；测试在 delete 后立即验证时传 ``"Strong"``。

    Returns:
        匹配的 chunk 列表，含 document_id, chunk_id, chunk_text, chunk_sequence,
        security_level, page_number, section_name, paragraph_no, distance

        注意定位符是**哨兵值**：无页码/段号时为 -1，无章节时为 ""（Milvus 2.4
        标量字段不支持 NULL），消费方需自行判断，不要直接展示 -1。
    """
    collection = ensureDocumentCollection()

    expr = None
    if securityLevel is not None:
        expr = f'security_level == "{securityLevel}"'
    searchKwargs: dict[str, Any] = {
        "data": [queryEmbedding],
        "anns_field": "embedding",
        "param": {"metric_type": "L2", "params": {"ef": 64}},
        "limit": topK,
        "output_fields": [
            "document_id",
            "chunk_id",
            "chunk_text",
            "chunk_sequence",
            "security_level",
            "page_number",
            "section_name",
            "paragraph_no",
        ],
        "expr": expr,
    }
    if consistencyLevel is not None:
        searchKwargs["consistency_level"] = consistencyLevel
    results = collection.search(**searchKwargs)

    hits: list[dict[str, Any]] = []
    for result in results:
        for hit in result:
            hits.append({
                "document_id": hit.entity.get("document_id"),
                "chunk_id": hit.entity.get("chunk_id"),
                "chunk_text": hit.entity.get("chunk_text"),
                "chunk_sequence": hit.entity.get("chunk_sequence"),
                "security_level": hit.entity.get("security_level"),
                "page_number": hit.entity.get("page_number"),
                "section_name": hit.entity.get("section_name"),
                "paragraph_no": hit.entity.get("paragraph_no"),
                "distance": float(hit.distance),
            })
    return hits


def deleteDocumentChunks(documentId: str) -> None:
    """删除指定 document_id 的全部 chunk。

    表达式**必须**带 document_id 过滤：集合是跨调用方共享的，一个没有
    过滤条件的 ``collection.delete("")`` 会把整个集合清空。
    """
    collection = ensureDocumentCollection()
    collection.delete(f'document_id == "{documentId}"')
    collection.flush()
    logger.info("Deleted Milvus document chunks for document_id=%s", documentId)


def queryDocumentChunks(
    documentId: str,
    consistency_level: str | None = None,
) -> list[dict[str, Any]]:
    """按 document_id 查出该文档的全部 chunk（不走向量检索）。

    门禁脚本要检查的是「写进去的定位符对不对」，不是「检索得准不准」。
    用 ``searchDocumentChunks`` 会因为集合跨调用方共享、topK 截断而漏掉
    目标行 —— 那会把门禁变成抛硬币。

    单个 document 的 chunk 数远低于 ``_MILVUS_QUERY_PAGE``，故仍用单次 query
    （超出该上限会被静默截断；真有单文档超限的一天，需与 listAllEmbeddings 一样
    改走 ``_queryAllRows``）。

    `consistency_level`：见 `queryWikiPageChunks` 注释；测试场景在 delete 后
    立即回读验证时传 ``"Strong"``。
    """
    collection = ensureDocumentCollection()
    collection.load()
    kwargs: dict[str, Any] = {"limit": _MILVUS_QUERY_PAGE}
    if consistency_level is not None:
        kwargs["consistency_level"] = consistency_level
    return collection.query(
        expr=f'document_id == "{documentId}"',
        output_fields=[
            "document_id",
            "chunk_id",
            "chunk_text",
            "chunk_sequence",
            "page_number",
            "section_name",
            "paragraph_no",
        ],
        **kwargs,
    )


# =============================================================================
# feat-wiki-semantic-search: wiki_page_embeddings（知识条目向量）
# =============================================================================


def _wikiPageFields() -> list[FieldSchema]:
    """wiki_page_embeddings 的字段定义。

    ⚠️ 顺序即契约：``insertWikiPageChunks`` 用位置列表写数据，增删字段必须
    同时改这里与那里的 data 列表。``id`` 是 auto_id 主键，不出现在 data 中。
    title/dimension/status 是写入时的快照副本，仅作检索过滤与降级展示；
    展示层的 SSOT 始终是 PG wiki_page（回查覆盖）。
    """
    return [
        FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema(name="page_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="chunk_text", dtype=DataType.VARCHAR, max_length=4000),
        FieldSchema(name="chunk_sequence", dtype=DataType.INT64),
        FieldSchema(name="title", dtype=DataType.VARCHAR, max_length=200),
        FieldSchema(name="dimension", dtype=DataType.VARCHAR, max_length=30),
        FieldSchema(name="status", dtype=DataType.VARCHAR, max_length=10),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=_DIM),
    ]


def ensureWikiPageCollection() -> Collection:
    """确保 wiki_page_embeddings 集合存在（不存在则创建）。"""
    return _ensureCollection(_WIKI_PAGE_COLLECTION_NAME, _wikiPageFields())


def insertWikiPageChunks(records: list[dict[str, Any]]) -> None:
    """批量插入知识条目 chunk 向量记录。

    顺序必须与 ``_wikiPageFields()`` 一致（id 除外）。
    """
    collection = ensureWikiPageCollection()
    data = [
        [r["page_id"] for r in records],
        [r["chunk_id"] for r in records],
        [r["chunk_text"][:4000] for r in records],  # truncate to max_length
        [r["chunk_sequence"] for r in records],
        [(r.get("title") or "")[:200] for r in records],
        [(r.get("dimension") or "")[:30] for r in records],
        [(r.get("status") or "")[:10] for r in records],
        [r["embedding"] for r in records],
    ]
    collection.insert(data)
    collection.flush()
    logger.info("Inserted %d wiki page chunks into Milvus", len(records))


def searchWikiPageChunks(
    queryEmbedding: list[float],
    *,
    dimension: str | None = None,
    statusExclude: tuple[str, ...] = ("EXPIRED",),
    topK: int = 10,
) -> list[dict[str, Any]]:
    """向量相似度检索知识条目 chunks。

    Args:
        queryEmbedding: 查询向量
        dimension: 可选，按知识维度过滤
        statusExclude: 排除的生命周期状态（默认排除 EXPIRED——过期知识不参与检索，
                       与知识图谱 graph_data.py 的口径一致）
        topK: 返回条数

    Returns:
        匹配 chunk 列表，含 page_id, chunk_id, chunk_text, chunk_sequence,
        title, dimension, status, distance
    """
    collection = ensureWikiPageCollection()

    exprParts = []
    if statusExclude:
        quoted = ",".join(f'"{s}"' for s in statusExclude)
        exprParts.append(f"status not in [{quoted}]")
    if dimension is not None:
        exprParts.append(f'dimension == "{dimension}"')
    expr = " and ".join(exprParts) or None

    results = collection.search(
        data=[queryEmbedding],
        anns_field="embedding",
        param={"metric_type": "L2", "params": {"ef": 64}},
        limit=topK,
        output_fields=[
            "page_id",
            "chunk_id",
            "chunk_text",
            "chunk_sequence",
            "title",
            "dimension",
            "status",
        ],
        expr=expr,
    )

    hits: list[dict[str, Any]] = []
    for result in results:
        for hit in result:
            hits.append({
                "page_id": hit.entity.get("page_id"),
                "chunk_id": hit.entity.get("chunk_id"),
                "chunk_text": hit.entity.get("chunk_text"),
                "chunk_sequence": hit.entity.get("chunk_sequence"),
                "title": hit.entity.get("title"),
                "dimension": hit.entity.get("dimension"),
                "status": hit.entity.get("status"),
                "distance": hit.distance,
            })
    return hits


def deleteWikiPageChunks(pageId: str) -> None:
    """删除指定 page_id 的全部 chunk（upsert 的 delete 半边）。

    表达式**必须**带 page_id 过滤：集合是跨调用方共享的，一个没有
    过滤条件的 ``collection.delete("")`` 会把整个集合清空。
    """
    collection = ensureWikiPageCollection()
    collection.delete(f'page_id == "{pageId}"')
    collection.flush()
    logger.info("Deleted Milvus wiki page chunks for page_id=%s", pageId)


def queryWikiPageChunks(
    pageId: str,
    consistency_level: str | None = None,
) -> list[dict[str, Any]]:
    """按 page_id 直查该条目的全部 chunk（不走向量检索，对账/门禁用）。

    `consistency_level`：可选覆盖（``"Strong"`` / ``"Bounded"`` / ``"Session"`` /
    ``"Eventually"`` 或 0/1/2/3）。**默认 None**（沿用 collection 级默认 Bounded），
    适用于生产检索路径。

    **实测提示（2026-09-27）**：PyMilvus 2.4.6 在 collection 已配 Bounded 时，
    per-request ``consistency_level="Strong"`` **在 delete 后的 0~5s 窗口内仍可能
    返回残留行**（Bounded 默认 5s 容忍窗口覆盖），并未真正等到 delete 落地。
    测试场景的可靠写法：delete → ``refreshWikiCollection()``（release + reload
    强制 QueryNode 刷新 delta binlog）→ query。参数**保留**以便服务端未来支持
    per-request 时立即生效，且若 collection 配 Strong 可零成本用上。
    """
    collection = ensureWikiPageCollection()
    collection.load()
    kwargs: dict[str, Any] = {"limit": _MILVUS_QUERY_PAGE}
    if consistency_level is not None:
        kwargs["consistency_level"] = consistency_level
    return collection.query(
        expr=f'page_id == "{pageId}"',
        output_fields=[
            "page_id",
            "chunk_id",
            "chunk_text",
            "chunk_sequence",
            "title",
            "dimension",
            "status",
        ],
        **kwargs,
    )


def refreshWikiCollection() -> None:
    """测试专用：release + load 强制 QueryNode 刷新 wiki collection。

    Milvus delete buffer 走 ``Proxy → DML channel → DataNode → QueryNode``
    三段异步管道；``flush()`` 仅持久化（Growing → Sealed → 对象存储），
    不保证 QueryNode 已加载并应用 delta binlog。**实测在 collection 默认
    Bounded（5s 容忍窗口）下，per-request ``consistency_level="Strong"`` 不能
    立即看到 delete 后状态**。

    **release + load** 强制 QueryNode 重新装载 segment + delta binlog，
    delete 立即对后续 query 可见（实测 < 3s 完成）。

    **生产对账不要用** —— 2-3s 延迟过大。仅集成测试 delete-then-query 验证场景。
    """
    collection = ensureWikiPageCollection()
    collection.release()
    collection.load()


def refreshDocumentCollection() -> None:
    """测试专用：release + load 强制 QueryNode 刷新 document collection。

    见 ``refreshWikiCollection`` 注释；语义一致，仅用于 document 集合。
    """
    collection = ensureDocumentCollection()
    collection.release()
    collection.load()


# =============================================================================
# M0-P0.4: 3-collection schema with external_id field (class/property/metric)
# =============================================================================

_CLASS_COLLECTION_NAME = "ontology_class_embeddings"
_PROPERTY_COLLECTION_NAME = "ontology_property_embeddings"
_METRIC_COLLECTION_NAME = "ontology_metric_embeddings"


def _classFields() -> list[FieldSchema]:
    """Class ontology embedding schema (includes external_id field)."""
    return _ontologyFields() + [
        FieldSchema(name="external_id", dtype=DataType.VARCHAR, max_length=128,
                    description="M0 unified_id, e.g. obj:class:1001"),
    ]


def _propertyFields() -> list[FieldSchema]:
    """Property ontology embedding schema (includes external_id field)."""
    return _ontologyFields() + [
        FieldSchema(name="external_id", dtype=DataType.VARCHAR, max_length=128,
                    description="M0 unified_id, e.g. obj:property:2001"),
    ]


def _metricFields() -> list[FieldSchema]:
    """Metric ontology embedding schema (includes external_id field)."""
    return _ontologyFields() + [
        FieldSchema(name="external_id", dtype=DataType.VARCHAR, max_length=128,
                    description="M0 unified_id, e.g. obj:metric:3001"),
    ]


def ensureClassCollection() -> Collection:
    """Ensure ontology_class_embeddings collection exists (creates if absent)."""
    return _ensureCollection(_CLASS_COLLECTION_NAME, _classFields())


def ensurePropertyCollection() -> Collection:
    """Ensure ontology_property_embeddings collection exists (creates if absent)."""
    return _ensureCollection(_PROPERTY_COLLECTION_NAME, _propertyFields())


def ensureMetricCollection() -> Collection:
    """Ensure ontology_metric_embeddings collection exists (creates if absent)."""
    return _ensureCollection(_METRIC_COLLECTION_NAME, _metricFields())


def insertEmbeddingsDual(records: list[dict[str, Any]]) -> None:
    """Dual-write: insert into BOTH old ontology_embeddings AND type-routed new collection.

    Per-record routing: ``type='class'`` → ontology_class_embeddings, etc.
    Each new collection's ``external_id`` field is initially empty string; backfill
    (Task M7) will populate it.

    Raises:
        ValueError: if any record has invalid ``type`` (not in VALID_EMBEDDING_TYPES).
    """
    if not records:
        return

    # Validate types
    for r in records:
        type_ = r.get("type")
        if type_ not in VALID_EMBEDDING_TYPES:
            raise ValueError(f"Invalid type {type_!r}; must be one of {sorted(VALID_EMBEDDING_TYPES)}")

    # Old collection write (single call to existing function)
    insertEmbeddings(records)

    # New collection write: route by type
    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_type.setdefault(r["type"], []).append(r)

    if "class" in by_type:
        _insertIntoNewCollection(_CLASS_COLLECTION_NAME, by_type["class"])
    if "property" in by_type:
        _insertIntoNewCollection(_PROPERTY_COLLECTION_NAME, by_type["property"])
    if "metric" in by_type:
        _insertIntoNewCollection(_METRIC_COLLECTION_NAME, by_type["metric"])


def _insertIntoNewCollection(name: str, records: list[dict[str, Any]]) -> None:
    """Insert records into a new 3-collection (class/property/metric) variant.

    Same schema as existing insertEmbeddings PLUS the new external_id field
    (initially ""; backfilled by Task M7).
    """
    if name == _CLASS_COLLECTION_NAME:
        collection = ensureClassCollection()
    elif name == _PROPERTY_COLLECTION_NAME:
        collection = ensurePropertyCollection()
    elif name == _METRIC_COLLECTION_NAME:
        collection = ensureMetricCollection()
    else:
        raise ValueError(f"Unknown collection name: {name}")

    data = [
        [r["ontology_id"] for r in records],       # id is auto_id, skip
        [r["type"] for r in records],
        [r["name"] for r in records],
        [r.get("alias") or "" for r in records],
        [r.get("description") or "" for r in records],
        [r["embedding"] for r in records],         # embedding before external_id
        ["" for _ in records],                     # external_id: empty initially, backfilled by M7
    ]
    collection.insert(data)
    collection.flush()
    logger.info("Dual-write: inserted %d embeddings into %s", len(records), name)


def queryClassEmbeddings() -> list[dict[str, Any]]:
    """Read all rows from ontology_class_embeddings."""
    return _queryAllRowsFromCollection(_CLASS_COLLECTION_NAME)


def queryPropertyEmbeddings() -> list[dict[str, Any]]:
    """Read all rows from ontology_property_embeddings."""
    return _queryAllRowsFromCollection(_PROPERTY_COLLECTION_NAME)


def queryMetricEmbeddings() -> list[dict[str, Any]]:
    """Read all rows from ontology_metric_embeddings."""
    return _queryAllRowsFromCollection(_METRIC_COLLECTION_NAME)


def _queryAllRowsFromCollection(name: str) -> list[dict[str, Any]]:
    """Full query of a collection, returning list[dict].

    Uses simple query(limit=_MILVUS_QUERY_PAGE) since each new collection
    stays well under 16384 rows in production. For future scale, swap to
    query_iterator like _queryAllRows does.
    """
    collection = _ensureCollectionByName(name)
    collection.load()
    results = collection.query(
        expr="id >= 0",
        output_fields=["ontology_id", "type", "name", "external_id"],
        limit=_MILVUS_QUERY_PAGE,
    )
    return results


def _ensureCollectionByName(name: str) -> Collection:
    """Connect and get Collection handle by name (assumes collection already exists)."""
    _connect()
    return Collection(name, using=_connAlias())


def closeConnection() -> None:
    """断开 Milvus 连接（幂等；未连接时 no-op）。"""
    if connections.has_connection(_connAlias()):
        connections.disconnect(alias=_connAlias())


def deleteByOntologyIdDual(ontologyId: int, type: str) -> None:
    """Dual-delete: 从 old collection 与 type-routed 新 collection 同时删除。

    双写窗口期必须双删：仅删 old 会在新 collection 留幻影行，
    下一次 insertEmbeddingsDual 又会重建 old，但新 collection 的孤立行
    导致 reconcile 报 placeholder_failed。

    type 作用域理由同 deleteByOntologyId：ontology_id 跨类型不唯一。
    """
    if type not in VALID_EMBEDDING_TYPES:
        raise ValueError(f"unknown embedding type: {type!r}")

    # Old collection
    deleteByOntologyId(ontologyId, type)

    # Type-routed new collection
    name_to_ensure = {
        "class": ensureClassCollection,
        "property": ensurePropertyCollection,
        "metric": ensureMetricCollection,
    }[type]
    new_collection = name_to_ensure()
    new_collection.delete(f"ontology_id == {ontologyId}")
    new_collection.flush()
    logger.info(
        "Dual-deleted Milvus records for ontology_id=%d type=%s",
        ontologyId, type,
    )


def listEmbeddingsAcross3Collections() -> list[dict[str, Any]]:
    """读取 3 个新 type-specific collection 全量，合并返回。

    与 listAllEmbeddings() 的差别：读 ontology_class_embeddings /
    ontology_property_embeddings / ontology_metric_embeddings，而非旧
    ontology_embeddings。M7 backfill 后 3 个新 collection 数据与旧一致；
    M12+ 旧 collection 丢弃后，此函数成为唯一读取入口。

    适用：诊断/统计 API（vectors.py 等）。
    """
    _connect()
    rows: list[dict[str, Any]] = []
    for query_fn in (
        queryClassEmbeddings,
        queryPropertyEmbeddings,
        queryMetricEmbeddings,
    ):
        rows.extend(query_fn())
    return rows
