"""research_finding_aggregator：finding 行数据确定性聚合（R2 plan Task 1）。

输入是 `verify` 阶段落库的 `supporting_data["rows"]: list[dict]`，输出是
`FindingStats`（topContributors / distribution / mom / anomalies / 列名）。

**约束**：零 LLM、零 DB、零 IO；`rows=[]` / 全空 / 全 None → 不抛，返回空 stats，
让 verify 阶段不会因聚合挂掉。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = ["FindingStats", "aggregateRows", "buildEnhancedClaim"]

MAX_TOP_N_DIM_UNIQUE = 20
ANOMALY_Z_THRESHOLD = 2.0
SHARE_PCT_DECIMALS = 1
DISTRIBUTION_DECIMALS = 2
ZSCORE_DECIMALS = 2
DELTA_PCT_DECIMALS = 1
_PERIOD_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


@dataclass(frozen=True)
class FindingStats:
    """finding 行数据的确定性聚合结果（报告 LLM 的「真」数据来源）。"""

    topContributors: tuple[dict[str, Any], ...]
    distribution: dict[str, dict[str, float]]
    mom: tuple[dict[str, Any], ...]
    anomalies: tuple[dict[str, Any], ...]
    numericColumns: tuple[str, ...]
    dimensionColumns: tuple[str, ...]
    rowCount: int

    def asdict(self) -> dict[str, Any]:
        return {
            "topContributors": list(self.topContributors),
            "distribution": self.distribution,
            "mom": list(self.mom),
            "anomalies": list(self.anomalies),
            "numericColumns": list(self.numericColumns),
            "dimensionColumns": list(self.dimensionColumns),
            "rowCount": self.rowCount,
        }


def aggregateRows(
    rows: list[dict[str, Any]],
    *,
    dimensionHints: list[str] | None = None,
    maxTopN: int = 5,
) -> FindingStats:
    """对 finding 行数据做确定性聚合；rows=[] 返空 stats（不会抛）。

    `dimensionHints` 强制把指定列当维度（覆盖数值启发式）。
    """
    if not rows:
        return FindingStats((), {}, (), (), (), (), 0)

    columns = _columns(rows)
    if not columns:
        return FindingStats((), {}, (), (), (), (), len(rows))

    hints = set(dimensionHints or [])
    numericCols = _numericColumns(rows, columns, exclude=hints)
    dimCols = _dimensionColumns(rows, columns, hints=hints)
    periodCol = _findPeriodColumn(rows, columns)

    return FindingStats(
        topContributors=tuple(_topContributors(rows, dimCols, maxTopN=maxTopN)),
        distribution=_distribution(rows, numericCols),
        mom=tuple(_mom(rows, periodCol, numericCols)),
        anomalies=tuple(_anomalies(rows, numericCols)),
        numericColumns=tuple(numericCols),
        dimensionColumns=tuple(dimCols),
        rowCount=len(rows),
    )


def buildEnhancedClaim(
    stats: FindingStats,
    *,
    originalClaim: str,
    maxChars: int = 200,
) -> str:
    """把 stats 拼成「数据视角的结论」；空 stats 回落 originalClaim。"""
    if stats.rowCount == 0 or not stats.topContributors:
        return originalClaim

    parts: list[str] = []
    if stats.dimensionColumns:
        dim = stats.dimensionColumns[0]
        chunks = [
            f"{it['value']} 占 {it['sharePct']}%"
            for it in sorted(stats.topContributors, key=lambda r: (-r["count"], r["value"]))
        ]
        parts.append(f"按 {dim} 分，{ '，'.join(chunks)}")

    firstNum = next(iter(stats.distribution), None)
    if firstNum is not None:
        d = stats.distribution[firstNum]
        parts.append(f"{firstNum} 范围 {d['min']:.2f}–{d['max']:.2f}")

    text = "；".join(parts) if parts else originalClaim
    return text[:maxChars] if len(text) > maxChars else text


# --- helpers (pure) ---------------------------------------------------------


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    seen, cols = set(), []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for k in row:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols


def _asNumber(v: Any) -> float | None:
    """bool / NaN 排除；int / float / Decimal 转 float。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    if isinstance(v, Decimal):
        return float(v)
    return None


def _isNumberValue(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float, Decimal)):
        return not (isinstance(v, float) and math.isnan(v))
    return False


def _numericColumns(
    rows: list[dict[str, Any]],
    columns: list[str],
    *,
    exclude: set[str],
) -> list[str]:
    out: list[str] = []
    for col in columns:
        if col in exclude:
            continue
        for r in rows:
            v = r.get(col)
            if v is None or v == "":
                continue
            if isinstance(v, str) and _PERIOD_RE.match(v):
                break  # 形如 "2026-01" 不算 numeric 列
            if _isNumberValue(v):
                out.append(col)
                break
    return out


