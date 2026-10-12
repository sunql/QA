"""研究步结果的**结构化证据摘要**（Task 4）：把一步 SQL 的结果行压成 JSON 安全的统计摘要。

设计口径（controller D9–D15，优先于计划原文）：

**D9 · 列类型从取值推断 —— 本链路没有任何列类型元数据。**
适配器契约是 ``execute_read_only(sql) -> list[dict[str, Any]]``（``business_db_pool``），
``stepResult`` 自己产出的也只是裸列名（``columns = list(rows[0].keys())``）。因此
``columns`` 参数可选：不给就从 ``rows`` 推断 kind；给了也只按**名字**用，不假设有
``type`` 字段。**刻意不做** schema 探测查询 —— 方言相关的元数据抓取远超本任务范围。

**D10 · 无 ``question`` 参数。** 统计摘要是与问题无关的客观摘要，且构造点拿不到用户问题。

**D11 · 唯一构造点是 ``research_agent_ports.stepResult``。** 本模块只产对象 + 一个出图
路径用的包装，接线在 ``chartStep``（``rows`` 在手边）。

**D12 · 加法。** ``stepResult.summary`` 仍是人类可读**字符串**；结构化摘要另起 ``evidence`` key。

**D14 · JSONB 边界。** 摘要落 ``state`` / 检查点 options（JSONB 列），而 ``sampleRows`` 带回
的是**驱动原始值**（``Decimal``/``date``/``datetime``）⇒ 直接落库会
``TypeError: Object of type Decimal is not JSON serializable``。**复用唯一一套映射**
``research_json``（Task 3），不另写第二份类型映射，落库与 SSE 口径因此逐字节一致：
``baseline`` 在两边都是字符串 ``"100"``（读回需要数值时按 ``Decimal(str(x))`` 处理）。

**D15 · 不照抄参考实现的三个崩溃点。** 逐值 ``Decimal(str(v))`` 归一（float 不炸）、
排序键显式处理 ``None`` 且归一类型（date 与 str 不互比）、时间列用**整词 token** 匹配
而非子串（``candidate_name`` 不含 time 词边界，不当时间列）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ConfigDict, Field, field_validator

from app.domain.schemas import CamelModel
from app.services.research_json_encoder import research_json

logger = logging.getLogger(__name__)

# 推断出的列 kind（D9）。``integer``/``numeric`` 同属可聚合的数值列。
KIND_INTEGER = "integer"
KIND_NUMERIC = "numeric"
KIND_TIMESTAMP = "timestamp"
KIND_TEXT = "text"
KIND_BOOLEAN = "boolean"
KIND_UNKNOWN = "unknown"

NUMERIC_KINDS = frozenset({KIND_INTEGER, KIND_NUMERIC})

# 有口径可算、名字里带这些**整词**的列才可能是时间轴（D15 bug 3）。
TIME_NAME_TOKENS = frozenset(
    {"year", "month", "date", "time", "period", "quarter", "week", "day", "timestamp"}
)

DEFAULT_MAX_SAMPLE_ROWS = 5
DEFAULT_TOP_N_LIMIT = 5

# 归一排序键里「缺失」组与「有值」组的判别位（0 有值 < 1 缺失 ⇒ NULL 排到一端）。
_MISSING_RANK = 1
_PRESENT_RANK = 0


class EvidenceSummary(CamelModel):
    """一步结果的统计摘要（frozen DTO；snake 字段 + camel 线上别名，见 ``CamelModel``）。

    数值标量以**字符串**承载（与 ``research_json`` 的 ``Decimal → str`` 口径一致，无损）。
    跨任务契约（Task 5）：``empty`` / ``baseline`` / ``current`` / ``total`` 可**按 Python
    字段名**构造与读取，故保留 ``populate_by_name=True``；改名或删除会静默打断下游。
    """

    model_config = ConfigDict(frozen=True)

    total: str | None = None
    baseline: str | None = None
    current: str | None = None
    delta: str | None = None
    delta_ratio: str | None = None
    top_n: list[dict[str, Any]] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)
    time_range: dict[str, str] | None = None
    empty: bool = False

    @field_validator("total", "baseline", "current", "delta", "delta_ratio", mode="before")
    @classmethod
    def _normalizeNumericScalar(cls, value: Any) -> Any:
        """按名直接构造时容忍 ``Decimal``/``int``/``float``：统一归一为线上字符串。

        ``summarizeEvidence`` 自己喂的是已归一字符串（no-op）；这条只为 Task 5 的
        ``EvidenceSummary(baseline=Decimal("100"), ...)`` 契约不因类型而炸。``Decimal → str``
        无损；``float → str`` 与 ``research_json`` 同口径；``bool`` 交给 pydantic 拒绝
        （数值字段不该收布尔）。
        """
        if value is None or isinstance(value, (str, bool)):
            return value
        if isinstance(value, (int, float, Decimal)):
            return str(value)
        return value


# ---------------------------------------------------------------------------
# 列类型推断（D9）
# ---------------------------------------------------------------------------


def _columnOrder(rows: list[dict[str, Any]], columns: Any) -> list[str]:
    """列顺序：显式传入优先（只认名字），否则取首行键序。"""
    if columns:
        names = [_columnName(column) for column in columns]
        return [name for name in names if name]
    return list(rows[0].keys())


def _columnName(column: Any) -> str | None:
    """`columns` 元素可以是裸列名（str）或含 ``name`` 的 dict；其余忽略。"""
    if isinstance(column, str):
        return column
    if isinstance(column, dict):
        name = column.get("name")
        return str(name) if name else None
    return None


def inferColumnKinds(rows: list[dict[str, Any]]) -> dict[str, str]:
    """逐列从**取值**推断 kind（本链路无列类型元数据，D9）。

    混类型列（如 int + str）保守判 ``unknown``：绝不拿一个可能错的 kind 去做算术。
    全 ``None`` 列 → ``unknown``。``bool`` 先于 ``int`` 判定（它是 ``int`` 的子类）。
    """
    return {
        name: _inferKind([row.get(name) for row in rows]) for name in _columnOrder(rows, None)
    }


def _inferKind(values: list[Any]) -> str:
    seen = {_valueKind(value) for value in values if value is not None}
    if not seen:
        return KIND_UNKNOWN
    if len(seen) == 1:
        return next(iter(seen))
    if seen <= NUMERIC_KINDS:  # int 与 Decimal/float 混排 ⇒ 统一当数值列
        return KIND_NUMERIC
    return KIND_UNKNOWN


def _valueKind(value: Any) -> str:
    if isinstance(value, bool):
        return KIND_BOOLEAN
    if isinstance(value, int):
        return KIND_INTEGER
    if isinstance(value, (Decimal, float)):
        return KIND_NUMERIC
    if isinstance(value, (datetime, date)):
        return KIND_TIMESTAMP
    if isinstance(value, str):
        return KIND_TEXT
    return KIND_UNKNOWN


def _tokenize(name: str) -> list[str]:
    """把列名拆成小写整词 token（``candidate_name`` → [candidate, name]；``createDate`` → [create, date]）。"""
    tokens: list[str] = []
    for chunk in re.split(r"[^0-9A-Za-z]+", name):
        if not chunk:
            continue
        tokens += re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[0-9]+|[A-Z]+", chunk)
    return [token.lower() for token in tokens]


def _hasTimeToken(name: str) -> bool:
    return bool(TIME_NAME_TOKENS & set(_tokenize(name)))


def _timeColumn(order: list[str], kinds: dict[str, str]) -> str | None:
    """时间轴列：**优先真时间戳**（取值即 timestamp），退而用整词 token 匹配名字（D15 bug 3）。

    名字兜底只在非数值列上做 —— 否则 ``period_amount`` 这类数值列会被误当时间轴。
    """
    for name in order:
        if kinds.get(name) == KIND_TIMESTAMP:
            return name
    for name in order:
        if kinds.get(name) in (KIND_INTEGER, KIND_TEXT, KIND_UNKNOWN) and _hasTimeToken(name):
            return name
    return None


def _metricColumn(order: list[str], kinds: dict[str, str], timeColumn: str | None) -> str | None:
    """被聚合的度量列：第一个数值列，**排除时间轴列**（``year`` 是轴不是度量）。"""
    for name in order:
        if name != timeColumn and kinds.get(name) in NUMERIC_KINDS:
            return name
    return None


# ---------------------------------------------------------------------------
# 数值归一与统计（D15 bug 1）
# ---------------------------------------------------------------------------


def _asDecimal(value: Any) -> Decimal | None:
    """把驱动标量归一为**有限** ``Decimal``；不可归一 / 非有限 → ``None``（调用方跳过，不抛）。

    ``float`` 走 ``Decimal(str(v))``（不是 ``Decimal(v)``）：后者会把二进制误差带进来，
    且 ``Decimal(0) + float`` 在参考实现里就是那记 ``TypeError``（D15 bug 1）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, float):
        return Decimal(str(value)) if math.isfinite(value) else None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _sumFinite(values: list[Decimal | None]) -> Decimal | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present, Decimal(0))


