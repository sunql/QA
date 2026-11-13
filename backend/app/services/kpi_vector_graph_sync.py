"""KPI 目录向量/图同步（feat-kpi-vector-graph-sync，设计 §3/§4）。

镜像 ontology_service 的 best-effort 同步模式：
- 向量：type="kpi" → kpi_catalog_embeddings（只索引 PUBLISHED，D3）；
- 图：KpiCatalog 节点（全状态，MERGE on unified_id）+ MEASURED_BY 边（metric 缺失不建边）；
- id_mapping：business_object="KPI"，unified_id=obj:KPI:{id}（零 DDL）。

D3 状态口径：向量只索引 PUBLISHED；流转到 DRAFT/DEPRECATED ⇒ 删向量、**图节点保留**
（status 属性原地更新）。metric 向量/图维持现状。

D4：无 outbox / 可靠队列 ⇒ 同步一律 best-effort（失败仅 warning，不影响主流程）。

MEASURED_BY 语义（钩子路径）：边集合必须与当前 `metric_id` **精确一致** ——
`metric_id` 变更或清空后，旧边必须被删掉，否则图里会出现「同一 KPI 被两个指标
测量」的矛盾（且图是喂给 LLM 的语义源）。对账路径按计划「只补缺失」执行，
用 linkKpiMeasuredBy 的幂等加边语义。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog, OntologyMetric
from app.domain.schemas import IdMappingCreate
from app.infrastructure import neo4j_client as neo4j

# 从 milvus_client 取（不是 milvus_dual_write）：milvus_client 在**文件末尾**
# re-export milvus_dual_write 的同名函数（避开循环 import），若先 import
# milvus_dual_write → 它回头 import milvus_client → milvus_client 末尾又 import
# 尚未定义完的 milvus_dual_write ⇒ ImportError（见 milvus_client.py:775 注释）。
from app.infrastructure.milvus_client import (
    deleteByOntologyIdDual,
    insertEmbeddingsDual,
)
from app.services.embedding_service import EmbeddingService
from app.services.id_mapping_service import IdMappingService

logger = logging.getLogger(__name__)

KPI_BUSINESS_OBJECT = "KPI"
KPI_NEO4J_LABEL = "KpiCatalog"
KPI_PG_TABLE = "kpi_catalog"
KPI_VECTOR_TYPE = "kpi"
METRIC_BUSINESS_OBJECT = "METRIC"
PUBLISHED_STATUS = "PUBLISHED"

# 后台同步任务的强引用（create_task 弱引用会被 GC，需持握防丢失）
_PENDING_KPI_SYNC_TASKS: set[asyncio.Task] = set()

# Milvus 写入串行化锁：pymilvus 的 insert/delete 之后必须 flush（单次 8-25s），
# N 个后台任务并发 flush 同一集合会拖垮 Milvus（同
# OntologyService.syncMissingClassEmbeddingsBestEffort 的教训）。
# 一次 KPI 的「删 + 插」构成一个临界区。
#
# asyncio.Lock 在 Python 3.10+ 不在构造时绑定 loop（3.12+ 本仓要求），但它**绑定
# 首次使用它的那个 loop**；跨 loop 使用（如 pytest-asyncio 每测试一个新 loop）
# 会抛 RuntimeError。故按「当前运行 loop」缓存锁对象：生产是单 worker 单 loop
# ⇒ 始终复用同一把；测试多 loop ⇒ 每个 loop 各一把（互斥语义不跨 loop，也不需要）。
_MILVUS_WRITE_LOCK: asyncio.Lock | None = None
_MILVUS_WRITE_LOCK_LOOP: asyncio.AbstractEventLoop | None = None


def _milvusWriteLock() -> asyncio.Lock:
    """取当前事件循环的 Milvus 写入锁（见 _MILVUS_WRITE_LOCK 的说明）。"""
    global _MILVUS_WRITE_LOCK, _MILVUS_WRITE_LOCK_LOOP  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _MILVUS_WRITE_LOCK is None or _MILVUS_WRITE_LOCK_LOOP is not loop:
        _MILVUS_WRITE_LOCK = asyncio.Lock()
        _MILVUS_WRITE_LOCK_LOOP = loop
    return _MILVUS_WRITE_LOCK


def kpiEmbeddingText(kpi: KpiCatalog, metricAlias: str | None) -> str:
    """KPI 向量文本：名称 + 挂靠指标别名 + 语义关键词 + 业务定义 + 单位（设计 §3.1）。"""
    return " ".join(
        filter(
            None,
            [
                kpi.kpi_name,
                metricAlias or "",
                *(kpi.semantic_keywords or []),
                kpi.business_definition or "",
                kpi.unit or "",
            ],
        )
    )


async def _loadKpiWithMetricAlias(
    session: AsyncSession, kpiId: int
) -> tuple[KpiCatalog | None, str | None]:
    """加载 KPI 行 + 挂靠 metric 别名（outerjoin，metric 缺失/无别名返 None）。"""
    stmt = (
        select(KpiCatalog, OntologyMetric.metric_alias)
        .outerjoin(OntologyMetric, OntologyMetric.id == KpiCatalog.metric_id)
        .where(KpiCatalog.id == kpiId)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None, None
    return row[0], row[1]


async def _resolveKpiUid(
    session: AsyncSession, idMappingSvc: IdMappingService, kpi: KpiCatalog
) -> str:
    """解析/注册 KPI unified_id（resolveByExternal → register 兜底，镜像 syncMissingGraph 模式）。

    ⚠️ **register 内部会 commit**（IdMappingService.register 的既有行为）⇒
    传入请求链路的事务 session 会**提前提交请求事务**。请求链路请一律走
    `syncKpiGraphBestEffort` / `deleteKpiGraphBestEffort`（自开 session）。
    """
    row = await idMappingSvc.resolveByExternal(
        session, KPI_BUSINESS_OBJECT, str(kpi.id)
    )
    if row is not None:
        return row.unified_id
    mapping = await idMappingSvc.register(
        session,
        IdMappingCreate(business_object=KPI_BUSINESS_OBJECT, external_id=str(kpi.id)),
        pg_table=KPI_PG_TABLE,
        pg_id=str(kpi.id),
    )
    return mapping.unified_id


async def _resolveMetricUid(
    session: AsyncSession, idMappingSvc: IdMappingService, metricId: int | None
) -> str | None:
    """挂靠指标的 unified_id；无 metric_id / 指标未入 id_mapping ⇒ None（不建边）。"""
    if metricId is None:
        return None
    row = await idMappingSvc.resolveByExternal(
        session, METRIC_BUSINESS_OBJECT, str(metricId)
    )
    return row.unified_id if row is not None else None


async def syncKpiGraphNow(session: AsyncSession, kpiId: int) -> str:
    """幂等建/更新 KpiCatalog 节点 + MEASURED_BY 边；返回 unified_id。

    图节点**全状态都建**（D3）：DRAFT/DEPRECATED 同样建节点，status 属性原地更新。
    MEASURED_BY 边集合被置为与当前 `metric_id` 精确一致（先删后建，同一事务）。

    ⚠️ **本函数可能写库并 commit**：首次遇到未注册的 KPI 时会调
    `IdMappingService.register`，而 register **内部会 commit** ⇒ 传入请求链路的
    事务 session 会提前提交请求事务。请求链路请一律走
    `syncKpiGraphBestEffort(kpiId)`（自开 session + 后台任务）。
    """
    kpi = await session.get(KpiCatalog, kpiId)
    if kpi is None:
        raise ValueError(f"KPI 不存在: id={kpiId}")
    idMappingSvc = IdMappingService()
    unifiedId = await _resolveKpiUid(session, idMappingSvc, kpi)
    neo4j.upsertKpiCatalogNode(
        unified_id=unifiedId,
        code=kpi.kpi_code,
        name=kpi.kpi_name,
        status=kpi.status,
        owner=kpi.owner,
        unit=kpi.unit,
    )
    metricUid = await _resolveMetricUid(session, idMappingSvc, kpi.metric_id)
    neo4j.setKpiMeasuredBy(unifiedId, metricUid)
    return unifiedId


async def syncKpiVectorNow(session: AsyncSession, kpiId: int) -> None:
    """单条向量重建：删旧 → PUBLISHED 才重插（D3：DRAFT/DEPRECATED 只删不插）。

    Milvus 是**同步 SDK**（insert/delete 之后还要 flush，单次 8-25s）⇒ 两个调用
    都经 `asyncio.to_thread` 移出事件循环，**不得**在 API worker 的事件循环线程上
    直接调用（否则一次 KPI 写入会堵住该 worker 上的所有其它请求）。

    并发串行化由调用方负责：走 best-effort 入口时锁在 `_runVectorOwnSession`
    持有（一次 KPI 的「删 + 插」= 一个临界区）；**直接**调本函数（admin 对账 /
    回填脚本）不在锁内，调用方需自行保证不并发 flush 同一集合。
    """
    kpi, metricAlias = await _loadKpiWithMetricAlias(session, kpiId)
    if kpi is None:
        raise ValueError(f"KPI 不存在: id={kpiId}")
    await asyncio.to_thread(deleteByOntologyIdDual, kpi.id, KPI_VECTOR_TYPE)
    if kpi.status != PUBLISHED_STATUS:
        logger.info(
            "KPI id=%d status=%s 非 PUBLISHED，向量保持删除", kpiId, kpi.status
        )
        return
    vec = await EmbeddingService().generateEmbedding(
        kpiEmbeddingText(kpi, metricAlias)
    )
    await asyncio.to_thread(insertEmbeddingsDual, [{
        "ontology_id": kpi.id,
        "type": KPI_VECTOR_TYPE,
        "name": kpi.kpi_name,
        "alias": metricAlias or "",
        "description": kpi.business_definition or "",
        "embedding": vec,
        "external_id": kpi.kpi_code,
    }])


async def deleteKpiVectorNow(kpiId: int) -> None:
    """删该 KPI 的向量（Milvus type-routed 集合，按 ontology_id=kpiId 定位）。

    同 syncKpiVectorNow：阻塞式 Milvus 调用走 `asyncio.to_thread`，不占事件循环。
    """
    await asyncio.to_thread(deleteByOntologyIdDual, kpiId, KPI_VECTOR_TYPE)


async def deleteKpiGraphNow(session: AsyncSession, kpiId: int) -> None:
    """删图节点（无 unified_id 映射时无事可做——从未入图）。

    ⚠️ 本函数**只读** id_mapping（不 register ⇒ 不 commit）⇒ 可以安全地接受请求
    链路的 session；但请求链路仍建议走 `deleteKpiGraphBestEffort(kpiId)`
    （自开 session，不占用请求事务）。
    """
    row = await IdMappingService().resolveByExternal(
        session, KPI_BUSINESS_OBJECT, str(kpiId)
    )
    if row is None:
        return
    neo4j.deleteNode(KPI_NEO4J_LABEL, row.unified_id)


# ---------------------------------------------------------------------------
# best-effort 装配（钩子用；各自开 session，失败仅告警）
# ---------------------------------------------------------------------------

# 后台任务的操作名（进日志，用于定位「哪条 KPI 的哪一步失败」）
OP_GRAPH = "graph"
OP_VECTOR = "vector"
OP_DELETE_VECTOR = "delete-vector"
OP_DELETE_GRAPH = "delete-graph"


async def _runQuietly(
    coro: Coroutine[Any, Any, None], *, kpiId: int, operation: str
) -> None:
    """跑后台协程并吞掉异常（D4：同步失败只告警，不影响主流程）。"""
    try:
        await coro
    except Exception:
        logger.warning(
            "KPI 向量/图后台同步失败 operation=%s kpiId=%s",
            operation,
            kpiId,
            exc_info=True,
        )


def _spawn(coro: Coroutine[Any, Any, None], *, kpiId: int, operation: str) -> None:
    task = asyncio.create_task(_runQuietly(coro, kpiId=kpiId, operation=operation))
    _PENDING_KPI_SYNC_TASKS.add(task)
    task.add_done_callback(_PENDING_KPI_SYNC_TASKS.discard)


def _ownSession() -> AsyncSession:
    from app.infrastructure.database import getSessionFactory

    return getSessionFactory()()


async def _runGraphOwnSession(kpiId: int) -> None:
    async with _ownSession() as session:
        await syncKpiGraphNow(session, kpiId)


async def syncKpiGraphBestEffort(kpiId: int) -> None:
    """图同步 best-effort 入口（自开 session + 后台任务；失败仅告警）。"""
    _spawn(_runGraphOwnSession(kpiId), kpiId=kpiId, operation=OP_GRAPH)


async def _runVectorOwnSession(kpiId: int) -> None:
    # Milvus 写入临界区：一次 KPI 的「删 + 插」不与别的 KPI 交错 flush。
    async with _milvusWriteLock(), _ownSession() as session:
        await syncKpiVectorNow(session, kpiId)


async def syncKpiVectorBestEffort(kpiId: int) -> None:
    """向量同步 best-effort 入口（自开 session + 后台任务；失败仅告警）。"""
    _spawn(_runVectorOwnSession(kpiId), kpiId=kpiId, operation=OP_VECTOR)


async def _runDeleteVector(kpiId: int) -> None:
    async with _milvusWriteLock():
        await deleteKpiVectorNow(kpiId)


async def deleteKpiVectorBestEffort(kpiId: int) -> None:
    """向量删除 best-effort 入口（纯 Milvus 操作，无需 DB session）。"""
    _spawn(_runDeleteVector(kpiId), kpiId=kpiId, operation=OP_DELETE_VECTOR)


async def _runDeleteGraphOwnSession(kpiId: int) -> None:
    async with _ownSession() as session:
        await deleteKpiGraphNow(session, kpiId)


async def deleteKpiGraphBestEffort(kpiId: int) -> None:
    """图节点删除 best-effort 入口（自开 session + 后台任务；失败仅告警）。"""
    _spawn(_runDeleteGraphOwnSession(kpiId), kpiId=kpiId, operation=OP_DELETE_GRAPH)
