"""P3a D19：带治理维度的 **v2 向量集合**（schema / 写入 / 带过滤检索）。

为什么是新集合而不是改 v1
------------------------
Milvus 2.4.6 的 ``add_collection_field`` 不可靠（``p0/数据基线.md`` §4 裁决 #4），
而本体的向量总量 < 7,100 行 ⇒ **新建集合 + 全量回填**最稳。v1 保持不动、
与之并存 ⇒ 回退只需关开关，**不需要回滚数据**。

字段与哨兵（定稿见 ``docs/update/1109/p0/milvus-方案.md`` §4）
--------------------------------------------------------------
- ``domain_codes`` ARRAY<VARCHAR>：主域 + 附域；``[]`` = 未归域；
- ``entity_type`` VARCHAR：业务对象码；``""`` = 非 BO 头类；
- ``org_codes`` ARRAY<VARCHAR>：组织码；``[]`` = 无组织；
- ``valid_from`` / ``valid_to`` INT64：epoch 毫秒；``0`` = 自始 / 长期有效；
- ``release_id`` INT64：语义发布 id；``0`` = 无激活发布。

⚠️ **表达式约束（2026-11-15 本地 2.4.6 实测，不是推测）**
------------------------------------------------------
- ARRAY 字段上 ``in [...]`` / ``like "X"`` **直接 query plan 解析失败**
  ⇒ 多值维度只能用 ``array_contains(f, "X") or ...``；
- ``not array_contains(f, "X")`` **会命中空数组行**
  ⇒ 排除语义必须**显式枚举允许域**，否定式会让未归域的行漏进来；
- 空数组是**合法值**（``enable_dynamic_field=false`` 下缺字段写不进去）。

⚠️ **与既有 ``insertEmbeddingsDual`` 的命名区分**
------------------------------------------------
``milvus_dual_write.insertEmbeddingsDual`` 里的 "dual write" 是
**早期「单集合 → 按类型拆三集合」的迁移遗留命名**，**不是** v1/v2 双写。
本模块的 v2 写入与它无关，见 ``insertRegistryV2``。
"""

from __future__ import annotations

import logging
from typing import Any

from pymilvus import Collection, DataType, FieldSchema

from app.domain.registry_filter import RegistryFilter
from app.infrastructure.milvus_client import (
    VALID_EMBEDDING_TYPES,
    _connAlias,
    _ensureCollection,
    _ontologyFields,
    hasEmbeddingCollection,
)

logger = logging.getLogger(__name__)

# v2 集合名：与 v1 一一对应，后缀 _v2。
REGISTRY_V2_COLLECTION_NAMES: dict[str, str] = {
    "class": "ontology_class_embeddings_v2",
    "property": "ontology_property_embeddings_v2",
    "metric": "ontology_metric_embeddings_v2",
    "kpi": "kpi_catalog_embeddings_v2",
}

# 域/组织码数组的容量上限（超过就截断——治理表里主域+附域远小于该数）。
_MAX_CODE_COUNT = 8
_MAX_CODE_LENGTH = 64

# 去重超取倍数（与 v1 同口径）：Milvus 的 limit 按「实体」计数，
# 集合里残留同一 ontology_id 的多份向量会吃光 topK 窗口。
_DEDUP_OVERFETCH = 8


def _registryV2Fields() -> list[FieldSchema]:
    """v2 集合 schema = v1 字段 + external_id + 5 个过滤维度。

    字段**顺序**即 ``insert`` 的列序（pymilvus 是列式写入），改顺序要同步改
    ``insertRegistryV2`` 里的 data 组装。
    """
    return _ontologyFields() + [
        FieldSchema(
            name="external_id",
            dtype=DataType.VARCHAR,
            max_length=128,
            description="M0 unified_id / kpi_catalog.kpi_code",
        ),
        FieldSchema(
            name="domain_codes",
            dtype=DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_capacity=_MAX_CODE_COUNT,
            max_length=_MAX_CODE_LENGTH,
            description="数据域编码（主域+附域）；空数组=未归域",
        ),
        FieldSchema(
            name="entity_type",
            dtype=DataType.VARCHAR,
            max_length=32,
            description="业务对象码；空串=非 BO 头类",
        ),
        FieldSchema(
            name="org_codes",
            dtype=DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_capacity=_MAX_CODE_COUNT,
            max_length=_MAX_CODE_LENGTH,
            description="组织编码；空数组=无组织",
        ),
        FieldSchema(
            name="valid_from",
            dtype=DataType.INT64,
            description="有效期起点 epoch 毫秒；0=自始",
        ),
        FieldSchema(
            name="valid_to",
            dtype=DataType.INT64,
            description="有效期终点 epoch 毫秒；0=长期有效",
        ),
        FieldSchema(
            name="release_id",
            dtype=DataType.INT64,
            description="语义发布 id；0=无激活发布",
        ),
    ]


