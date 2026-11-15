"""P3a D19：v2 集合写入（**双写的一侧**）。

双写的口径（与 D18 四步方案一致）
--------------------------------
- **开关关 ⇒ 一个字节都不写 v2**：v1 保持今天的唯一真源，P3a 整体未启用
  ⇒ 读路径逐字节等于今天、写路径也逐字节等于今天；
- 开关开 ⇒ 每条 embedding 同时写 v1 与 v2（v2 多带治理维度）；
- v2 写入**先删后插**（幂等覆盖），与 v1 的 delete+insert 同口径；
- v2 写入失败**只告警不抛**（best-effort）：v2 是影子副本，挂了不该拖垮 CRUD；
  真正「能不能用 v2 召回」由读路径闸门 fail-closed 兜底。

⚠️ 与既有 ``insertEmbeddingsDual`` 的命名区分
----------------------------------------------
``milvus_dual_write.insertEmbeddingsDual`` 里的 "dual write" 是**早期
「单集合 → 按类型拆三集合」的迁移遗留命名**，**不是** v1/v2 双写。
本模块才是 P3a 的 v1/v2 双写。

⚠️ 维度 ≠ 授权
--------------
写入时解析的治理维度只是「这条向量行属于哪些域/组织/实体」，**不决定谁能看到它**。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.infrastructure.database import getSessionFactory
from app.infrastructure.milvus_registry_v2 import deleteRegistryV2, insertRegistryV2
from app.services.ontology_dimension_service import (
    DimensionSet,
    resolveDimensions,
)

logger = logging.getLogger(__name__)


def registryV2WriteEnabled() -> bool:
    """P3a 开关（``ENABLE_SEMANTIC_REGISTRY_RAG``，默认关）。

    与读路径**同一个**开关：开 ⇒ 双写 + 读 v2；关 ⇒ 只写 v1、只读 v1。
    （读与写用两个开关会出现「读 v2 但没写 v2」的错配态 ⇒ 必然空召回。）
    """
    return bool(getSettings().enableSemanticRegistryRag)


async def buildRegistryV2Records(
    session: AsyncSession,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """给记录补齐治理维度字段。

    Returns:
        **新记录**（原记录不动 —— 调用方常复用同一批 record 再写 v1）。

    查不到治理事实 ⇒ 该条写空维度（显式「未归域」），**不猜**。
    """
    byType: dict[str, list[int]] = {}
    for record in records:
        type_ = str(record.get("type") or "")
        if type_:
            byType.setdefault(type_, []).append(int(record["ontology_id"]))

    dimsById: dict[tuple[str, int], DimensionSet] = {}
    for type_, ids in byType.items():
        resolved = await resolveDimensions(session, type_, sorted(set(ids)))
        for entityId, dim in resolved.items():
            dimsById[(type_, entityId)] = dim

    prepared: list[dict[str, Any]] = []
    for record in records:
        type_ = str(record.get("type") or "")
        dim = dimsById.get((type_, int(record["ontology_id"])), DimensionSet())
        prepared.append(
            {
                **record,
                "domain_codes": dim.domainCodes,
                "org_codes": dim.orgCodes,
                "entity_type": dim.entityType,
                "valid_from": dim.validFrom,
                "valid_to": dim.validTo,
                "release_id": dim.releaseId,
            }
        )
    return prepared


async def syncRegistryV2Embeddings(
    records: list[dict[str, Any]],
    *,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """把一批 embedding 写入 v2 集合（先删后插，幂等覆盖）。

    Args:
        records: 与 v1 写入同构的记录（``ontology_id``/``type``/``name``/…）。
        session: 解析治理维度用的会话；``None`` ⇒ 自开一个短会话
            （短会话看不到未提交的行 ⇒ 调用方应在**提交后**调用本函数）。

    Returns:
        ``{"written": n, "failed": n, "skipped": bool}``；**永不抛异常**
        （v2 是影子副本，失败只告警 + 计数）。
    """
    if not records:
        return {"written": 0, "failed": 0, "skipped": False}
    if not registryV2WriteEnabled():
        return {"written": 0, "failed": 0, "skipped": True}

    try:
        prepared = await _prepareRecords(records, session=session)
    except Exception as exc:  # noqa: BLE001 —— 维度解析失败不该拖垮 CRUD
        logger.error("语义注册表 v2：治理维度解析失败（%d 条）：%s", len(records), exc)
        return {"written": 0, "failed": len(records), "skipped": False}

    try:
        # Milvus 是**同步 SDK** ⇒ 阻塞调用一律移出事件循环
        # （与 kpi_vector_graph_sync 的 Milvus 临界区同口径）。
        for record in prepared:
            await asyncio.to_thread(
                deleteRegistryV2, int(record["ontology_id"]), str(record["type"])
            )
        await asyncio.to_thread(insertRegistryV2, prepared)
    except Exception as exc:  # noqa: BLE001 —— 写入失败只告警
        logger.error("语义注册表 v2：写入失败（%d 条）：%s", len(prepared), exc)
        return {"written": 0, "failed": len(prepared), "skipped": False}

    logger.info("语义注册表 v2：写入 %d 条", len(prepared))
    return {"written": len(prepared), "failed": 0, "skipped": False}


async def deleteRegistryV2Embedding(ontologyId: int, type_: str) -> None:
    """v2 删除（best-effort）。

    开关关 ⇒ **不碰 v2**（P3a 整体未启用 ⇒ 不产生任何 v2 侧副作用）。
    删除失败只告警（影子副本删不掉不该拖垮 CRUD；读路径闸门 fail-closed 兜底）。
    """
    if not registryV2WriteEnabled():
        return
    try:
        await asyncio.to_thread(deleteRegistryV2, ontologyId, type_)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "语义注册表 v2：删除失败 id=%d type=%s：%s", ontologyId, type_, exc
        )


async def _prepareRecords(
    records: list[dict[str, Any]],
    *,
    session: AsyncSession | None,
) -> list[dict[str, Any]]:
    """补齐治理维度（会话由调用方给或自开短会话）。"""
    if session is not None:
        return await buildRegistryV2Records(session, records)
    async with getSessionFactory()() as ownSession:
        return await buildRegistryV2Records(ownSession, records)