def _boundaryValues(
    sortedRows: list[dict[str, Any]], timeColumn: str, metricColumn: str
) -> tuple[Decimal | None, Decimal | None]:
    """基线 / 当前：按时间排序后，**有时间值**的行里第一个 / 最后一个带度量的值。

    时间缺失的行不参与边界（否则「当前」可能是行序最末的无时间行）。
    """
    values = [
        (row.get(timeColumn), _asDecimal(row.get(metricColumn)))
        for row in sortedRows
        if row.get(timeColumn) is not None
    ]
    present = [metric for _, metric in values if metric is not None]
    if not present:
        return None, None
    baseline = next(metric for _, metric in values if metric is not None)
    current = next(metric for _, metric in reversed(values) if metric is not None)
    return baseline, current


def _delta(baseline: Decimal | None, current: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    if baseline is None or current is None:
        return None, None
    difference = current - baseline
    if baseline == 0:
        return difference, None
    try:
        return difference, difference / baseline
    except (InvalidOperation, ZeroDivisionError):
        return difference, None


def _metricValues(rows: list[dict[str, Any]], metricColumn: str | None) -> list[Decimal | None]:
    if metricColumn is None:
        return []
    return [_asDecimal(row.get(metricColumn)) for row in rows]


def _topNRows(rows: list[dict[str, Any]], metricColumn: str | None, limit: int) -> list[dict[str, Any]]:
    """Top-N：按度量值降序（稳定排序 ⇒ 并列保持行序，无 set 迭代不确定性）。"""
    if metricColumn is None:
        return []
    ranked = [(row, _asDecimal(row.get(metricColumn))) for row in rows]
    ranked = [(row, metric) for row, metric in ranked if metric is not None]
    ranked.sort(key=lambda item: item[1], reverse=True)
    return [{"rank": rank, "row": dict(row)} for rank, (row, _) in enumerate(ranked[:limit], 1)]


# ---------------------------------------------------------------------------
# 时间排序 / 时间范围（D15 bug 2）
# ---------------------------------------------------------------------------


def _sortByTime(
    rows: list[dict[str, Any]], timeColumn: str | None, timeKind: str | None
) -> list[dict[str, Any]]:
    """按时间列排序：键按推断 kind 归一（数值列用 Decimal，其余用 ISO/字符串），缺失排一端。"""
    if timeColumn is None:
        return list(rows)
    if timeKind in NUMERIC_KINDS:
        return sorted(rows, key=lambda row: _numericSortKey(row.get(timeColumn)))
    return sorted(rows, key=lambda row: _textSortKey(row.get(timeColumn)))


def _numericSortKey(value: Any) -> tuple[int, Decimal]:
    parsed = _asDecimal(value)
    if parsed is None:
        return (_MISSING_RANK, Decimal(0))
    return (_PRESENT_RANK, parsed)


def _textSortKey(value: Any) -> tuple[int, str]:
    if value is None:
        return (_MISSING_RANK, "")
    return (_PRESENT_RANK, _timeLabel(value))


def _timeLabel(value: Any) -> str:
    """时间轴标签：``date``/``datetime`` → ISO 8601；其余 → ``str``（整数 year → ``"2024"``）。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _timeRange(
    sortedRows: list[dict[str, Any]], timeColumn: str | None
) -> dict[str, str] | None:
    if timeColumn is None:
        return None
    labels = [_timeLabel(row.get(timeColumn)) for row in sortedRows if row.get(timeColumn) is not None]
    if not labels:
        return None
    return {"start": labels[0], "end": labels[-1]}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def summarizeEvidence(
    rows: list[dict[str, Any]],
    columns: Any = None,
    *,
    maxSampleRows: int = DEFAULT_MAX_SAMPLE_ROWS,
    topNLimit: int = DEFAULT_TOP_N_LIMIT,
) -> EvidenceSummary:
    """把一步的结果行压成 JSON 安全的 :class:`EvidenceSummary`。

    ``columns`` 可选（D9）：省略则从 ``rows`` 推断 kind；给出则只按名字用作列顺序。
    返回的模型字段已是 JSON 安全值（``research_json`` 口径），可直接 ``model_dump`` 落库。

    最后一步把整个 payload 过一遍 **``research_json``**（D14）：``sampleRows`` / ``topN``
    里的驱动原始值（``Decimal``/``date``/``datetime``）与数值标量一并归一为 JSON 安全
    标量。**刻意复用唯一一套映射**，不另写第二份类型映射 —— 落库与 SSE 因此逐字节一致。
    """
    if not rows:
        return EvidenceSummary(empty=True)

    order = _columnOrder(rows, columns)
    kinds = {name: _inferKind([row.get(name) for row in rows]) for name in order}
    timeColumn = _timeColumn(order, kinds)
    timeKind = kinds.get(timeColumn) if timeColumn else None
    metricColumn = _metricColumn(order, kinds, timeColumn)

    sortedRows = _sortByTime(rows, timeColumn, timeKind)
    baseline, current = (
        _boundaryValues(sortedRows, timeColumn, metricColumn) if timeColumn and metricColumn else (None, None)
    )
    delta, deltaRatio = _delta(baseline, current)

    payload: dict[str, Any] = {
        "total": _sumFinite(_metricValues(rows, metricColumn)),
        "baseline": baseline,
        "current": current,
        "delta": delta,
        "delta_ratio": deltaRatio,
        "top_n": _topNRows(rows, metricColumn, topNLimit),
        "sample_rows": [dict(row) for row in rows[:maxSampleRows]],
        "time_range": _timeRange(sortedRows, timeColumn),
        "empty": False,
    }
    return EvidenceSummary.model_validate(json.loads(research_json(payload)))


def trySummarizeEvidence(
    rows: list[dict[str, Any]],
    columns: Any = None,
    *,
    maxSampleRows: int = DEFAULT_MAX_SAMPLE_ROWS,
    topNLimit: int = DEFAULT_TOP_N_LIMIT,
) -> EvidenceSummary | None:
    """出图路径专用包装：**任何失败只留痕，不让单步失败**（D12 加法原则）。

    本模块读的是驱动原始行，可能含 ``research_json`` 白名单外的类型（``bytes`` /
    ``memoryview`` / oracledb LOB ...）。同批行在 ``EVENT_STEP_DATA`` 的 emit 处失败会被
    ``emitEvent`` 吞掉（只 warning），故「SSE 能忍」不代表此处能忍；若此处上抛，会经
    ``_guardedRun`` 把**单步**升级成**整 turn 失败**。结构化摘要是加法：算不出来就不给，
    返回 ``None`` 即「本步无结构化证据」。
    """
    try:
        return summarizeEvidence(
            rows, columns, maxSampleRows=maxSampleRows, topNLimit=topNLimit
        )
    except Exception:  # noqa: BLE001 —— 见 docstring：失败不得升级成步失败
        logger.warning("结构化证据摘要生成失败，本步不带 evidence", exc_info=True)
        return None
