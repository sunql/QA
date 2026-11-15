"""manifest 名字 → 图谱 `unified_id` 的解析（唯一碰 PG 的入口）。

**为什么单独一个模块**：`semantic_graph_mirror.py` 是纯逻辑（可单测、不连库），
解析要连 PG ⇒ 拆开，纯逻辑与 IO 各归各（也顺手把文件压回 400 行以内）。

**口径必须与编译器一致**（否则「发布时的快照」与「物化时的活数据」会分叉）：

- 类：只认**活跃**类（`valid_to IS NULL`）⇒ 墓碑类不可解析；
- 属性：只认活跃类名下的属性；
- 指标 / KPI：按名字取，行不存在 ⇒ 解析不到。

解析不到有三种成因，报告里分开说：**本体里没有**、**同名多行**（歧义）、
**实体存在但没登记统一 ID**（`id_mapping` 缺行 ⇒ 用 LEFT JOIN 才能看见）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import String, and_, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.id_mapping import (
    BUSINESS_OBJECT_CLASS,
    BUSINESS_OBJECT_KPI,
    BUSINESS_OBJECT_METRIC,
    BUSINESS_OBJECT_PROPERTY,
)
from app.domain.models import (
    IdMapping,
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyProperty,
)
from app.services.semantic_graph_mirror import EntityResolution, buildEntityResolution

# PG 单条语句的参数上限 65535；名字列表要 IN (...) ⇒ 必须分片。
# 取 2000 留足余量（一次查询只绑定一次名字列表）。
_QUERY_CHUNK_SIZE = 2_000


def _mappingJoin(mapping: IdMapping, businessObject: str, pgIdColumn: Any) -> Any:
    """把 id_mapping 关联到本体表主键（external_id 存的是主键的字符串形式）。"""
    return and_(
        mapping.business_object == businessObject,
        mapping.external_id == cast(pgIdColumn, String),
    )


def _chunked(values: Sequence[Any], size: int = _QUERY_CHUNK_SIZE) -> list[list[Any]]:
    """把长列表切片（防参数数量超限）。"""
    return [list(values[start : start + size]) for start in range(0, len(values), size)]


async def resolveManifestEntities(
    session: AsyncSession,
    manifest: dict[str, Any],
) -> EntityResolution:
    """把 manifest 里的**名字**解析成图谱用的 `unified_id`。"""
    classNames = [cls["className"] for cls in manifest.get("classes") or []]
    metricNames = [metric["metricName"] for metric in manifest.get("metrics") or []]
    kpiCodes = [kpi["kpiCode"] for kpi in manifest.get("kpis") or []]

    classRows = await _resolveClasses(session, classNames)
    propertyRows = await _resolveProperties(session, classNames)
    metricRows = await _resolveMetrics(session, metricNames)
    kpiRows = await _resolveKpis(session, kpiCodes)

    return buildEntityResolution(
        classRows=classRows,
        propertyRows=propertyRows,
        metricRows=metricRows,
        kpiRows=kpiRows,
    )


async def _resolveClasses(
    session: AsyncSession, classNames: Sequence[str]
) -> list[tuple[str, str | None]]:
    """类名 → unified_id（LEFT JOIN id_mapping ⇒ 未登记的实体也看得见）。"""
    rows: list[tuple[str, str | None]] = []
    for chunk in _chunked(list(dict.fromkeys(classNames))):
        stmt = (
            select(OntologyClass.class_name, IdMapping.unified_id)
            .outerjoin(
                IdMapping,
                _mappingJoin(IdMapping, BUSINESS_OBJECT_CLASS, OntologyClass.id),
            )
            .where(
                OntologyClass.class_name.in_(chunk),
                OntologyClass.valid_to.is_(None),
            )
        )
        rows.extend((row[0], row[1]) for row in (await session.execute(stmt)).all())
    return rows


async def _resolveProperties(
    session: AsyncSession, classNames: Sequence[str]
) -> list[tuple[str, str, str | None]]:
    """(类名, 属性名) → unified_id。"""
    rows: list[tuple[str, str, str | None]] = []
    for chunk in _chunked(list(dict.fromkeys(classNames))):
        stmt = (
            select(
                OntologyClass.class_name,
                OntologyProperty.property_name,
                IdMapping.unified_id,
            )
            .join(OntologyProperty, OntologyProperty.class_id == OntologyClass.id)
            .outerjoin(
                IdMapping,
                _mappingJoin(IdMapping, BUSINESS_OBJECT_PROPERTY, OntologyProperty.id),
            )
            .where(
                OntologyClass.class_name.in_(chunk),
                OntologyClass.valid_to.is_(None),
            )
        )
        rows.extend(
            (row[0], row[1], row[2]) for row in (await session.execute(stmt)).all()
        )
    return rows


async def _resolveMetrics(
    session: AsyncSession, metricNames: Sequence[str]
) -> list[tuple[str, str | None]]:
    """指标名 → unified_id。"""
    rows: list[tuple[str, str | None]] = []
    for chunk in _chunked(list(dict.fromkeys(metricNames))):
        stmt = (
            select(OntologyMetric.metric_name, IdMapping.unified_id)
            .outerjoin(
                IdMapping,
                _mappingJoin(IdMapping, BUSINESS_OBJECT_METRIC, OntologyMetric.id),
            )
            .where(OntologyMetric.metric_name.in_(chunk))
        )
        rows.extend((row[0], row[1]) for row in (await session.execute(stmt)).all())
    return rows


async def _resolveKpis(
    session: AsyncSession, kpiCodes: Sequence[str]
) -> list[tuple[str, str | None]]:
    """KPI 编码 → unified_id。"""
    rows: list[tuple[str, str | None]] = []
    for chunk in _chunked(list(dict.fromkeys(kpiCodes))):
        stmt = (
            select(KpiCatalog.kpi_code, IdMapping.unified_id)
            .outerjoin(
                IdMapping,
                _mappingJoin(IdMapping, BUSINESS_OBJECT_KPI, KpiCatalog.id),
            )
            .where(KpiCatalog.kpi_code.in_(chunk))
        )
        rows.extend((row[0], row[1]) for row in (await session.execute(stmt)).all())
    return rows


__all__ = ["resolveManifestEntities"]
