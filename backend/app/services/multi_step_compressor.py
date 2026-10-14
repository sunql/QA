"""多步上下文动态压缩（spec §5，方案③：行截断 + 关键列提取 + 极值点）。

零额外 LLM 调用；原始 data 永不删除，压缩结果另存 data_compressed。
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.utils.json_safe import jsonSafe

COMPRESS_THRESHOLD = 0.7
DEFAULT_MAX_ROWS = 30
MAX_DISTINCT_VALUES = 50
TOP_N_EXTREMES = 5

_TIME_HINT = re.compile(
    r"(date|time|month|year|week|day|quarter|日期|时间|月份|年份|周|季度)", re.IGNORECASE
)

_KIND_TIME = "time"
_KIND_NUMERIC = "numeric"
_KIND_CATEGORY = "category"


def classifyColumn(name: str, values: list) -> str:
    if _TIME_HINT.search(name or ""):
        return _KIND_TIME
    present = [v for v in values if v is not None]
    if present and all(_isNumber(v) for v in present):
        return _KIND_NUMERIC
    return _KIND_CATEGORY


def compressStepData(rows: list[dict], *, maxRows: int = DEFAULT_MAX_ROWS) -> dict:
    if not rows:
        return {"rows": [], "columns": {}, "meta": {"original_rows": 0, "compressed_rows": 0, "ratio": 1.0}}

    columns = list(rows[0].keys())
    summary: dict[str, dict] = {}
    for column in columns:
        values = [row.get(column) for row in rows]
        summary[column] = _summarizeColumn(column, values, rows, columns)

    kept = [_jsonSafeRow(row) for row in rows[:maxRows]]
    original = len(rows)
    return {
        "rows": kept,
        "columns": summary,
        "meta": {
            "original_rows": original,
            "compressed_rows": len(kept),
            "ratio": round(len(kept) / original, 4),
        },
    }


def estimatePromptTokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4


def shouldCompress(
    estimatedTokens: int, maxInputTokens: int, *, threshold: float = COMPRESS_THRESHOLD
) -> bool:
    if maxInputTokens <= 0:
        return False
    return estimatedTokens > maxInputTokens * threshold


def _summarizeColumn(
    column: str, values: list, rows: list[dict], columns: list[str]
) -> dict:
    present = [v for v in values if v is not None]
    kind = classifyColumn(column, values)

    if kind == _KIND_TIME:
        return {"distinct": _distinctSorted(present)}

    if kind == _KIND_NUMERIC:
        numbers = [float(v) for v in present if _isNumber(v)]
        if not numbers:
            return {"distinct": _distinctSorted(present)[:MAX_DISTINCT_VALUES]}
        summary = {
            "max": max(numbers),
            "min": min(numbers),
            "avg": round(sum(numbers) / len(numbers), 4),
            "sum": round(sum(numbers), 4),
        }
        ranked = sorted(
            (row for row in rows if _isNumber(row.get(column))),
            key=lambda row: abs(float(row[column])),
            reverse=True,
        )[:TOP_N_EXTREMES]
        summary["top"] = [{c: _jsonSafe(row.get(c)) for c in columns} for row in ranked]
        return summary

    return {"distinct": _distinctSorted(present)[:MAX_DISTINCT_VALUES]}


def _distinctSorted(values: list) -> list:
    return sorted({_jsonSafe(v) for v in values if v is not None}, key=lambda v: str(v))


def _jsonSafeRow(row: dict) -> dict:
    """返回归一化后的新行（不改调用方的字典）。"""
    return {column: _jsonSafe(value) for column, value in row.items()}


def _jsonSafe(value: object) -> object:
    """把 DB 原值归一为 JSON 原生类型。

    `data_compressed` 是裸 JSONB 列（无 `default=str` 编码器），Decimal/datetime
    直接写入会抛 TypeError；而 `rows` / `top` / `distinct` 三处都会带出 DB 原值。
    归一规则收敛到 SSOT `app.utils.json_safe.jsonSafe`（多步落库路径同用一份，
    避免两处 Decimal/datetime 规则漂移）。
    """
    return jsonSafe(value)


def _isNumber(value: object) -> bool:
    if isinstance(value, bool):
        return False
    # 含 Decimal：业务库数值列常以 Decimal 返回
    # （同 data_summary._to_float / chat_multistep._summarizeStepData）。
    return isinstance(value, (int, float, Decimal))
