"""P1a：KPI → 数据源绑定建议（只读；永不自动写入）。

信号与分值（按分值降序，同分按名称升序）：

1. ``legacy_name_exact``  0.90 —— legacy ``kpi_catalog.data_source`` 与数据源名
   大小写不敏感完全相等（历史数据里常直接写库名/数据源名）。
2. ``ontology_table``     0.80 —— ``kpi_catalog.metric_id`` → ``ontology_metric``
   → ``ontology_class.source_table``，再由 ``schema_cache`` 反查持有该表的
   ``datasource_id``。
3. ``legacy_name_fuzzy``  0.55 —— legacy 文本是数据源名的子串（或反之）。

只考虑 ``data_source.is_active=True`` 的源；同一数据源多个信号命中时合并
reasons 并取最高分；最多返回 5 条。

数据源数量很小，全部载入后在内存里匹配，避免 ILIKE 通配符转义与多次往返。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, OntologyClass, OntologyMetric, SchemaCache
from app.services.kpi_catalog_service import KpiCatalogService

_SCORE_EXACT = 0.90
_SCORE_ONTOLOGY = 0.80
_SCORE_FUZZY = 0.55
_MAX_SUGGESTIONS = 5


@dataclass
class _Hit:
    """累加器：同一数据源的多条信号合并（局部可变，出口转 frozen）。"""

    datasourceId: int
    name: str
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DatasourceSuggestion:
    datasourceId: int
    name: str
    score: float
    reasons: tuple[str, ...]


def _addHit(
    hits: dict[int, _Hit], ds: DataSource, score: float, reason: str
) -> None:
    hit = hits.get(ds.id)
    if hit is None:
        hits[ds.id] = _Hit(ds.id, ds.name, score, [reason])
        return
    hit.score = max(hit.score, score)
    if reason not in hit.reasons:
        hit.reasons.append(reason)


class KpiDatasourceSuggestionService:
    async def suggest(
        self, session: AsyncSession, kpiId: int
    ) -> list[DatasourceSuggestion]:
        """返回该 KPI 的数据源绑定建议；未知 KPI 抛 NotFoundError。"""
        kpi = await KpiCatalogService().getKpi(session, kpiId)
        sources = await self._activeSources(session)
        if not sources:
            return []

        hits: dict[int, _Hit] = {}
        legacy = (kpi.data_source or "").strip().lower()
        if legacy:
            for ds in sources:
                name = ds.name.strip().lower()
                if name == legacy:
                    _addHit(hits, ds, _SCORE_EXACT, "legacy_name_exact")
                elif legacy in name or name in legacy:
                    _addHit(hits, ds, _SCORE_FUZZY, "legacy_name_fuzzy")

        ontologyIds = await self._ontologyDatasourceIds(session, kpi.metric_id)
        for ds in sources:
            if ds.id in ontologyIds:
                _addHit(hits, ds, _SCORE_ONTOLOGY, "ontology_table")

        ordered = sorted(hits.values(), key=lambda h: (-h.score, h.name))
        return [
            DatasourceSuggestion(h.datasourceId, h.name, h.score, tuple(h.reasons))
            for h in ordered[:_MAX_SUGGESTIONS]
        ]

    @staticmethod
    async def _activeSources(session: AsyncSession) -> list[DataSource]:
        result = await session.execute(
            select(DataSource).where(DataSource.is_active.is_(True))
        )
        return list(result.scalars().all())

    @staticmethod
    async def _ontologyDatasourceIds(
        session: AsyncSession, metricId: int | None
    ) -> set[int]:
        """metric → class.source_table → schema_cache 中持有该表的数据源。"""
        if metricId is None:
            return set()
        metric = await session.get(OntologyMetric, metricId)
        if metric is None or metric.target_class_id is None:
            return set()
        ontologyClass = await session.get(OntologyClass, metric.target_class_id)
        if ontologyClass is None or not (ontologyClass.source_table or "").strip():
            return set()
        target = ontologyClass.source_table.strip().upper()

        rows = (await session.execute(select(SchemaCache))).scalars().all()
        matched: set[int] = set()
        for row in rows:
            for table in row.schema_data or []:
                name = str(table.get("table_name", "")).strip().upper()
                if name == target:
                    matched.add(row.datasource_id)
                    break
        return matched
