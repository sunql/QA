"""P1a：KPI → 数据源绑定建议（只读；永不自动写入）。

信号与分值（按分值降序，同分按名称升序）：

1. ``legacy_name_exact``  0.90 —— legacy ``kpi_catalog.data_source`` 与数据源名
   大小写不敏感完全相等（历史数据里常直接写库名/数据源名）。
2. ``legacy_table_exact`` 0.85 —— legacy ``data_source`` 里是**裸表名**（多个表用
   空格 / ``+`` / ``,`` / ``、`` 分隔），与 ``schema_cache`` 的「表名 → 数据源」
   索引精确 join（大小写不敏感）。
3. ``ontology_table``     0.80 —— ``kpi_catalog.metric_id`` → ``ontology_metric``
   → ``ontology_class.source_table``，再由 ``schema_cache`` 反查持有该表的
   ``datasource_id``。
4. ``legacy_name_fuzzy``  0.55 —— legacy 文本是数据源名的子串（或反之）。

只考虑 ``data_source.is_active=True`` 的源；同一数据源多个信号命中时合并
reasons 并取最高分；最多返回 5 条。

注意：``score`` 取**最高**信号分值；``reasons`` 在**出口按分值降序**排列
（同分保持命中顺序），前端只展示 ``reasons[0]`` ⇒ 即分值最高那条信号。

数据源数量很小，全部载入后在内存里匹配，避免 ILIKE 通配符转义与多次往返。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, OntologyClass, OntologyMetric, SchemaCache
from app.services.kpi_catalog_service import KpiCatalogService

_SCORE_EXACT = 0.90
# 裸表名 → 数据源的精确 join（分值低于 legacy 名称精确，高于本体表反查）
_SCORE_TABLE_EXACT = 0.85
_SCORE_ONTOLOGY = 0.80
_SCORE_FUZZY = 0.55
_MAX_SUGGESTIONS = 5

# reason → 分值（出口排序用）。未知 reason 兜底 0.0，不让它把排序打乱。
_REASON_SCORES: Mapping[str, float] = {
    "legacy_name_exact": _SCORE_EXACT,
    "legacy_table_exact": _SCORE_TABLE_EXACT,
    "ontology_table": _SCORE_ONTOLOGY,
    "legacy_name_fuzzy": _SCORE_FUZZY,
}

# `data_source` 历史值是裸表名，多个表用空格 / '+' / ',' / '、' 分隔
_TABLE_SEPARATORS = re.compile(r"[\s+,、]+")

# 数据源名里可能含空白（dev 上就有 "THBI Oracle"）。legacy data_source 若照抄
# 数据源名，切分会把它拆成碎片，碎片再冒充别的数据源的表名 ⇒ 0.85 的
# legacy_table_exact 会压过 0.80 的 ontology_table，把错的数据源顶到第一名。
# 修法：拆表名**之前**先把「含空白的启用数据源名」跨度从文本里摘掉。
#
# 只对**含空白**的名字建模式：单字数据源名（"DWD_PAYMENT"）在存量语义里
# 同时是合法裸表名，摘掉会丢掉 legacy_table_exact（既有集成测试依赖）。
_DS_NAME_BOUNDARY_LEFT = r"(?<![A-Z0-9_])"
_DS_NAME_BOUNDARY_RIGHT = r"(?![A-Z0-9_])"


def buildDatasourceNamePattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """把**含空白**的数据源名编译成边界安全的联合正则。

    名字内部的空白用 ``\\s+`` 容忍多空格（"THBI  Oracle" 也算命中）；
    前后加 ``(?<![A-Z0-9_])`` / ``(?![A-Z0-9_])`` 边界，
    避免 "THBI_ORDERS" 之类含下划线的标识被误当成独立词命中。

    单字名字一律不建模式（见模块注释与既有测试约束）。

    名字先转大写，匹配时用 ``re.IGNORECASE``：legacy 文本大小写混用
    （``"THBI Oracle ORDERS"``）也必须能命中；边界断言在 IGNORECASE 下
    同样拒绝小写字母（``[A-Z]`` 会匹配 ``[a-z]``）。
    """
    alternatives: list[str] = []
    for name in names:
        cleaned = (name or "").strip().upper()
        if not cleaned or not any(ch.isspace() for ch in cleaned):
            continue
        alternatives.append(r"\s+".join(re.escape(w) for w in cleaned.split()))
    if not alternatives:
        return None
    # 长名优先，避免短名先吃掉长名的前缀
    alternatives.sort(key=len, reverse=True)
    body = "|".join(alternatives)
    return re.compile(
        rf"{_DS_NAME_BOUNDARY_LEFT}(?:{body}){_DS_NAME_BOUNDARY_RIGHT}",
        re.IGNORECASE,
    )


def stripDatasourceNames(text: str | None, pattern: re.Pattern[str] | None) -> str:
    """把文本里「含空白的启用数据源名」跨度替换成空格（其余原样保留）。

    替换成空格而不是空串 ⇒ 保留分隔语义，后续切分不会把两侧粘成一个 token。
    """
    if not text or pattern is None:
        return text or ""
    return pattern.sub(" ", text)


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


def splitLegacyTableNames(
    text: str | None,
    datasourceNamePattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    """把 ``kpi_catalog.data_source`` 拆成候选表名（大写、去空项、去重、保序）。

    历史数据里 ``data_source`` 存的是**裸表名**而不是数据源名，多个表用
    空格或 ``+`` 分隔，例如 ``"DWD_PURCHASE_ORDER + DWD_GOODS_RECEIPT"``。

    ``datasourceNamePattern`` 非空时，先把「含空白的启用数据源名」跨度摘掉，
    避免数据源名被切成碎片后冒充别的数据源的表名（B-3）。
    """
    if not text:
        return ()
    prepared = stripDatasourceNames(text, datasourceNamePattern)
    ordered: dict[str, None] = {}
    for part in _TABLE_SEPARATORS.split(prepared):
        name = part.strip().upper()
        if name:
            ordered.setdefault(name, None)
    return tuple(ordered)


def buildTableIndex(schemaRows: tuple[SchemaCache, ...]) -> dict[str, tuple[int, ...]]:
    """表名（大写）→ 持有该表的数据源 id 列表。

    ``schema_data`` 是无类型 JSONB，历史行里可能有非 dict 条目 —— 跳过。

    表名同时兼容 ``table_name`` 与 ``tableName`` 两种键（与
    ``api/v1/datasource.py`` 的分层统计口径保持一致，否则同一份 schema
    在一处数得到、在这里却看不见）。

    ⚠️ 与 ``datasource.py`` 有一处**刻意差异**：那里用 ``str(name).upper()``
    把数字/布尔表名转成字符串，这里直接**跳过**脏值 —— ``None`` 若被
    ``str()`` 转成 ``"NONE"``，就会凭空多出一个可被冒名命中的表名。
    """
    index: dict[str, set[int]] = {}
    for row in schemaRows:
        if row.datasource_id is None:
            continue
        for table in row.schema_data or []:
            if not isinstance(table, dict):
                continue
            raw = table.get("table_name") or table.get("tableName")
            if not isinstance(raw, str):
                continue
            name = raw.strip().upper()
            if not name:
                continue
            index.setdefault(name, set()).add(row.datasource_id)
    return {name: tuple(sorted(ids)) for name, ids in index.items()}


@dataclass(frozen=True)
class SuggestionContext:
    """一次批量建议共享的只读快照（数据源 + schema_cache + 表名索引）。

    ``schema_data`` 是完整 schema JSON（可能数 MB），逐 KPI 重新加载会放大
    成 N 倍流量；预检脚本一次预览 N 个 KPI 时用同一份快照。

    ``tableDatasourceIds`` 是「表名 → 数据源 id」索引，由 ``schemaRows``
    一次构建；两条表级信号（legacy 裸表名 / ontology class 主表）共用它。

    ``datasourceNamePattern`` 是含空白启用数据源名的边界安全模式，拆 legacy
    裸表名前先摘掉这些跨度（见 ``buildDatasourceNamePattern``）。
    """

    sources: tuple[DataSource, ...]
    schemaRows: tuple[SchemaCache, ...]
    tableDatasourceIds: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    datasourceNamePattern: re.Pattern[str] | None = None


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
    async def buildContext(self, session: AsyncSession) -> SuggestionContext:
        """加载一次批量建议可复用的只读快照。"""
        sources = await self._activeSources(session)
        schemaRows = tuple((await session.execute(select(SchemaCache))).scalars().all())
        return SuggestionContext(
            sources=tuple(sources),
            schemaRows=schemaRows,
            tableDatasourceIds=buildTableIndex(schemaRows),
            datasourceNamePattern=buildDatasourceNamePattern(ds.name for ds in sources),
        )

    async def suggest(
        self,
        session: AsyncSession,
        kpiId: int,
        *,
        context: SuggestionContext | None = None,
    ) -> list[DatasourceSuggestion]:
        """返回该 KPI 的数据源绑定建议；未知 KPI 抛 NotFoundError。"""
        kpi = await KpiCatalogService().getKpi(session, kpiId)
        ctx = context or await self.buildContext(session)
        sources = ctx.sources
        if not sources:
            return []

        hits: dict[int, _Hit] = {}
        sourcesById = {ds.id: ds for ds in sources}
        legacy = (kpi.data_source or "").strip().lower()
        if legacy:
            for ds in sources:
                name = ds.name.strip().lower()
                if name == legacy:
                    _addHit(hits, ds, _SCORE_EXACT, "legacy_name_exact")
                elif legacy in name or name in legacy:
                    _addHit(hits, ds, _SCORE_FUZZY, "legacy_name_fuzzy")

        # 裸表名 → 数据源（存量 data_source 就是裸表名，这是存量数据唯一可用的信号）
        # 先摘掉「含空白的启用数据源名」跨度（B-3）：否则 "THBI Oracle ORDERS"
        # 会被切成 THBI/ORACLE/ORDERS，碎片 THBI/ORACLE 可能撞上别的数据源的表名，
        # 产出 0.85 的 legacy_table_exact 压过 0.80 的 ontology_table。
        # 注意：名称类信号（legacy_name_exact / legacy_name_fuzzy）仍用原始文本。
        for table in splitLegacyTableNames(kpi.data_source, ctx.datasourceNamePattern):
            for dsId in ctx.tableDatasourceIds.get(table, ()):
                ds = sourcesById.get(dsId)
                if ds is not None:
                    _addHit(hits, ds, _SCORE_TABLE_EXACT, "legacy_table_exact")

        ontologyIds = await self._ontologyDatasourceIds(session, kpi.metric_id, ctx)
        for ds in sources:
            if ds.id in ontologyIds:
                _addHit(hits, ds, _SCORE_ONTOLOGY, "ontology_table")

        ordered = sorted(hits.values(), key=lambda h: (-h.score, h.name))
        return [
            DatasourceSuggestion(
                h.datasourceId,
                h.name,
                h.score,
                tuple(sorted(h.reasons, key=lambda r: -_REASON_SCORES.get(r, 0.0))),
            )
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
        session: AsyncSession,
        metricId: int | None,
        context: SuggestionContext,
    ) -> set[int]:
        """metric → class.source_table → schema_cache 中持有该表的数据源。"""
        if metricId is None:
            return set()
        metric = await session.get(OntologyMetric, metricId)
        if metric is None or metric.target_class_id is None:
            return set()
        ontologyClass = await session.get(OntologyClass, metric.target_class_id)
        if (
            ontologyClass is None
            or ontologyClass.valid_to is not None  # 软删除（墓碑）不再参与建议
            or not (ontologyClass.source_table or "").strip()
        ):
            return set()
        target = ontologyClass.source_table.strip().upper()
        return set(context.tableDatasourceIds.get(target, ()))
