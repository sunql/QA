"""Milvus 侧的「读行 → 体检 → delete → insert → 刷新 → 复扫校验」原语。

与 ``scripts/milvus_external_id_placeholder.py``（PG 侧占位行判定）分工：
- **PG 侧**：能不能写、写了会不会悬垂引用；
- **本模块（Milvus 侧）**：行能不能取到、载荷完不完整、写了之后**看得见**吗。

为什么单独成文件：这段逻辑有两条独立的硬约束（delete 之前的体检、delete 之后的
后置条件校验），彼此耦合但与「跑哪几个集合、PG 怎么处理」无关 ⇒ 拆开才读得懂。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from pymilvus import Collection

from app.domain.id_mapping import buildUnifiedId
from app.infrastructure.milvus_client import (
    _MILVUS_QUERY_PAGE,
    _connAlias,
    refreshCollection,
)

logger = logging.getLogger(__name__)

# 元数据投影：不含 embedding（1024 维）⇒ 扫描成本低；**必须含 id**（auto-id 主键），
# 否则无法精确定位「要回填的那一行」。
_META_FIELDS: list[str] = [
    "id", "ontology_id", "type", "name", "alias", "description", "external_id",
]
# 全量投影：元数据 + embedding，用于取出待重插的行内容。
_FULL_FIELDS: list[str] = [*_META_FIELDS, "embedding"]

# 重插时的列顺序（必须与集合 schema 的字段顺序一致）。
_COLUMN_FIELDS: tuple[str, ...] = (
    "ontology_id", "type", "name", "alias", "description", "embedding", "external_id",
)

# 复扫校验的重试次数与间隔（秒）：release+load 之后 delete 应当立即可见，
# 这里给一点传播延迟的容忍；用尽仍不满足 ⇒ 抛错。
_RESIDUE_CHECK_ATTEMPTS = 3
_RESIDUE_CHECK_INTERVAL_SECONDS = 1.0

# 一次 delete 的最大主键数：批量 delete 把「delete 已发出、insert 还没发出」的
# 危险窗口从 N 次串行往返压到 1 次。
_DELETE_BATCH_SIZE = 500

# 复扫校验用强一致：集合默认 Bounded（5s 容忍窗口）⇒ 默认查询可能读到旧视图
# ⇒ 后置条件形同虚设。release+load 之后数据已封段，Strong 不会误判。
_STRONG_CONSISTENCY = "Strong"


class BackfillContractError(RuntimeError):
    """回填契约未成立（幂等性 / 数据安全前提被破坏）—— **必须中止**，不得继续。

    与「基础设施类失败」严格区分：基础设施失败（PG 不可用、集合 RPC 报错）
    只影响一个集合，可以隔离继续；而契约被破坏说明脚本**没有做到它承诺的事**
    （写少了、没写进去、写进去了但看不见），继续跑只会扩大损失 ⇒ 直接抛出。
    """


def asInt(value: object) -> int | None:
    """把 Milvus 字段转成 int；转不了就返回 None（跳过该行，绝不猜）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def queryRows(
    collectionName: str,
    outputFields: list[str],
    *,
    consistencyLevel: str | None = None,
) -> list[dict]:
    """读集合全量（按 outputFields 投影）。

    ⚠️ Milvus 的 ``query(limit=...)`` 超过服务端上限会被**静默截断**（不报错）⇒
    「取全量」的读取必须先确认没撞上限，否则被截掉的行既不会被回填、也不会被
    复扫校验看到 —— 契约就成了空话。
    （同款守卫见 ``scripts/backfill_milvus_embeddings.py``。）
    """
    collection = Collection(collectionName, using=_connAlias())
    collection.load()
    queryKwargs = {}
    if consistencyLevel is not None:
        queryKwargs["consistency_level"] = consistencyLevel
    rows = collection.query(
        expr="id >= 0",
        output_fields=list(outputFields),
        limit=_MILVUS_QUERY_PAGE,
        **queryKwargs,
    )
    if len(rows) >= _MILVUS_QUERY_PAGE:
        raise BackfillContractError(
            f"{collectionName}: 命中 Milvus 查询上限 {_MILVUS_QUERY_PAGE} 行，"
            f"读到的不是全量 ⇒ 需改用 query_iterator 分批后再回填"
        )
    return rows


def targetsNeedingBackfill(rows: list[dict]) -> list[dict]:
    """挑出 external_id 为空的行。

    这些行自带 auto-id（id）⇒ 后续 delete 精确打在「要回填的那一行」上。

    ⚠️ 不能改用 ontology_id 定位：同一 ontology_id 可能有多行（历史 delete
    不可见留下的残留重复行），按 ontology_id 取会取到另一行 ⇒ 重插的是**另一行
    的内容** ⇒ 越修越乱。
    """
    return [r for r in rows if not r.get("external_id")]


def embeddingDim(collection: Collection) -> int | None:
    """集合里 embedding 字段的维度；取不到（fake / 无向量字段）返回 None。"""
    for field in getattr(collection.schema, "fields", []):
        if getattr(field, "name", "") != "embedding":
            continue
        params = getattr(field, "params", None) or {}
        dim = params.get("dim")
        if isinstance(dim, int) and not isinstance(dim, bool):
            return dim
    return None


