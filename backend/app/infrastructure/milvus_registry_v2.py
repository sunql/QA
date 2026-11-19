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
from typing import Any, Sequence

from pymilvus import Collection, DataType, FieldSchema

from app.domain.registry_filter import RegistryFilter
from app.infrastructure.milvus_client import (
    VALID_EMBEDDING_TYPES,
    _connAlias,
    _ensureCollection,
    _ontologyFields,
    buildClassIdScopeExpr,
    hasEmbeddingCollection,
    hnswSearchParams,
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

# ``entity_type`` 是标量列，列宽比数组元素窄（见 ``_registryV2Fields``）。
_MAX_ENTITY_TYPE_LENGTH = 32

# 探针/核对读集合时的单页行数。
_READ_PAGE = 1000

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


def _normalizeFilterCodes(
    values: tuple[str, ...], dimension: str, maxLength: int
) -> tuple[str, ...]:
    """过滤侧字面量归一 + 校验（与写侧 ``_codeList`` **同口径**）。

    为什么必须与写侧对齐：写侧会 strip / 丢空串 / 截断到列宽上限，
    过滤侧若不同步 ⇒ 同一个码在两侧的判定不一致 ⇒

    - **空字面量**：``entity_type == ""`` 命中**所有非 BO 头类**
      （空串是存储层的合法哨兵值）⇒ 调用方传一个空实体类型，
      得到的是「放大」而不是「收窄」；
    - **超长字面量**：写侧被静默截断，过滤侧若持**被截断的前缀**，
      可能命中「真实码更长」的域行 ⇒ 跨域可见（**泄漏方向**）。

    ⇒ 空 / 纯空白 / 超长 / 含控制字符 一律 **fail fast 拒绝**
    （闸门把异常转成空结果 ⇒ fail-closed）。

    控制字符：实测裸 LF/CR 会让 Milvus 表达式解析失败
    （``token recognition error``）⇒ 整条表达式报错 ⇒ 该域召回全挂
    （可用性攻击）。域码/组织码里换行从来不是合法值。
    """
    codes: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(
                f"{dimension}过滤字面量必须是字符串，收到 {type(value).__name__}"
            )
        text = value.strip()
        if not text:
            raise ValueError(f"{dimension}过滤字面量不得为空或纯空白")
        if len(text) > maxLength:
            raise ValueError(
                f"{dimension}过滤字面量长度 {len(text)} 超过上限 {maxLength}"
            )
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
            raise ValueError(f"{dimension}过滤字面量含控制字符")
        if text not in codes:
            codes.append(text)
    return tuple(codes)


def buildRegistryFilterExpr(registryFilter: RegistryFilter | None) -> str | None:
    """把过滤维度编译成 Milvus ``expr``；无过滤返回 ``None``（不收窄）。

    同维度内 OR、跨维度 AND。**绝不生成 ``not array_contains(...)``**
    （实测会命中空数组行 ⇒ 未归域的行会被当成「非该域」放进来）。

    ⚠️ 字面量不合法（空 / 超长 / 含控制字符）⇒ **抛 ``ValueError``**：
    闸门把异常转成空结果（fail-closed）；放行则会把「收窄」变成「放大」。
    """
    if registryFilter is None:
        return None

    domainCodes = _normalizeFilterCodes(
        registryFilter.domainCodes, "域", _MAX_CODE_LENGTH
    )
    orgCodes = _normalizeFilterCodes(
        registryFilter.orgCodes, "组织", _MAX_CODE_LENGTH
    )
    entityTypes = _normalizeFilterCodes(
        registryFilter.entityTypes, "实体", _MAX_ENTITY_TYPE_LENGTH
    )

    parts: list[str] = []

    if domainCodes:
        parts.append(
            " or ".join(
                f'array_contains(domain_codes, "{_escapeLiteral(code)}")'
                for code in domainCodes
            )
        )

    if orgCodes:
        parts.append(
            " or ".join(
                f'array_contains(org_codes, "{_escapeLiteral(code)}")'
                for code in orgCodes
            )
        )

    if entityTypes:
        parts.append(
            " or ".join(
                f'entity_type == "{_escapeLiteral(code)}"'
                for code in entityTypes
            )
        )

    if registryFilter.releaseId is not None:
        releaseId = int(registryFilter.releaseId)
        if releaseId <= 0:
            # release_id == 0 是「无激活发布」的**哨兵值**，不是发布版本。
            # 放它进表达式 ⇒ 条件退化成「召回所有没有激活发布的行」，
            # 而调用方以为自己限定了某个发布 ⇒ 口径完全错位。
            # ⇒ 当非法输入拒绝（上层 fail-closed 成空结果）。
            raise ValueError(
                f"releaseId 必须是正的发布 id（0 是「无激活发布」哨兵），收到 {releaseId}"
            )
        parts.append(f"release_id == {releaseId}")

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


def _collectionEntityCount(name: str) -> int:
    """集合的**物理**行数（只读；含墓碑）。

    ⚠️ 物理行数 ≠ 可见行数：删除只打墓碑，行数不减 ⇒ 物理行数只能判
    「有没有写过」，**不能**当「召回会看到多少行」用（可见行数见回填脚本
    ``_countV2Rows`` 的去重计数）。
    """
    collection = Collection(name, using=_connAlias())
    return int(collection.num_entities)


def registryV2RowCount(type_: str) -> int:
    """v2 集合的物理行数；集合不存在返回 ``0``。

    只读探针（开关打开时用于启动期就绪告警）。
    """
    name = REGISTRY_V2_COLLECTION_NAMES.get(type_)
    if name is None:
        raise ValueError(f"unknown embedding type: {type_!r}")
    if not hasEmbeddingCollection(name):
        return 0
    return _collectionEntityCount(name)


def _collectionRows(name: str) -> list[dict[str, Any]]:
    """读集合的**治理维度行**（只读；不含向量）。

    用 ``query``（返回可见行）而不是 ``num_entities``（物理行数，含墓碑）：
    维度探针要回答的是「**能召回的行**里有多少带维度」。
    """
    collection = Collection(name, using=_connAlias())
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = collection.query(
            expr="id >= 0",
            limit=_READ_PAGE,
            offset=offset,
            output_fields=[
                "ontology_id",
                "domain_codes",
                "org_codes",
                "entity_type",
            ],
        )
        if not page:
            break
        rows.extend(page)
        offset += len(page)
        if len(page) < _READ_PAGE:
            break
    return rows


def _rowHasDimension(row: dict[str, Any]) -> bool:
    """该行是否带了**任一**治理维度（域 / 组织 / 实体）。"""
    if row.get("domain_codes"):
        return True
    if row.get("org_codes"):
        return True
    return bool(str(row.get("entity_type") or "").strip())


def registryV2DimensionedRowCount(type_: str) -> int:
    """**带任一治理维度**的可见行数；集合不存在返回 ``0``。

    为什么物理行数不够：v2 里「行在、维度全空」是完全可能的
    （维度解析失败写空 / 只回填了未归域的行）⇒ 带过滤的召回**恒空** ⇒
    只看行数的探针会误报「就绪」⇒ 又是静默零召回。
    """
    name = REGISTRY_V2_COLLECTION_NAMES.get(type_)
    if name is None:
        raise ValueError(f"unknown embedding type: {type_!r}")
    if not hasEmbeddingCollection(name):
        return 0
    return sum(1 for row in _collectionRows(name) if _rowHasDimension(row))


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


def _andExprs(*exprs: str | None) -> str | None:
    """合并多个 Milvus 过滤表达式（``and`` 关系）。

    全空 ⇒ ``None``（不过滤）；只有一个 ⇒ 原样返回；多个 ⇒ ``(a) and (b)``。
    加括号是必要的：``a and b or c`` 的优先级不是我们想要的。
    """
    present = [expr for expr in exprs if expr]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return " and ".join(f"({expr})" for expr in present)


def searchRegistryV2(
    queryEmbedding: list[float],
    topK: int = 5,
    typeFilter: str | None = None,
    expr: str | None = None,
    classOntologyIds: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """在 v2 集合上检索（可带 ``expr`` 过滤）。

    Args:
        expr: **治理维度**过滤表达式（域/组织/实体/发布/有效期）。与本体类 id
            作用域（``classOntologyIds``）**并存**：两者用 ``and`` 合并，不互相覆盖。
        classOntologyIds: **本体类 id 作用域**（Gap A）。``None`` = 不限；
            空集 = deny-all。**只作用于 class 集合**（``ontology_id`` 在类/属性
            之间共用 id 序列）。

    返回结构与 v1 一致（``ontology_id`` / ``type`` / ``name`` / ``alias`` /
    ``description`` / ``distance``）⇒ 上层调用方不需要区分读的是 v1 还是 v2。
    """
    if typeFilter is not None and typeFilter not in VALID_EMBEDDING_TYPES:
        raise ValueError(f"unknown embedding type filter: {typeFilter!r}")

    classScopeExpr = buildClassIdScopeExpr(classOntologyIds)

    if typeFilter is not None:
        return _searchV2Collection(
            typeFilter, queryEmbedding, topK, expr,
            classScopeExpr=classScopeExpr if typeFilter == "class" else None,
        )

    hits: list[dict[str, Any]] = []
    for type_ in ("class", "property", "metric", "kpi"):
        hits.extend(_searchV2Collection(
            type_, queryEmbedding, topK, expr,
            classScopeExpr=classScopeExpr if type_ == "class" else None,
        ))
    hits.sort(key=lambda h: h["distance"])
    return hits[:topK]


def _searchV2Collection(
    type_: str,
    queryEmbedding: list[float],
    topK: int,
    expr: str | None,
    classScopeExpr: str | None = None,
) -> list[dict[str, Any]]:
    """单个 v2 集合检索；集合不存在 ⇒ 返回空（可选集合语义）。

    ``expr``（治理维度）与 ``classScopeExpr``（本体类 id 作用域）用 ``and`` 合并：
    两个口径是**交集**关系，任一为 ``None`` 时只剩另一个。
    """
    name = REGISTRY_V2_COLLECTION_NAMES[type_]
    if not hasEmbeddingCollection(name):
        return []

    combinedExpr = _andExprs(expr, classScopeExpr)

    collection = Collection(name, using=_connAlias())
    collection.load()
    results = collection.search(
        data=[queryEmbedding],
        anns_field="embedding",
        param=hnswSearchParams(topK * _DEDUP_OVERFETCH),
        limit=topK * _DEDUP_OVERFETCH,
        output_fields=["ontology_id", "type", "name", "alias", "description"],
        expr=combinedExpr,
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
