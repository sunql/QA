"""KPI 治理 token → 物理列别名解析层（P3 D2）。

设计 §7：治理侧结构化 token（``numerator_columns`` / ``denominator_columns`` /
``grain_keys``）先经本层解析成物理列，层 2 对账再比较列集合；纯时间词（月/季度/年/
周/日/天）不进列比较，映射为 :class:`~app.domain.enums.TimeGrain`。

纯函数部分零 IO（不碰库）：同输入同输出，便于单测与层 2 复用。
唯一 IO 是底部薄 loader :func:`sourcePropertiesForMetric`（只读 SELECT）。

解析顺序（每 token，首个命中的阶段决定结果）：

1. 归一化（strip/lower）后等于某 ``source_column`` ⇒ 该物理列（原样存储值）；
2. 等于某 ``property_name`` ⇒ 该行的 ``source_column``；无 ``source_column`` ⇒ unknown；
3. 等于某 ``property_alias`` ⇒ 同上；
4. 等于某 ``business_aliases`` 元素 ⇒ 同上；
5. 命中多个**不同**物理列 ⇒ unknown（不猜）；
6. 无任何命中 ⇒ missing（层 2 据此判 mismatch）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import TimeGrain
from app.domain.models import OntologyMetricSource, OntologyProperty

_DATE_DATA_TYPES: frozenset[str] = frozenset({"DATE", "DATETIME", "TIMESTAMP"})
_DATE_COLUMN_MARKER = "DATE"
_TS_COLUMN_SUFFIX = "_TS"

# 纯时间词（R8）→ TimeGrain。`月份` 与 `月` 同义；`日`/`天` 同义。
_TIME_TOKEN_TO_GRAIN: dict[str, TimeGrain] = {
    "月": TimeGrain.MONTH,
    "月份": TimeGrain.MONTH,
    "季度": TimeGrain.QUARTER,
    "年": TimeGrain.YEAR,
    "周": TimeGrain.WEEK,
    "日": TimeGrain.DAY,
    "天": TimeGrain.DAY,
}

# `按X` 前缀形态认的全部裸时间词，必须与 `_TIME_TOKEN_TO_GRAIN` 的键对齐
# （R8：月/月份/季度/年/周/日/天）。直接从映射派生，避免两处清单再次漂移
# （此前漏 `月份` ⇒ `按月份` 被当列 token ⇒ missing ⇒ 误判 mismatch）。
_BY_PREFIX = "按"
_BARE_TIME_WORDS: frozenset[str] = frozenset(_TIME_TOKEN_TO_GRAIN)

# 自由文本 grain 推导：最长词优先（「季度」先于「月」；两者无重叠但保持显式）。
_GRAIN_TEXT_MARKERS: tuple[tuple[str, TimeGrain], ...] = (
    ("季度", TimeGrain.QUARTER),
    ("月", TimeGrain.MONTH),
    ("周", TimeGrain.WEEK),
    ("年", TimeGrain.YEAR),
    ("日", TimeGrain.DAY),
    ("天", TimeGrain.DAY),
)


@dataclass(frozen=True)
class SourceProperty:
    """源类一条 ``ontology_property`` 行的解析视图。"""

    property_name: str
    source_column: str | None
    property_alias: str | None
    business_aliases: tuple[str, ...] = ()
    data_type: str | None = None


@dataclass(frozen=True)
class ResolutionOutcome:
    """解析结果（frozen：调用方不得原地修改）。

    ``resolved`` 保存 ``(原始 token, 物理列)``；``unknownTokens`` 为多候选（不猜），
    ``missingTokens`` 为无候选（层 2 判 mismatch）。纯时间词不进入这两组 token，
    只产出 ``timeGrain``；``timeCandidates`` 是源类并集中的日期列（R3 报告用）。
    """

    resolved: tuple[tuple[str, str], ...] = ()
    unknownTokens: tuple[str, ...] = ()
    missingTokens: tuple[str, ...] = ()
    timeGrain: TimeGrain | None = None
    timeCandidates: tuple[str, ...] = ()


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def _timeGrainForToken(normalized: str) -> TimeGrain | None:
    """纯时间词映射；非时间词 ⇒ None。"""
    grain = _TIME_TOKEN_TO_GRAIN.get(normalized)
    if grain is not None:
        return grain
    if normalized.startswith(_BY_PREFIX):
        bare = normalized[len(_BY_PREFIX) :]
        if bare in _BARE_TIME_WORDS:
            return _TIME_TOKEN_TO_GRAIN[bare]
    return None


def _decide(columns: tuple[str | None, ...]) -> tuple[str, str | None]:
    """单一物理列 ⇒ resolved；含空列或多列 ⇒ unknown（不猜）。"""
    distinct = set(columns)
    if len(distinct) == 1:
        column = next(iter(distinct))
        if column is not None:
            return ("resolved", column)
    return ("unknown", None)


def _resolveOneToken(normalized: str, sourceProperties: tuple[SourceProperty, ...]) -> tuple[str, str | None]:
    """按阶段顺序解析单个 token，返回 ``(status, column)``。"""
    identityHits = tuple(
        prop.source_column for prop in sourceProperties if _normalize(prop.source_column) == normalized
    )
    if identityHits:
        return _decide(identityHits)

    nameHits = tuple(
        prop.source_column for prop in sourceProperties if _normalize(prop.property_name) == normalized
    )
    if nameHits:
        return _decide(nameHits)

    aliasHits = tuple(
        prop.source_column for prop in sourceProperties if _normalize(prop.property_alias) == normalized
    )
    if aliasHits:
        return _decide(aliasHits)

    businessHits = tuple(
        prop.source_column
        for prop in sourceProperties
        if any(_normalize(alias) == normalized for alias in prop.business_aliases)
    )
    if businessHits:
        return _decide(businessHits)

    return ("missing", None)


def _isDateColumn(prop: SourceProperty) -> bool:
    if not prop.source_column:
        return False
    dataType = (prop.data_type or "").strip().upper()
    column = prop.source_column.upper()
    return dataType in _DATE_DATA_TYPES or _DATE_COLUMN_MARKER in column or column.endswith(_TS_COLUMN_SUFFIX)


def _collectTimeCandidates(
    sourceProperties: tuple[SourceProperty, ...],
) -> tuple[str, ...]:
    columns = {prop.source_column for prop in sourceProperties if _isDateColumn(prop)}
    return tuple(sorted(column for column in columns if column is not None))


def resolveGovernanceTokens(
    tokens: tuple[str, ...] | list[str] | None,
    sourceProperties: tuple[SourceProperty, ...],
) -> ResolutionOutcome:
    """治理 token → 物理列 + 纯时间词 → TimeGrain（纯函数，零 IO）。"""
    timeCandidates = _collectTimeCandidates(sourceProperties)
    if not tokens:
        return ResolutionOutcome(timeCandidates=timeCandidates)

    timeTokens: list[tuple[str, TimeGrain]] = []
    columnTokens: list[str] = []
    for token in tokens:
        normalized = _normalize(token)
        grain = _timeGrainForToken(normalized)
        if grain is not None:
            timeTokens.append((token, grain))
        else:
            columnTokens.append(token)

    resolved: list[tuple[str, str]] = []
    unknownTokens: list[str] = []
    missingTokens: list[str] = []

    if len({grain for _, grain in timeTokens}) > 1:
        unknownTokens.extend(token for token, _ in timeTokens)
        timeGrain: TimeGrain | None = None
    else:
        timeGrain = timeTokens[0][1] if timeTokens else None

    for token in columnTokens:
        normalized = _normalize(token)
        if not normalized:
            missingTokens.append(token)
            continue
        status, column = _resolveOneToken(normalized, sourceProperties)
        if status == "resolved" and column is not None:
            resolved.append((token, column))
        elif status == "unknown":
            unknownTokens.append(token)
        else:
            missingTokens.append(token)

    return ResolutionOutcome(
        resolved=tuple(resolved),
        unknownTokens=tuple(unknownTokens),
        missingTokens=tuple(missingTokens),
        timeGrain=timeGrain,
        timeCandidates=timeCandidates,
    )


def deriveTimeGrain(grainText: str | None) -> TimeGrain | None:
    """自由文本 grain → TimeGrain（最长词优先）；无时间词 ⇒ None。

    回填（R6）与层 2 共用：``kpi_catalog.grain`` 如 ``供应商+工厂+月`` ⇒ MONTH。
    """
    if not grainText:
        return None
    for marker, grain in _GRAIN_TEXT_MARKERS:
        if marker in grainText:
            return grain
    return None


async def sourcePropertiesForMetric(session: AsyncSession, metricId: int) -> tuple[SourceProperty, ...]:
    """``ontology_metric_source`` 源类并集 → ``ontology_property`` 行；无行 ⇒ ()。

    薄 IO，只读 SELECT。``target_class_id`` 12/12 为 NULL（recon §3），
    源类以 ``ontology_metric_source`` 为权威。
    """
    classRows = await session.execute(
        select(OntologyMetricSource.class_id).where(OntologyMetricSource.metric_id == metricId).distinct()
    )
    classIds = list(classRows.scalars().all())
    if not classIds:
        return ()

    # 只选需要的列（列表达式 select），不加载 ORM 实体：实体 select 会触发
    # ``OntologyProperty.ontology_class → OntologyClass.metrics`` 的 selectin 关系
    # 加载，进而 SELECT 整行 ontology_metric —— 0143 未迁移时该表缺 time_grain /
    # time_column ⇒ 回填脚本的 dry-run 在 dev 库上直接 UndefinedColumn 崩溃。
    propertyRows = await session.execute(
        select(
            OntologyProperty.property_name,
            OntologyProperty.source_column,
            OntologyProperty.property_alias,
            OntologyProperty.business_aliases,
            OntologyProperty.data_type,
        ).where(OntologyProperty.class_id.in_(classIds))
    )
    properties = propertyRows.all()
    return tuple(
        SourceProperty(
            property_name=row.property_name,
            source_column=row.source_column,
            property_alias=row.property_alias,
            business_aliases=tuple(row.business_aliases or ()),
            data_type=row.data_type,
        )
        for row in properties
    )


__all__ = [
    "ResolutionOutcome",
    "SourceProperty",
    "deriveTimeGrain",
    "resolveGovernanceTokens",
    "sourcePropertiesForMetric",
]
