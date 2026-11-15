"""P3a D19：语义注册表 RAG 闸门（开关 + v1/v2 路由 + 降级口径）。

闸门是向量召回的**唯一出口**（与 P2 的 `semantic_schema_gate` 同款架构）：
调用方只说「要什么」，由闸门决定「读哪个集合、带不带过滤」。

三条不可让步的口径
------------------
1. **开关关 ⇒ 与今天逐字节一致**：读 v1 集合、不带任何过滤、不碰 v2。
   过滤是 P3a 新增的**能力**，开关关 = 这项能力整体未启用（不是「启用但不生效」）。
   调用方在开关关时传了过滤器 ⇒ 记 warning + ``degradedReason`` 可观测
   （否则「以为在按域收窄、其实全量召回」是个静默的口径失真）。
2. **有作用域时，v2 不可用 ⇒ fail-closed（空结果），不许回落成无过滤的 v1**。
   回落 = 把越权行放进来 = 泄漏。宁可召回为空（上层有 fallback 路径），
   也不许用「更宽的答案」冒充「按权限过滤的答案」。
3. **无作用域时，v2 不可用 ⇒ 回落 v1**（此时 v1 与 v2 语义等价，回落不改变可见性）。

⚠️ 过滤 ≠ 授权
--------------
本闸门**按调用方给的维度收窄**，不判断「调用方有没有权限看这些维度」。
仓内至今没有「用户 → 可见域/组织」的策略源（现有 ACL 只有 admin 级的
``assertCanModify``）⇒ 维度由上层显式传入；策略接线是另一次受控变更。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.config import getSettings
from app.domain.registry_filter import RegistryFilter
from app.infrastructure import milvus_client as milvus
from app.infrastructure.milvus_registry_v2 import (
    buildRegistryFilterExpr,
    hasRegistryV2Collection,
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
        degradedReason: 降级原因（``None`` = 未降级）。取值见模块文档。
    """

    hits: list[dict[str, Any]]
    usedRegistryV2: bool
    degradedReason: str | None = None


def registryRagEnabled() -> bool:
    """P3a 开关（``ENABLE_SEMANTIC_REGISTRY_RAG``，默认关）。"""
    return bool(getSettings().enableSemanticRegistryRag)


def searchRegistryEmbeddings(
    queryEmbedding: list[float],
    topK: int = 5,
    typeFilter: str | None = None,
    registryFilter: RegistryFilter | None = None,
) -> RegistrySearchResult:
    """向量召回的唯一出口（带治理维度过滤）。

    Args:
        queryEmbedding: 查询向量。
        topK: 返回条数。
        typeFilter: 限定类型（class/property/metric/kpi；``None`` = 全部）。
        registryFilter: 过滤维度（``None`` 或全空 = 不收窄）。

    Returns:
        ``RegistrySearchResult``；**永不抛异常**（向量检索失败降级成空结果
        或 v1 回落），降级痕迹落在 ``degradedReason`` 上。
    """
    if not registryRagEnabled():
        return _searchV1(queryEmbedding, topK, typeFilter, registryFilter)
    return _searchV2(queryEmbedding, topK, typeFilter, registryFilter)


# ---------------------------------------------------------------------------
# v1 路径（开关关 / 无作用域降级）
# ---------------------------------------------------------------------------


def _searchV1(
    queryEmbedding: list[float],
    topK: int,
    typeFilter: str | None,
    registryFilter: RegistryFilter | None,
) -> RegistrySearchResult:
    """读 v1 集合（不过滤）。

    开关关却带了过滤器 ⇒ 口径失真（以为按域收窄、实际全量）⇒ 必须可观测。
    """
    degraded: str | None = None
    if registryFilter is not None and registryFilter.hasScope():
        degraded = "switch_off_with_filter"
        logger.warning(
            "语义注册表 RAG 开关关闭，调用方却带了过滤维度（域/组织/实体）⇒ "
            "本次召回未按维度收窄（口径失真）。去重维度：domain=%s org=%s entity=%s",
            list(registryFilter.domainCodes),
            list(registryFilter.orgCodes),
            list(registryFilter.entityTypes),
        )
    hits = milvus.searchEmbeddingsByTypeRouted(
        queryEmbedding, topK=topK, typeFilter=typeFilter
    )
    return RegistrySearchResult(hits=hits, usedRegistryV2=False, degradedReason=degraded)


# ---------------------------------------------------------------------------
# v2 路径（开关开）
# ---------------------------------------------------------------------------


def _searchV2(
    queryEmbedding: list[float],
    topK: int,
    typeFilter: str | None,
    registryFilter: RegistryFilter | None,
) -> RegistrySearchResult:
    """读 v2 集合（可带过滤表达式）。"""
    hasScope = registryFilter is not None and registryFilter.hasScope()

    expr: str | None
    try:
        expr = buildRegistryFilterExpr(registryFilter)
    except Exception as exc:  # noqa: BLE001 —— 表达式构造失败一律降级，不炸上层
        logger.error("语义注册表 RAG 过滤表达式构造失败：%s", exc, exc_info=True)
        if hasScope:
            return RegistrySearchResult(hits=[], usedRegistryV2=False, degradedReason="filter_invalid")
        return _fallbackToV1(queryEmbedding, topK, typeFilter, "filter_invalid")

    missing = _missingV2Types(typeFilter)
    if missing:
        logger.warning(
            "语义注册表 RAG：v2 集合缺失（%s）", ", ".join(missing)
        )
        if hasScope:
            # 有作用域却读不到 v2 ⇒ fail-closed：绝不用「不过滤的 v1」冒充
            # 「按维度过滤的答案」（那等于把越权行放进来）。
            return RegistrySearchResult(hits=[], usedRegistryV2=False, degradedReason="v2_missing")
        return _fallbackToV1(queryEmbedding, topK, typeFilter, "v2_missing")

    try:
        hits = searchRegistryV2(
            queryEmbedding, topK=topK, typeFilter=typeFilter, expr=expr
        )
    except Exception as exc:  # noqa: BLE001 —— Milvus 异常降级，不炸上层
        logger.error("语义注册表 RAG：v2 检索失败：%s", exc, exc_info=True)
        if hasScope:
            return RegistrySearchResult(hits=[], usedRegistryV2=False, degradedReason="v2_error")
        return _fallbackToV1(queryEmbedding, topK, typeFilter, "v2_error")

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
) -> RegistrySearchResult:
    """无作用域时的 v1 回落（此时 v1/v2 语义等价，回落不改变可见性）。"""
    hits = milvus.searchEmbeddingsByTypeRouted(
        queryEmbedding, topK=topK, typeFilter=typeFilter
    )
    return RegistrySearchResult(hits=hits, usedRegistryV2=False, degradedReason=reason)