# ---------------------------------------------------------------------------
# 过滤表达式（纯函数 —— 不连 Milvus，可直接单测）
# ---------------------------------------------------------------------------


def _escapeLiteral(value: str) -> str:
    """转义 Milvus 字符串字面量。

    域码/组织码来自治理表（人工可编辑）⇒ 一个裸 ``"`` 就能把表达式拆开，
    这是**注入面**。转义实测可行（2026-11-15：``"A\\"B"`` 能匹配到 ``A"B`` 的行）。

    非字符串一律拒绝（fail fast）——悄悄 ``str()`` 一下反而可能拼出意料之外的
    表达式。
    """
    if not isinstance(value, str):
        raise ValueError(f"过滤字面量必须是字符串，收到 {type(value).__name__}")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def buildRegistryFilterExpr(registryFilter: RegistryFilter | None) -> str | None:
    """把过滤维度编译成 Milvus ``expr``；无过滤返回 ``None``（不收窄）。

    同维度内 OR、跨维度 AND。**绝不生成 ``not array_contains(...)``**
    （实测会命中空数组行 ⇒ 未归域的行会被当成「非该域」放进来）。
    """
    if registryFilter is None:
        return None

    parts: list[str] = []

    if registryFilter.domainCodes:
        parts.append(
            " or ".join(
                f'array_contains(domain_codes, "{_escapeLiteral(code)}")'
                for code in registryFilter.domainCodes
            )
        )

    if registryFilter.orgCodes:
        parts.append(
            " or ".join(
                f'array_contains(org_codes, "{_escapeLiteral(code)}")'
                for code in registryFilter.orgCodes
            )
        )

    if registryFilter.entityTypes:
        parts.append(
            " or ".join(
                f'entity_type == "{_escapeLiteral(code)}"'
                for code in registryFilter.entityTypes
            )
        )

    if registryFilter.releaseId is not None:
        parts.append(f"release_id == {int(registryFilter.releaseId)}")

    if registryFilter.validAt is not None:
        at = int(registryFilter.validAt)
        # valid_to == 0 = 长期有效（哨兵），不能被区间排除。
        parts.append(f"valid_from <= {at} and (valid_to == 0 or valid_to >= {at})")

    return " and ".join(parts) if parts else None


def requiredRegistryV2Types(typeFilter: str | None) -> list[str]:
    """本轮检索**必需存在**的 v2 集合类型。

    与 v1 的严格/可选语义一致：class/property/metric 缺失即「不可用」；
    ``kpi_catalog_embeddings`` 是**可选集合**（第一次 KPI 写入之前本来就没有）
    ⇒ 缺失不算不可用（v1 读路径同款处理）。
    """
    if typeFilter is None:
        return ["class", "property", "metric"]
    if typeFilter == "kpi":
        return []
    if typeFilter in VALID_EMBEDDING_TYPES:
        return [typeFilter]
    raise ValueError(f"unknown embedding type filter: {typeFilter!r}")


# ---------------------------------------------------------------------------
# 集合管理与写入
# ---------------------------------------------------------------------------


def ensureRegistryV2Collection(type_: str) -> Collection:
    """确保某个 v2 集合存在（幂等；缺索引则补 HNSW 索引）。"""
    name = REGISTRY_V2_COLLECTION_NAMES.get(type_)
    if name is None:
        raise ValueError(f"unknown embedding type: {type_!r}")
    return _ensureCollection(name, _registryV2Fields())


def hasRegistryV2Collection(type_: str) -> bool:
    """探针：v2 集合是否已存在（只读）。"""
    name = REGISTRY_V2_COLLECTION_NAMES.get(type_)
    if name is None:
        return False
    return hasEmbeddingCollection(name)


def insertRegistryV2(records: list[dict[str, Any]]) -> None:
    """写入 v2 集合（按 ``type`` 路由）。

    每条记录需带：``ontology_id`` / ``type`` / ``name`` / ``alias`` /
    ``description`` / ``embedding``，以及可选的 ``external_id``；
    治理维度缺省为空（未归域）——**缺省即显式的「没有这一维」**，
    不是「维度随便填」。
    """
    if not records:
        return

    byType: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        type_ = record.get("type")
        if type_ not in VALID_EMBEDDING_TYPES:
            raise ValueError(
                f"Invalid type {type_!r}; must be one of {sorted(VALID_EMBEDDING_TYPES)}"
            )
        byType.setdefault(type_, []).append(record)

    for type_, rows in byType.items():
        collection = ensureRegistryV2Collection(type_)
        data = [
            [r["ontology_id"] for r in rows],
            [r["type"] for r in rows],
            [r["name"] for r in rows],
            [r.get("alias") or "" for r in rows],
            [r.get("description") or "" for r in rows],
            [r["embedding"] for r in rows],
            [r.get("external_id") or "" for r in rows],
            [_codeList(r.get("domain_codes")) for r in rows],
            [r.get("entity_type") or "" for r in rows],
            [_codeList(r.get("org_codes")) for r in rows],
            [int(r.get("valid_from") or 0) for r in rows],
            [int(r.get("valid_to") or 0) for r in rows],
            [int(r.get("release_id") or 0) for r in rows],
        ]
        collection.insert(data)
        collection.flush()
        logger.info(
            "Registry v2: inserted %d embeddings into %s",
            len(rows),
            REGISTRY_V2_COLLECTION_NAMES[type_],
        )