def assertCollectionUsable(collection: Collection, collectionName: str) -> None:
    """**delete 之前**体检：集合必须能接收我们要写的全部字段。

    少一个字段 ⇒ 整批 insert 会被服务端拒绝 ⇒ 若此时已经 delete 过，同批的行
    全部丢失（Milvus **无自动备份** ⇒ 不可逆）⇒ 必须在任何 delete 之前中止。
    """
    existing = {getattr(f, "name", "") for f in getattr(collection.schema, "fields", [])}
    missing = [f for f in _COLUMN_FIELDS if f not in existing]
    if missing:
        raise BackfillContractError(
            f"{collectionName}: 集合 schema 缺少字段 {missing} ⇒ 整批 insert 会失败 ⇒ "
            f"已 delete 的行会全部丢失 ⇒ 在任何 delete 之前中止"
        )


def payloadProblem(full: dict, expectedDim: int | None) -> str:
    """返回 '' 表示载荷可用于重插；否则返回原因。

    载荷不完整的行会被**排除出本批次**（不删、不插、计入 invalid）—— 旧实现把
    缺失的 embedding 悄悄变成空列表，会让整批 insert 因维度不匹配失败 ⇒ 同批
    已 delete 的行全部丢失。
    """
    embedding = full.get("embedding")
    if not isinstance(embedding, list) or not embedding:
        return "embedding 缺失或为空"
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in embedding):
        return "embedding 含非数值"
    if expectedDim is not None and len(embedding) != expectedDim:
        return f"embedding 维度 {len(embedding)} ≠ 集合维度 {expectedDim}"
    if asInt(full.get("ontology_id")) is None:
        return "ontology_id 不可用"
    for field in ("name", "alias", "description"):
        value = full.get(field)
        if value is not None and not isinstance(value, str):
            return f"{field} 不是字符串"
    return ""


@dataclass(frozen=True)
class TargetRow:
    """一个待回填目标行 —— 按**行自己的 auto-id** 定位（不按 ontology_id）。"""

    autoId: int
    ontologyId: int
    expectedUid: str
    full: dict


def resolveTargetRow(
    target: dict,
    type_: str,
    byAutoId: dict[int, dict],
    expectedDim: int | None,
) -> tuple[TargetRow | None, str]:
    """把一行目标解析成 TargetRow。

    返回 (None, reason)，reason ∈ {"invalid", "vanished"}：
    - "invalid"：id / ontology_id 不可用、或载荷不完整 ⇒ 数据本身有问题，跳过并计数；
    - "vanished"：行在两次扫描之间消失 ⇒ 跳过（不能拿残缺内容去插一行）。
    """
    autoId = asInt(target.get("id"))
    if autoId is None:
        logger.warning(
            "type=%s: 跳过没有可用 id 的行 ontology_id=%s", type_, target.get("ontology_id")
        )
        return None, "invalid"

    full = byAutoId.get(autoId)
    if full is None:
        logger.info("type=%s: 行 id=%s 在两次扫描之间消失，跳过", type_, autoId)
        return None, "vanished"

    ontologyId = asInt(full.get("ontology_id"))
    if ontologyId is None:
        logger.warning("type=%s: 跳过 id=%s 的行（ontology_id 不可用）", type_, autoId)
        return None, "invalid"

    problem = payloadProblem(full, expectedDim)
    if problem:
        logger.warning("type=%s: 跳过 id=%s 的行（%s）", type_, autoId, problem)
        return None, "invalid"

    return (
        TargetRow(
            autoId=autoId,
            ontologyId=ontologyId,
            expectedUid=buildUnifiedId(type_, str(ontologyId)),
            full=full,
        ),
        "",
    )


def appendRow(columns: dict[str, list], full: dict, type_: str, expectedUid: str) -> None:
    """把待重插的行内容按列追加（容器只在本次调用内新建，之后不再变更）。"""
    columns["ontology_id"].append(int(full["ontology_id"]))
    columns["type"].append(type_)
    columns["name"].append(full.get("name") or "")
    columns["alias"].append(full.get("alias") or "")
    columns["description"].append(full.get("description") or "")
    columns["embedding"].append(list(full["embedding"]))
    columns["external_id"].append(expectedUid)


@dataclass(frozen=True)
class VisibilityGap:
    """复扫时「后置条件未满足」的具体缺口（三类，可同时出现）。

    ⚠️ 这里**没有**「期望的 uid 是否可见」这一项：它被 ``missingNew`` 与
    ``wrongUid`` **严格蕴含**（每个期望 uid 都挂在某一个新行上 ⇒ 该行要么不可见
    ⇒ ``missingNew``；要么可见但 external_id 不符 ⇒ ``wrongUid``）⇒ 单独加它
    只会得到一个**永远不会独立触发**的检查（变异取证 M5 实证：把它去掉，
    全部测试仍然绿 ⇒ 当时它就是死代码，已删除）。
    """

    residue: list[int]
    missingNew: list[int]
    wrongUid: list[tuple[int, str]]

    @property
    def isEmpty(self) -> bool:
        return not (self.residue or self.missingNew or self.wrongUid)

    def describe(self) -> str:
        return (
            f"已删行仍可见 {len(self.residue)} 个（ids={self.residue[:10]}）、"
            f"新行不可见 {len(self.missingNew)} 个、"
            f"external_id 不符 {len(self.wrongUid)} 个"
        )


