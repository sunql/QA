"""P3a D19：语义注册表 RAG 闸门（开关 + v1/v2 路由 + 降级口径）。

闸门是向量召回的**唯一出口**（与 P2 的 `semantic_schema_gate` 同款架构）：
调用方只说「要什么」，由闸门决定「读哪个集合、带不带过滤」。

三条不可让步的口径
------------------
1. **开关关 且 调用方未声明作用域 ⇒ 与今天逐字节一致**：读 v1 集合、
   不带任何过滤、不碰 v2。过滤是 P3a 新增的**能力**，开关关 = 这项能力
   整体未启用（不是「启用但不生效」）。
   ⚠️ 反过来：**调用方在开关关时声明了作用域** ⇒ fail-closed（空结果）——
   这条路径没有能力施加收窄，回落成「不过滤的 v1」= 泄漏。
2. **有作用域时，v2 不可用 ⇒ fail-closed（空结果），不许回落成无过滤的 v1**。
   回落 = 把越权行放进来 = 泄漏。宁可召回为空（上层有 fallback 路径），
   也不许用「更宽的答案」冒充「按权限过滤的答案」。
3. **无作用域时，v2 不可用 ⇒ 回落 v1**（此时 v1 与 v2 语义等价，回落不改变可见性）。

「有作用域」的判定（D21R2）
---------------------------
**过滤器的存在本身就是声明**：``registryFilter=None`` ⇒ 无作用域；
任何 ``RegistryFilter`` 实例 ⇒ 已声明作用域（各维度取值为空 ⇒ deny-all）。
⇒ 不存在「忘了写标志位 ⇒ 变成全量可见」的可能状态。

降级原因（``degradedReason``）取值
----------------------------------
====================== ============================================
取值                    含义（**都是「拒绝」或「降级」，不是「放行」**）
====================== ============================================
``switch_off_denied``   开关关，但调用方声明了作用域 ⇒ 拒绝（空结果）
``filter_invalid``      过滤表达式构造失败（非法字面量 / 非法发布 id）⇒ 拒绝
``scope_denied``        声明了作用域但各维度取值为空 ⇒ 拒绝（deny-all）
``v2_missing``          v2 集合缺失（有作用域⇒拒绝；无作用域⇒回落 v1）
``v2_error``            v2 检索异常（有作用域⇒拒绝；无作用域⇒回落 v1）
====================== ============================================

⚠️ 过滤 ≠ 授权
--------------
本闸门**按调用方给的维度收窄**，不判断「调用方有没有权限看这些维度」。
仓内至今没有「用户 → 可见域/组织」的策略源（现有 ACL 只有 admin 级的
``assertCanModify``）⇒ 维度由上层显式传入；策略接线是另一次受控变更。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from app.config import getSettings
from app.domain.registry_filter import RegistryFilter
from app.infrastructure import milvus_client as milvus
from app.infrastructure.milvus_registry_v2 import (
    buildRegistryFilterExpr,
    hasRegistryV2Collection,
    registryV2DimensionedRowCount,
    registryV2RowCount,
    requiredRegistryV2Types,
    searchRegistryV2,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegistrySearchResult:
    """一次向量召回的结果 + 可观测的降级痕迹。

    Attributes:
        hits: 命中列表（结构与 v1 一致：ontology_id/type/name/alias/description/distance）。
        usedRegistryV2: 是否读的是 v2 集合。
        degradedReason: 降级原因（``None`` = 未降级）。取值表见模块文档。
    """

    hits: list[dict[str, Any]]
    usedRegistryV2: bool
    degradedReason: str | None = None


@dataclass(frozen=True)
class RegistryV2Readiness:
    """某个 v2 集合的就绪度快照（只读探针的结果）。

    Attributes:
        type_: 集合类型（class/property/metric）。
        rows: **物理**行数（含墓碑）；``None`` = 探不到（Milvus 不可达 / 异常）。
        dimensionedRows: **带任一治理维度的可见行数**；``None`` = 探不到。
    """

    type_: str
    rows: int | None
    dimensionedRows: int | None


def registryRagEnabled() -> bool:
    """P3a 开关（``ENABLE_SEMANTIC_REGISTRY_RAG``，默认关）。"""
    return bool(getSettings().enableSemanticRegistryRag)


def registryRagReadiness() -> dict[str, RegistryV2Readiness]:
    """v2 集合的就绪度快照（**只读探针**；开关打开时用于启动期告警）。

    为什么需要：v2 缺失或为空 ⇒ 带过滤的召回**安静地**返回空 ⇒
    用户看到的是「查不到实体」，没有任何降级痕迹 ⇒ 排查成本极高
    （且容易被误判成「模型没召回」）。故在启动期探一次，把未就绪喊出来。

    为什么还要报「带维度的行数」：物理行数 > 0 也可能是「行在、维度全空」⇒
    带过滤的召回**恒空** ⇒ 只看行数的探针会误报「就绪」⇒ 又是静默零召回。

    Returns:
        ``{类型: 就绪度快照}``；探不到（异常）时 ``rows``/``dimensionedRows``
        为 ``None``（与「集合为空（0）」区分开 —— 两种情形的处置完全不同）。
    """
    readiness: dict[str, RegistryV2Readiness] = {}
    for type_ in requiredRegistryV2Types(None):
        rows: int | None
        try:
            rows = registryV2RowCount(type_)
        except Exception as exc:  # noqa: BLE001 —— 探针不许把启动带崩
            logger.warning("语义注册表 RAG 就绪探针失败（%s）：%s", type_, exc)
            readiness[type_] = RegistryV2Readiness(type_=type_, rows=None, dimensionedRows=None)
            continue

        dimensionedRows: int | None
        try:
            dimensionedRows = registryV2DimensionedRowCount(type_)
        except Exception as exc:  # noqa: BLE001 —— 同上
            logger.warning("语义注册表 RAG 维度探针失败（%s）：%s", type_, exc)
            dimensionedRows = None

        readiness[type_] = RegistryV2Readiness(
            type_=type_, rows=rows, dimensionedRows=dimensionedRows
        )
    return readiness


def searchRegistryEmbeddings(
    queryEmbedding: list[float],
    topK: int = 5,
    typeFilter: str | None = None,
    registryFilter: RegistryFilter | None = None,
    classOntologyIds: Sequence[int] | None = None,
) -> RegistrySearchResult:
    """向量召回的唯一出口（带治理维度过滤）。

    Args:
        queryEmbedding: 查询向量。
        topK: 返回条数。
        typeFilter: 限定类型（class/property/metric/kpi；``None`` = 全部）。
        registryFilter: 过滤维度。``None`` = **本轮无作用域**（不收窄）；
            传了对象 = **已声明作用域**（各维度取值为空 ⇒ deny-all）。
        classOntologyIds: **本体类 id 作用域**（Gap A）：只保留这些本体类的向量。
            ``None`` = 不限（与今天逐字节一致）；空集 = deny-all（恒空）。
            **只作用于 class 集合**。

    Returns:
        ``RegistrySearchResult``；**永不抛异常**（向量检索失败降级成空结果
        或 v1 回落），降级痕迹落在 ``degradedReason`` 上。
    """
    if not registryRagEnabled():
        return _searchV1(queryEmbedding, topK, typeFilter, registryFilter, classOntologyIds)
    return _searchV2(queryEmbedding, topK, typeFilter, registryFilter, classOntologyIds)


# ---------------------------------------------------------------------------
# v1 路径（开关关 / 无作用域降级）
# ---------------------------------------------------------------------------


def _searchV1(
    queryEmbedding: list[float],
    topK: int,
    typeFilter: str | None,
    registryFilter: RegistryFilter | None,
    classOntologyIds: Sequence[int] | None = None,
) -> RegistrySearchResult:
    """读 v1 集合（不过滤）。

    开关关却带了作用域 ⇒ **fail-closed（空结果）**：这条路径没有能力施加
    调用方声明的收窄，回落成「不过滤的 v1」= 把调用方本来不想看到的行
    全部放进来 = 泄漏。降级痕迹落在 ``degradedReason`` 上（可观测）。

    **本体类 id 作用域不走这条 fail-closed 规则**：那条规则针对**治理维度**
    （域/组织/实体/发布/有效期，越权 = 泄漏）；作用域是**数据源相关性**
    （不收窄只是窗口偏宽，不是越权）⇒ v1 照常按作用域过滤检索。
    """
    if registryFilter is not None:
        logger.warning(
            "语义注册表 RAG 开关关闭，调用方却声明了作用域（域/组织/实体/发布/有效期）⇒ "
            "本次召回 fail-closed（空结果），不回落成不过滤的 v1。"
            "维度：domain=%s org=%s entity=%s release=%s validAt=%s",
            list(registryFilter.domainCodes),
            list(registryFilter.orgCodes),
            list(registryFilter.entityTypes),
            registryFilter.releaseId,
            registryFilter.validAt,
        )
        return RegistrySearchResult(
            hits=[], usedRegistryV2=False, degradedReason="switch_off_denied"
        )
    hits = _routedSearch(queryEmbedding, topK, typeFilter, classOntologyIds)
    return RegistrySearchResult(hits=hits, usedRegistryV2=False, degradedReason=None)


def _routedSearch(
    queryEmbedding: list[float],
    topK: int,
    typeFilter: str | None,
    classOntologyIds: Sequence[int] | None,
) -> list[dict[str, Any]]:
    """调 v1 检索；**没有作用域时调用形态逐字不变**（不多传 kwarg）。

    这样「无作用域 = 与今天完全一致」这条不变量在**调用形态**上也成立：
    既有替身（只认 ``(queryEmbedding, topK, typeFilter)``）不必改，
    下游任何按位置/关键字解包的实现也不会被多出来的参数打断。
    """
    if classOntologyIds is None:
        return milvus.searchEmbeddingsByTypeRouted(
            queryEmbedding, topK=topK, typeFilter=typeFilter
        )
    return milvus.searchEmbeddingsByTypeRouted(
        queryEmbedding, topK=topK, typeFilter=typeFilter,
        classOntologyIds=classOntologyIds,
    )


# ---------------------------------------------------------------------------
# v2 路径（开关开）
# ---------------------------------------------------------------------------


def _searchV2(
    queryEmbedding: list[float],
    topK: int,
    typeFilter: str | None,
    registryFilter: RegistryFilter | None,
    classOntologyIds: Sequence[int] | None = None,
) -> RegistrySearchResult:
    """读 v2 集合（可带过滤表达式）。"""
    # 「有作用域」= 调用方给了过滤器（**存在即声明**）；``None`` = 无作用域。
    hasScope = registryFilter is not None

    expr: str | None
    try:
        expr = buildRegistryFilterExpr(registryFilter)
    except Exception as exc:  # noqa: BLE001 —— 表达式构造失败一律降级，不炸上层
        logger.error("语义注册表 RAG 过滤表达式构造失败：%s", exc, exc_info=True)
        if hasScope:
            return RegistrySearchResult(hits=[], usedRegistryV2=False, degradedReason="filter_invalid")
        return _fallbackToV1(queryEmbedding, topK, typeFilter, "filter_invalid", classOntologyIds)

    if hasScope and expr is None:
        # 「有作用域但构造不出表达式」= 调用方声明了作用域却没给任何取值
        # （例如空过滤器）⇒ 语义是 deny-all。
        # 放行（拿不过滤的 v2 结果）= 恰好在本该「什么都看不到」的一轮全量可见。
        logger.warning(
            "语义注册表 RAG：调用方声明了作用域但没有任何可过滤的取值 ⇒ "
            "本次召回 fail-closed（deny-all），不返回未过滤结果。"
        )
        return RegistrySearchResult(hits=[], usedRegistryV2=False, degradedReason="scope_denied")

    missing = _missingV2Types(typeFilter)
    if missing:
        logger.warning(
            "语义注册表 RAG：v2 集合缺失（%s）", ", ".join(missing)
        )
        if hasScope:
            # 有作用域却读不到 v2 ⇒ fail-closed：绝不用「不过滤的 v1」冒充
            # 「按维度过滤的答案」（那等于把越权行放进来）。
            return RegistrySearchResult(hits=[], usedRegistryV2=False, degradedReason="v2_missing")
        return _fallbackToV1(queryEmbedding, topK, typeFilter, "v2_missing", classOntologyIds)

    try:
        if classOntologyIds is None:
            # 无作用域时调用形态逐字不变（不多传 kwarg）。
            hits = searchRegistryV2(
                queryEmbedding, topK=topK, typeFilter=typeFilter, expr=expr
            )
        else:
            hits = searchRegistryV2(
                queryEmbedding, topK=topK, typeFilter=typeFilter, expr=expr,
                classOntologyIds=classOntologyIds,
            )
    except Exception as exc:  # noqa: BLE001 —— Milvus 异常降级，不炸上层
        logger.error("语义注册表 RAG：v2 检索失败：%s", exc, exc_info=True)
        if hasScope:
            return RegistrySearchResult(hits=[], usedRegistryV2=False, degradedReason="v2_error")
        return _fallbackToV1(queryEmbedding, topK, typeFilter, "v2_error", classOntologyIds)

    return RegistrySearchResult(hits=hits, usedRegistryV2=True, degradedReason=None)


def _missingV2Types(typeFilter: str | None) -> list[str]:
    """本次检索必需的 v2 集合里，缺哪些。"""
    try:
        required = requiredRegistryV2Types(typeFilter)
    except ValueError:
        return []
    return [type_ for type_ in required if not hasRegistryV2Collection(type_)]


def _fallbackToV1(
    queryEmbedding: list[float],
    topK: int,
    typeFilter: str | None,
    reason: str,
    classOntologyIds: Sequence[int] | None = None,
) -> RegistrySearchResult:
    """无作用域时的 v1 回落（此时 v1/v2 语义等价，回落不改变可见性）。"""
    hits = _routedSearch(queryEmbedding, topK, typeFilter, classOntologyIds)
    return RegistrySearchResult(hits=hits, usedRegistryV2=False, degradedReason=reason)
