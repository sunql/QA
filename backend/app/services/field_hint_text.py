"""字段治理提示：`fieldHints` → 回答末尾文案（纯函数，零 IO）。

**为什么放正文而不是结构化提示条**（已确认的决策）：追加进最终回答并随消息落库，
PDF 导出 / 历史回放 / 研究报告天然继承，不需要每个消费端各写一套 UI。

**两类提示**：缺中文名、数值列缺单位。字段最多列 8 个，其余显示「等 N 个」。
**无内容时返回空串**（调用方直接拼接即可），保证存量回答逐字不变。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.services.messages_zh import (
    MSG_FIELD_HINT_MISSING_LABEL,
    MSG_FIELD_HINT_MISSING_UNIT,
    MSG_FIELD_HINT_TITLE,
    MSG_FIELD_HINT_TRUNCATED,
)

MAX_HINT_FIELDS = 8
_HINT_KEYS = ("missingLabels", "missingUnits")

__all__ = [
    "MAX_HINT_FIELDS",
    "appendFieldHints",
    "columnLabelsOf",
    "composeFieldHintText",
    "fieldHintsOf",
    "hintsFromSteps",
    "mergeFieldHints",
]


def _fieldsOf(hints: Any, key: str) -> list[str]:
    if not isinstance(hints, dict):
        return []
    values = hints.get(key)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value]


def mergeFieldHints(*hints: Any) -> dict[str, list[str]]:
    """合并多份 hints，去重且保持首次出现顺序。"""
    merged: dict[str, list[str]] = {key: [] for key in _HINT_KEYS}
    for hint in hints:
        for key in _HINT_KEYS:
            for field in _fieldsOf(hint, key):
                if field not in merged[key]:
                    merged[key].append(field)
    return merged


def _joinNames(fields: list[str], columnLabels: Mapping[str, str] | None) -> str:
    labels = columnLabels or {}
    shown = [labels.get(field, field) for field in fields[:MAX_HINT_FIELDS]]
    text = "、".join(shown)
    if len(fields) > MAX_HINT_FIELDS:
        return text + MSG_FIELD_HINT_TRUNCATED.format(count=len(fields))
    return text


def composeFieldHintText(
    hints: Any, *, columnLabels: Mapping[str, str] | None = None
) -> str:
    """hints → 以 `\\n\\n` 开头的提示段；无内容返回空串。"""
    labels = _fieldsOf(hints, "missingLabels")
    units = _fieldsOf(hints, "missingUnits")
    if not labels and not units:
        return ""
    lines = [MSG_FIELD_HINT_TITLE]
    if labels:
        lines.append(
            MSG_FIELD_HINT_MISSING_LABEL.format(
                fields=_joinNames(labels, columnLabels)
            )
        )
    if units:
        lines.append(
            MSG_FIELD_HINT_MISSING_UNIT.format(
                fields=_joinNames(units, columnLabels)
            )
        )
    return "\n\n" + "\n".join(lines)


def appendFieldHints(
    answer: str, hints: Any, *, columnLabels: Mapping[str, str] | None = None
) -> str:
    """在回答末尾追加提示；无内容时原样返回。"""
    text = composeFieldHintText(hints, columnLabels=columnLabels)
    return f"{answer}{text}" if text else answer


def fieldHintsOf(payload: Any) -> dict[str, Any] | None:
    """从 chart_option 取 fieldHints；缺键/形状不对 → None（旧消息不提示）。"""
    if not isinstance(payload, dict):
        return None
    hints = payload.get("fieldHints")
    if not isinstance(hints, dict):
        return None
    return {
        "missingLabels": _fieldsOf(hints, "missingLabels"),
        "missingUnits": _fieldsOf(hints, "missingUnits"),
    }


def columnLabelsOf(*payloads: Any) -> dict[str, str]:
    """合并多份负载里的 columnLabels（后写覆盖，与渲染器的取值口径一致）。"""
    labels: dict[str, str] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        raw = payload.get("columnLabels")
        if not isinstance(raw, dict):
            continue
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str) and value:
                labels[key] = value
    return labels


def hintsFromSteps(steps: Iterable[Any]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """多步：汇总每步 chart_option/table_option 的 hints 与 labels。"""
    hints: list[Any] = []
    labels: dict[str, str] = {}
    for step in steps:
        chart_option = getattr(step, "chart_option", None)
        table_option = getattr(step, "table_option", None)
        hints.append(fieldHintsOf(chart_option))
        labels.update(columnLabelsOf(chart_option, table_option))
    return mergeFieldHints(*hints), labels