def _dimensionColumns(
    rows: list[dict[str, Any]],
    columns: list[str],
    *,
    hints: set[str],
) -> list[str]:
    """维度列 = hints ∪ {非空、非纯数字、唯一值 ≤ MAX_TOP_N_DIM_UNIQUE}。"""
    out = [c for c in columns if c in hints]
    for col in columns:
        if col in hints:
            continue
        unique: set[str] = set()
        hasAny, allNumOrDate = False, True
        for r in rows:
            v = r.get(col)
            if v is None or v == "":
                continue
            hasAny = True
            if _isNumberValue(v):
                continue
            if isinstance(v, str) and _PERIOD_RE.match(v):
                continue
            allNumOrDate = False
            unique.add(str(v))
        if not hasAny or allNumOrDate:
            continue
        if len(unique) > MAX_TOP_N_DIM_UNIQUE:
            continue
        out.append(col)
    return out


def _distribution(
    rows: list[dict[str, Any]],
    numericCols: list[str],
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for col in numericCols:
        vals = [n for r in rows if (n := _asNumber(r.get(col))) is not None]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        std = (
            math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
            if len(vals) > 1
            else 0.0
        )
        out[col] = {
            "min": round(min(vals), DISTRIBUTION_DECIMALS),
            "max": round(max(vals), DISTRIBUTION_DECIMALS),
            "mean": round(mean, DISTRIBUTION_DECIMALS),
            "std": round(std, DISTRIBUTION_DECIMALS),
        }
    return out


def _topContributors(
    rows: list[dict[str, Any]],
    dimCols: list[str],
    *,
    maxTopN: int,
) -> list[dict[str, Any]]:
    if not dimCols:
        return []
    dim = dimCols[0]
    counts: dict[str, int] = {}
    for r in rows:
        v = r.get(dim)
        if v is None or v == "":
            continue
        counts[str(v)] = counts.get(str(v), 0) + 1
    total = sum(counts.values())
    if total == 0:
        return []
    items = [
        {"dim": dim, "value": k, "count": c, "sharePct": round(c / total * 100, SHARE_PCT_DECIMALS)}
        for k, c in counts.items()
    ]
    items.sort(key=lambda r: (-r["count"], r["value"]))
    return items[:maxTopN]


def _findPeriodColumn(
    rows: list[dict[str, Any]],
    columns: list[str],
) -> str | None:
    for col in columns:
        for r in rows:
            v = r.get(col)
            if isinstance(v, str) and _PERIOD_RE.match(v):
                return col
    return None


def _mom(
    rows: list[dict[str, Any]],
    periodCol: str | None,
    numericCols: list[str],
) -> list[dict[str, Any]]:
    if periodCol is None or not numericCols:
        return []
    valCol = numericCols[0]
    series = sorted(
        ((r[periodCol], n)
         for r in rows
         if isinstance(r.get(periodCol), str) and _PERIOD_RE.match(r[periodCol])
         and (n := _asNumber(r.get(valCol))) is not None),
        key=lambda x: x[0],
    )
    out: list[dict[str, Any]] = []
    for i in range(1, len(series)):
        prevPeriod, prevVal = series[i - 1]
        curPeriod, curVal = series[i]
        out.append({
            "period": curPeriod,
            "value": curVal,
            "deltaPct": None if prevVal == 0 else round((curVal - prevVal) / prevVal * 100, DELTA_PCT_DECIMALS),
        })
    return out


def _anomalies(
    rows: list[dict[str, Any]],
    numericCols: list[str],
) -> list[dict[str, Any]]:
    if not numericCols:
        return []
    col = numericCols[0]
    vals = [(i, n) for i, r in enumerate(rows) if (n := _asNumber(r.get(col))) is not None]
    if len(vals) < 2:
        return []
    nums = [v for _, v in vals]
    mean = sum(nums) / len(nums)
    std = math.sqrt(sum((v - mean) ** 2 for v in nums) / (len(nums) - 1))
    if std == 0:
        return []
    out: list[dict[str, Any]] = []
    for idx, v in vals:
        z = round((v - mean) / std, ZSCORE_DECIMALS)
        if z <= ANOMALY_Z_THRESHOLD:
            continue
        row = rows[idx]
        dimVal = next((row[c] for c in row if c != col and row[c] not in (None, "")), idx)
        out.append({"dim": col, "value": dimVal, "zScore": z})
    out.sort(key=lambda r: -r["zScore"])
    return out
