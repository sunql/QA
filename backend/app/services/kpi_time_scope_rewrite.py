"""存量 ESCAPE_HATCH 公式的 ``{time_scope}`` 改写建议（纯函数，零 IO）。

D5 裁决：脚本只产 dry-run 建议，不写库；admin 按建议在指标编辑表单逐条
确认改写（走既有审计）。time_column 缺失 ⇒ blocked（R3 不猜列名）。
插入启发式（建议文本仍需 admin 人工确认）：
- formula 无 WHERE ⇒ 追加 ` WHERE ({占位符})`；
- 有 WHERE ⇒ 第一个 WHERE 后插 `({占位符}) AND `。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.services.time_scope_renderer import TIME_SCOPE_PLACEHOLDER

ACTION_NOOP = "noop"
ACTION_SUGGEST = "suggest"
ACTION_BLOCKED = "blocked"

_RE_FIRST_WHERE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_PLACEHOLDER_IN_PARENS = f"({TIME_SCOPE_PLACEHOLDER})"


@dataclass(frozen=True)
class TimeScopeRewriteSuggestion:
    metricId: int
    metricName: str
    executionSource: str | None
    formula: str
    timeColumn: str | None
    action: str
    reason: str | None
    suggestedFormula: str | None


def buildTimeScopeRewriteSuggestions(
    metrics: Sequence[Mapping[str, object]],
) -> tuple[TimeScopeRewriteSuggestion, ...]:
    """ESCAPE_HATCH + formula 非空才参与；其余输入原样跳过。"""
    suggestions: list[TimeScopeRewriteSuggestion] = []
    for row in metrics:
        executionSource = (row.get("execution_source") or "").strip().upper() or None
        if executionSource != "ESCAPE_HATCH":
            continue
        formula = (row.get("formula") or "").strip()
        if not formula:
            continue
        timeColumn = (row.get("time_column") or "").strip() or None
        metricId = int(row["id"])
        metricName = str(row.get("metric_name") or "")
        if TIME_SCOPE_PLACEHOLDER in formula:
            suggestions.append(TimeScopeRewriteSuggestion(
                metricId=metricId, metricName=metricName,
                executionSource=executionSource, formula=formula,
                timeColumn=timeColumn, action=ACTION_NOOP,
                reason=None, suggestedFormula=None,
            ))
            continue
        if timeColumn is None:
            suggestions.append(TimeScopeRewriteSuggestion(
                metricId=metricId, metricName=metricName,
                executionSource=executionSource, formula=formula,
                timeColumn=None, action=ACTION_BLOCKED,
                reason="time_column 缺失：先治理补列（R3 不猜），再改写占位符",
                suggestedFormula=None,
            ))
            continue
        match = _RE_FIRST_WHERE.search(formula)
        if match:
            insertAt = match.end()
            suggested = (
                formula[:insertAt] + f" {_PLACEHOLDER_IN_PARENS} AND" + formula[insertAt:]
            )
            reason = "第一个 WHERE 后插入占位符（请人工确认不破坏既有条件语义）"
        else:
            suggested = f"{formula} WHERE {_PLACEHOLDER_IN_PARENS}"
            reason = "formula 无 WHERE，末尾追加（行级过滤在聚合前，ratio 语义安全）"
        suggestions.append(TimeScopeRewriteSuggestion(
            metricId=metricId, metricName=metricName,
            executionSource=executionSource, formula=formula,
            timeColumn=timeColumn, action=ACTION_SUGGEST,
            reason=reason, suggestedFormula=suggested,
        ))
    return tuple(suggestions)
