"""Milvus 向量数据库客户端。

负责：连接管理、ontology_embeddings / query_embeddings 集合的创建/插入/搜索。

embedding 生成由调用方负责（LLM / EmbeddingService），此处只做向量存储与检索。

M0-P0.4 Phase 2 (Task 15): Split into milvus_client.py + milvus_dual_write.py
+ milvus_query_helpers.py + milvus_search.py.
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

# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

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


# 当前连接实际挂载的 database 名（pymilvus 不再回读该值，自行记录以便比对）。
_connectedDbName: str | None = None


def getDbName() -> str:
    """当前配置的目标 database 名；空配置归一为 Milvus 默认库 "default"。"""
    return getSettings().milvusDbName or "default"


def _connect() -> None:
    """建立（或复用）Milvus 连接；已注册但失效时自动重连。

    has_connection 只检查连接别名是否注册，不保证连接存活，
    故额外用 list_collections 做轻量健康检查。
    连接按 database 维度校验：配置的 MILVUS_DB_NAME 变了也要重连，
    否则「测试库」配置会被已缓存的默认库连接静默吞掉（drop 打回生产集合）。
    """
    global _connectedDbName
    desiredDb = getDbName()
    if connections.has_connection(_connAlias()) and _connectedDbName == desiredDb:
        try:
            utility.list_collections(using=_connAlias())
            return
        except Exception:
            connections.disconnect(alias=_connAlias())
            logger.warning("Milvus 连接已失效，重新连接")
    elif connections.has_connection(_connAlias()):
        connections.disconnect(alias=_connAlias())
        logger.info("Milvus database 配置变更（%s → %s），重新连接", _connectedDbName, desiredDb)
    uri = _getUri()
    parsed = urlparse(uri if "://" in uri else f"//{uri}")
    host = parsed.hostname or "localhost"
    port = parsed.port or 19530
    connections.connect(alias=_connAlias(), host=host, port=port, db_name=desiredDb)
    _connectedDbName = desiredDb


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


# ---------------------------------------------------------------------------
# Phase 5: query_embeddings（历史查询向量）
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Phase 5.2: document_embeddings（文档向量）
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# feat-wiki-semantic-search: wiki_page_embeddings（知识条目向量）
# ---------------------------------------------------------------------------


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
    """测试专用：release + load 强制 QueryNode 刷新 wiki collection.

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
    """测试专用：release + load 强制 QueryNode 刷新 document collection.

    见 ``refreshWikiCollection`` 注释；语义一致，仅用于 document 集合。
    """
    collection = ensureDocumentCollection()
    collection.release()
    collection.load()


# ---------------------------------------------------------------------------
# M0-P0.4: 3-collection schema with external_id field (class/property/metric)
# NOTE: The actual implementations are imported from milvus_dual_write.py,
# milvus_query_helpers.py, and milvus_search.py above.
# This section only re-exports the constants used by callers.
# ---------------------------------------------------------------------------

_CLASS_COLLECTION_NAME = "ontology_class_embeddings"
_PROPERTY_COLLECTION_NAME = "ontology_property_embeddings"
_METRIC_COLLECTION_NAME = "ontology_metric_embeddings"


def closeConnection() -> None:
    """断开 Milvus 连接（幂等；未连接时 no-op）。

    同时清掉 _connectedDbName：它承诺「镜像当前连接挂的库」，留着旧值会让
    _connect() 的快路径拿着过期信息比对（未来若有人绕过 _connect 直连同别名，
    就会漏掉一次该做的重连），故一并归零。
    """
    global _connectedDbName
    if connections.has_connection(_connAlias()):
        connections.disconnect(alias=_connAlias())
    _connectedDbName = None


# ---------------------------------------------------------------------------
# OLD single-collection API (deprecated; legacy after Task 14 drop)
# ---------------------------------------------------------------------------
# Background: ontology_embeddings collection was dropped in Task 14 of M0-P0.4.
# These wrappers preserve Python-level API surface (so existing test fixtures
# and monkeypatches keep working) while redirecting writes/reads to the new
# type-routed collections (or no-op for safety).
#
# ROLLBACK: if drop causes production breakage, recreate the collection via
# `Collection("ontology_embeddings", schema=...)` and revert these wrappers
# to their pre-Task-14 implementations (commits 2ba51d1..9ec442a).


def insertEmbeddings(records: list[dict[str, Any]]) -> None:
    """DEPRECATED: legacy single-collection insert. Redirects to insertEmbeddingsDual."""
    # The old ontology_embeddings collection no longer exists; old API is a
    # thin wrapper around the type-routed dual-write. Records still land in
    # the appropriate new collection via type discriminator.
    insertEmbeddingsDual(records)


def searchByEmbedding(
    queryEmbedding: list[float],
    topK: int = 5,
    typeFilter: str | None = None,
) -> list[dict[str, Any]]:
    """DEPRECATED: legacy single-collection search. Redirects to type-routed search."""
    return searchEmbeddingsByTypeRouted(queryEmbedding, topK, typeFilter)


def deleteByOntologyId(ontologyId: int, type: str) -> None:
    """DEPRECATED: legacy single-collection delete. Redirects to dual delete."""
    if type not in VALID_EMBEDDING_TYPES:
        raise ValueError(f"unknown embedding type: {type!r}")
    deleteByOntologyIdDual(ontologyId, type)


def deleteByOntologyIds(ontologyIds: list[int], type: str) -> None:
    """DEPRECATED: legacy bulk delete. No-op shim (no callers in production)."""
    # No production caller exists; left as a no-op for backward compat with
    # any future import. Original implementation called old collection which
    # no longer exists.
    if type not in VALID_EMBEDDING_TYPES:
        raise ValueError(f"unknown embedding type: {type!r}")
    # Intentionally no-op.


def listAllEmbeddings() -> list[dict[str, Any]]:
    """DEPRECATED: legacy single-collection full read. Redirects to 3-collection read."""
    return listEmbeddingsAcross3Collections()


def ensureCollection() -> "Collection | None":
    """DEPRECATED: legacy single-collection ensure. No-op (collection dropped)."""
    # The legacy ontology_embeddings collection is intentionally absent.
    # Production callers have all migrated to ensureClass/Property/MetricCollection.
    return None


def dropCollection() -> None:
    """DEPRECATED: legacy single-collection drop. No-op (already dropped in Task 14)."""
    # Intentionally no-op; collection was dropped in Task 14. Kept for
    # backward compat with test fixtures that called dropCollection() before.
    pass


# ---------------------------------------------------------------------------
# Imports from extracted modules (Task 15 split — loaded at end to avoid
# circular import: milvus_dual_write.py imports _connAlias etc. from here)
# ---------------------------------------------------------------------------
from app.infrastructure.milvus_dual_write import (
    _classFields,
    _propertyFields,
    _metricFields,
    ensureClassCollection,
    ensurePropertyCollection,
    ensureMetricCollection,
    insertEmbeddingsDual,
    deleteByOntologyIdDual,
    rebuildOntologyCollections,
)
from app.infrastructure.milvus_query_helpers import (
    queryClassEmbeddings,
    queryPropertyEmbeddings,
    queryMetricEmbeddings,
    listEmbeddingsAcross3Collections,
)
from app.infrastructure.milvus_search import (
    searchEmbeddingsByTypeRouted,
)