def checkVisibility(
    rows: list[dict],
    autoIds: list[int],
    pairs: list[tuple[int, str]],
) -> VisibilityGap:
    """比对一次复扫的结果与期望，返回缺口。

    「新行」按**插入顺序**配对（newId ↔ expectedUid），不靠 uid 集合差集 ——
    同一 ontology_id 本就可能有多行，集合差集分不清「哪一行是新行」。
    """
    byId = {r.get("id"): r for r in rows}
    deletedTarget = set(autoIds)
    return VisibilityGap(
        residue=sorted(deletedTarget & set(byId)),
        missingNew=[newId for newId, _ in pairs if newId not in byId],
        wrongUid=[
            (newId, expected)
            for newId, expected in pairs
            if newId in byId and byId[newId].get("external_id") != expected
        ],
    )


async def assertBackfillApplied(
    collectionName: str,
    pendingAutoIds: list[int],
    expectedUids: list[str],
    *,
    insertCount: int,
    newIds: list[int],
) -> None:
    """复扫校验：delete 与 insert 两条后置条件都必须成立，否则抛错。

    ① insert 的返回条数与预期一致（RPC 层就没写全 ⇒ 直接抛错）；
    ② 服务端回 auto-id 的条数与写入行数一致（否则无法逐行校验 ⇒ 直接抛错）；
    ③ 被删的旧 auto-id 已不可见（不刷新就复扫会「看得见」已删行）；
    ④ 新插入的行已可见，且**逐行**带本次期望的 external_id。

    缺任何一条都抛错 —— 绝不返回可能撒谎的 written。
    """
    expectedRows = len(pendingAutoIds)
    if insertCount != expectedRows:
        raise BackfillContractError(
            f"Milvus insert 条数不符：collection={collectionName} "
            f"期望 {expectedRows} 行，实际 {insertCount} 行 ⇒ 拒绝返回 written"
        )
    if len(newIds) != expectedRows:
        raise BackfillContractError(
            f"Milvus insert 返回的 primary_keys 数量不符：collection={collectionName} "
            f"期望 {expectedRows} 个，实际 {len(newIds)} 个 ⇒ 无法逐行校验 ⇒ 拒绝返回 written"
        )

    pairs = list(zip(newIds, expectedUids, strict=True))
    gap = VisibilityGap([], [], [])
    for attempt in range(_RESIDUE_CHECK_ATTEMPTS):
        rows = queryRows(collectionName, _META_FIELDS, consistencyLevel=_STRONG_CONSISTENCY)
        gap = checkVisibility(rows, pendingAutoIds, pairs)
        if gap.isEmpty:
            return
        if attempt < _RESIDUE_CHECK_ATTEMPTS - 1:
            logger.warning(
                "collection=%s: 回填尚未可见（已删行仍可见 %d、新行不可见 %d、uid 不符 %d），重试 %d/%d",
                collectionName, len(gap.residue), len(gap.missingNew),
                len(gap.wrongUid),
                attempt + 1, _RESIDUE_CHECK_ATTEMPTS,
            )
            await asyncio.sleep(_RESIDUE_CHECK_INTERVAL_SECONDS)

    raise BackfillContractError(
        f"Milvus 回填未生效：collection={collectionName} {gap.describe()}"
        f"⇒ 幂等契约不成立，拒绝返回 written"
    )


def deleteByIds(collection: Collection, autoIds: list[int]) -> None:
    """批量 delete（``id in [...]``）—— 把危险窗口从 N 次往返压到 1 次。"""
    for start in range(0, len(autoIds), _DELETE_BATCH_SIZE):
        chunk = autoIds[start : start + _DELETE_BATCH_SIZE]
        collection.delete("id in [" + ", ".join(str(i) for i in chunk) + "]")


async def applyBackfill(
    collection: Collection,
    collectionName: str,
    autoIds: list[int],
    expectedUids: list[str],
    columns: dict[str, list],
) -> None:
    """delete → insert → flush → release+load → 复扫校验。

    delete 与 insert 都发出之后才 flush；flush 之后才 release+load ——
    先落盘再重装，QueryNode 才会同时看到「旧行已删」与「新行已插」。
    """
    deleteByIds(collection, autoIds)
    result = collection.insert([columns[field] for field in _COLUMN_FIELDS])
    collection.flush()
    refreshCollection(collection)
    await assertBackfillApplied(
        collectionName,
        autoIds,
        expectedUids,
        insertCount=result.insert_count,
        newIds=list(result.primary_keys),
    )
