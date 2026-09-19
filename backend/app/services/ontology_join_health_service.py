"""本体 JOIN 关系健康巡检（三层保障第 2 层：主动巡检）。

背景（2026-09-18 DIM_SUPPLIER 孤岛事故）：零边类只在用户提问报
「无法通过关联路径连通」时才暴露——缺的是主动巡检。本模块提供：

- buildJoinHealthReport：孤岛类清单（零 JOIN 边，标注数仓分层 + FK 语义列）
- probeDeadEdges：死边检测（对业务库跑连接键值域探针，零交集/空列 → 死边）

死边比缺边更糟：validatePlan 放行但查询结果全空，且 LLM 会被诱导走空列路径。
探针设计：每列 `SELECT DISTINCT col FROM tbl WHERE col IS NOT NULL`（有界采样
PROBE_SAMPLE_LIMIT），重叠率 = |A∩B| / min(|A|,|B|)；任一侧为空也算死边（ratio 0）。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty

RunQuery = Callable[[str], Awaitable[list[dict[str, Any]]]]

# 探针采样上限：distinct 超过此值按截断样本计算（对死边判定足够：
# 死边是零交集， Healthy 边交集通常远大于 0）
PROBE_SAMPLE_LIMIT = 10_000

# FK 语义列判定：显式 is_foreign_key 标记，或 *_CODE 命名约定
_CODE_SUFFIX = "_CODE"


def layerOf(sourceTable: str | None) -> str:
    """数仓分层：source_table 首段前缀（ODS_/DWD_/DIM_/DWS_/ADS_…），未知返回 UNKNOWN。"""
    if not sourceTable or "_" not in sourceTable:
        return "UNKNOWN"
    return sourceTable.split("_", 1)[0].upper()


def isFkLikeColumn(propertyName: str, isForeignKey: bool) -> bool:
    """FK 语义列：显式 FK 标记或 *_CODE 命名（大写比较，兼容小写入库）。"""
    return bool(isForeignKey) or propertyName.upper().endswith(_CODE_SUFFIX)


async def buildJoinHealthReport(session: AsyncSession) -> dict[str, Any]:
    """孤岛巡检：零 JOIN 边类清单 + 全局计数。deadEdges 由 probeDeadEdges 另行填充。"""
    classes = (await session.execute(select(OntologyClass))).scalars().all()
    joins = (await session.execute(select(OntologyJoin))).scalars().all()

    connectedIds: set[int] = set()
    for j in joins:
        connectedIds.add(j.source_class_id)
        connectedIds.add(j.target_class_id)

    propRows = await session.execute(
        select(
            OntologyProperty.class_id,
            OntologyProperty.property_name,
            OntologyProperty.is_foreign_key,
        )
    )
    fkLikeByClass: dict[int, list[str]] = {}
    for classId, propName, isFk in propRows.all():
        if isFkLikeColumn(propName, bool(isFk)):
            fkLikeByClass.setdefault(classId, []).append(propName)

    isolated = [
        {
            "class_id": c.id,
            "class_name": c.class_name,
            "source_table": c.source_table,
            "layer": layerOf(c.source_table),
            "fk_like_columns": sorted(fkLikeByClass.get(c.id, [])),
        }
        for c in classes
        if c.id not in connectedIds
    ]
    isolated.sort(key=lambda item: item["class_name"])

    return {
        "total_classes": len(classes),
        "total_joins": len(joins),
        "isolated": isolated,
        "dead_edges": None,
    }


async def probeDeadEdges(session: AsyncSession, runQuery: RunQuery) -> list[dict[str, Any]]:
    """死边检测：逐条 JOIN 边采样两侧列值域，重叠率 0（含任一侧空列）判死。"""
    joins = (await session.execute(select(OntologyJoin))).scalars().all()
    classes = {
        c.id: c
        for c in (await session.execute(select(OntologyClass))).scalars().all()
    }

    dead: list[dict[str, Any]] = []
    for j in joins:
        srcCls = classes.get(j.source_class_id)
        tgtCls = classes.get(j.target_class_id)
        if srcCls is None or tgtCls is None:
            continue
        for idx, (srcCol, tgtCol) in enumerate(zip(j.source_columns, j.target_columns)):
            overlap, srcN, tgtN = await _columnOverlap(
                runQuery, srcCls.source_table, srcCol, tgtCls.source_table, tgtCol
            )
            if overlap <= 0.0:
                dead.append(
                    {
                        "join_id": j.id,
                        "join_index": idx,
                        "source_class": srcCls.class_name,
                        "source_column": srcCol,
                        "target_class": tgtCls.class_name,
                        "target_column": tgtCol,
                        "overlap_ratio": overlap,
                        "source_distinct": srcN,
                        "target_distinct": tgtN,
                    }
                )
    return dead


async def _columnOverlap(
    runQuery: RunQuery,
    srcTable: str | None,
    srcCol: str,
    tgtTable: str | None,
    tgtCol: str,
) -> tuple[float, int, int]:
    """两列值域重叠率 = |A∩B| / min(|A|,|B|)；空表名/空列按 (0, n, m) 处理。"""
    if not srcTable or not tgtTable:
        return 0.0, 0, 0
    srcRows = await runQuery(
        f"SELECT DISTINCT {srcCol} AS v FROM {srcTable} "
        f"WHERE {srcCol} IS NOT NULL LIMIT {PROBE_SAMPLE_LIMIT}"
    )
    tgtRows = await runQuery(
        f"SELECT DISTINCT {tgtCol} AS v FROM {tgtTable} "
        f"WHERE {tgtCol} IS NOT NULL LIMIT {PROBE_SAMPLE_LIMIT}"
    )
    srcVals = {(str(r.get("v")).strip() if r.get("v") is not None else "") for r in srcRows}
    srcVals.discard("")
    tgtVals = {(str(r.get("v")).strip() if r.get("v") is not None else "") for r in tgtRows}
    tgtVals.discard("")
    if not srcVals or not tgtVals:
        return 0.0, len(srcVals), len(tgtVals)
    overlap = len(srcVals & tgtVals) / min(len(srcVals), len(tgtVals))
    return overlap, len(srcVals), len(tgtVals)


async def _defaultRunQuery(sql: str) -> list[dict[str, Any]]:
    """默认探针执行器：默认活跃数据源（THBI）只读查询。

    独立函数（而非闭包）便于测试 monkeypatch 替换。
    """
    from app.domain.models import DataSource
    from app.infrastructure.business_db_pool import get_adapter
    from app.infrastructure.database import getSessionFactory

    sessionMaker = getSessionFactory()
    async with sessionMaker() as metaSession:
        ds = (
            await metaSession.execute(
                select(DataSource).where(
                    DataSource.is_default == True,  # noqa: E712
                    DataSource.is_active == True,  # noqa: E712
                )
            )
        ).scalars().first()
    if ds is None:
        raise RuntimeError("无默认活跃数据源，值域探针不可用")
    adapter = get_adapter(ds.id, ds)
    return await adapter.execute_read_only(sql)