def deleteRegistryV2(ontologyId: int, type_: str) -> None:
    """从 v2 集合删除某条向量（幂等覆盖的前置动作）。

    与 v1 的 ``deleteByOntologyIdDual`` 同口径：先删后插 ⇒ 重复同步不会留下
    同一 ``ontology_id`` 的多份向量（Milvus 的 topK 按「实体」计数，残留副本
    会吃光召回窗口）。

    ⚠️ **删除可见性**：Milvus 2.4 里删除的实体要 **release + load** 之后才对
    检索不可见（只 flush 不可见）。故这里 delete → flush → release → load；
    release/load 失败只告警（删除本身已提交，下一轮同步会再删一次）。
    """
    name = REGISTRY_V2_COLLECTION_NAMES.get(type_)
    if name is None:
        raise ValueError(f"unknown embedding type: {type_!r}")
    if not hasEmbeddingCollection(name):
        return

    collection = Collection(name, using=_connAlias())
    collection.delete(f"ontology_id == {int(ontologyId)}")
    collection.flush()
    try:
        collection.release()
        collection.load()
    except Exception as exc:  # noqa: BLE001 —— 可见性问题不影响「删除已提交」
        logger.warning("Registry v2: release/load 失败（删除可见性延迟）：%s", exc)
    logger.info(
        "Registry v2: deleted ontology_id=%d from %s", ontologyId, name
    )


def _codeList(values: Any) -> list[str]:
    """治理码列表归一：去空、去重、截断到容量上限。"""
    if not values:
        return []
    codes: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in codes:
            continue
        codes.append(text[:_MAX_CODE_LENGTH])
        if len(codes) >= _MAX_CODE_COUNT:
            break
    return codes


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def searchRegistryV2(
    queryEmbedding: list[float],
    topK: int = 5,
    typeFilter: str | None = None,
    expr: str | None = None,
) -> list[dict[str, Any]]:
    """在 v2 集合上检索（可带 ``expr`` 过滤）。

    返回结构与 v1 一致（``ontology_id`` / ``type`` / ``name`` / ``alias`` /
    ``description`` / ``distance``）⇒ 上层调用方不需要区分读的是 v1 还是 v2。
    """
    if typeFilter is not None and typeFilter not in VALID_EMBEDDING_TYPES:
        raise ValueError(f"unknown embedding type filter: {typeFilter!r}")

    if typeFilter is not None:
        return _searchV2Collection(typeFilter, queryEmbedding, topK, expr)

    hits: list[dict[str, Any]] = []
    for type_ in ("class", "property", "metric", "kpi"):
        hits.extend(_searchV2Collection(type_, queryEmbedding, topK, expr))
    hits.sort(key=lambda h: h["distance"])
    return hits[:topK]


def _searchV2Collection(
    type_: str,
    queryEmbedding: list[float],
    topK: int,
    expr: str | None,
) -> list[dict[str, Any]]:
    """单个 v2 集合检索；集合不存在 ⇒ 返回空（可选集合语义）。"""
    name = REGISTRY_V2_COLLECTION_NAMES[type_]
    if not hasEmbeddingCollection(name):
        return []

    collection = Collection(name, using=_connAlias())
    collection.load()
    results = collection.search(
        data=[queryEmbedding],
        anns_field="embedding",
        param={"metric_type": "L2", "params": {"ef": 64}},
        limit=topK * _DEDUP_OVERFETCH,
        output_fields=["ontology_id", "type", "name", "alias", "description"],
        expr=expr,
    )
    hits: list[dict[str, Any]] = []
    seenIds: set[Any] = set()
    for result in results:
        for hit in result:
            row = {
                "ontology_id": hit.entity.get("ontology_id"),
                "type": hit.entity.get("type"),
                "name": hit.entity.get("name"),
                "alias": hit.entity.get("alias"),
                "description": hit.entity.get("description"),
                "distance": float(hit.distance),
            }
            if row["ontology_id"] in seenIds:
                continue
            seenIds.add(row["ontology_id"])
            hits.append(row)
            if len(hits) >= topK:
                return hits
    return hits
