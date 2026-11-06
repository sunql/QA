"""L4 执行轨迹（Agent Trace）快照构造。

纯函数模块：把 loop 内部一次工具调用的输入/输出归一成前端可展示的摘要。
只保留**计数 / 耗时 / 状态 / 参数摘要**，永远不携带工具结果原文
（rows、columns 数据、完整 tool content）——那些既会撑爆 JSONB，也含不可信内容。

快照形状见 ``Harness/changes/2026-10-31-l4-agent-trace/summary.md``：

    {
      "kind": "l4_agent_trace",
      "status": "answered",              # answered | max_iterations
      "iterations": 3,
      "totalMs": 4210,
      "stepCount": 3,
      "steps": [ ... ],
    }
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

MAX_TRACE_STEPS = 50
MAX_SQL_PREVIEW_CHARS = 120
MAX_ERROR_CHARS = 200
MAX_JOIN_TABLES = 5
KIND = "l4_agent_trace"

_WHITESPACE = re.compile(r"\s+")


def _collapse(text: str) -> str:
    """把多行 SQL / 错误文本压成单行，避免卡片里出现无意义换行。"""
    return _WHITESPACE.sub(" ", text).strip()


def _truncate(text: str, limit: int) -> str:
    return _collapse(text)[:limit]


def _jsonObject(rawContent: str) -> dict[str, Any] | None:
    """工具 content 是 JSON 字符串；解析失败按「无摘要」处理，绝不抛错。"""
    try:
        parsed = json.loads(rawContent)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _count(value: Any) -> int | None:
    if isinstance(value, (list, tuple)):
        return len(value)
    return None


def _errorText(data: dict[str, Any] | None, rawContent: str) -> str:
    """错误摘要：优先 detail/reason，并保留 error 码；原文截断到上限。"""
    if data:
        code = data.get("error")
        for key in ("detail", "reason", "error"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            if key != "error" and isinstance(code, str) and code.strip():
                return _truncate(f"{code.strip()}: {value}", MAX_ERROR_CHARS)
            return _truncate(value, MAX_ERROR_CHARS)
    return _truncate(rawContent or "tool_error", MAX_ERROR_CHARS)


def summarizeArgs(toolName: str, args: Mapping[str, Any] | None) -> dict[str, Any]:
    """参数摘要（白名单制）：未知工具的参数一律不回显，防止任意内容进快照。"""
    args = args or {}
    if toolName == "execute_sql":
        sql = args.get("sql")
        if isinstance(sql, str) and sql.strip():
            return {"sqlPreview": _truncate(sql, MAX_SQL_PREVIEW_CHARS)}
        return {}
    if toolName in ("describe_table", "sample_rows"):
        table = args.get("table_name")
        return {"table": table} if isinstance(table, str) else {}
    if toolName == "list_joins":
        tables = args.get("tables")
        if isinstance(tables, str):
            tables = [tables]
        if isinstance(tables, Sequence) and not isinstance(tables, (str, bytes)):
            names = [item for item in tables if isinstance(item, str)][:MAX_JOIN_TABLES]
            return {"tables": names} if names else {}
        return {}
    return {}


def summarizeResult(
    toolName: str,
    rawContent: str,
    isError: bool = False,
) -> tuple[dict[str, Any], str | None]:
    """返回 ``(resultSummary, errorText)``；errorText 仅在失败时非空。"""
    data = _jsonObject(rawContent)
    if isError:
        return {}, _errorText(data, rawContent)
    if data is None:
        return {}, None

    if toolName == "list_tables":
        count = _count(data.get("tables"))
        return ({"tables": count} if count is not None else {}), None
    if toolName == "describe_table":
        count = _count(data.get("columns"))
        return ({"columns": count} if count is not None else {}), None
    if toolName == "sample_rows":
        count = _count(data.get("rows"))
        return ({"rows": count} if count is not None else {}), None
    if toolName == "list_joins":
        count = data.get("count")
        return ({"joins": count} if isinstance(count, int) else {}), None
    if toolName == "execute_sql":
        summary: dict[str, Any] = {}
        rowCount = data.get("row_count")
        if isinstance(rowCount, int):
            summary["rows"] = rowCount
        if data.get("truncated") is True:
            summary["truncated"] = True
        return summary, None
    return {}, None


def buildStep(
    *,
    index: int,
    iteration: int,
    tool: str,
    args: Mapping[str, Any] | None,
    rawContent: str,
    isError: bool,
    elapsedMs: float,
) -> dict[str, Any]:
    """构造单步轨迹。返回值是纯 JSON 数据（str/int/bool/dict），可直接落 JSONB。"""
    summary, error = summarizeResult(tool, rawContent, isError)
    step: dict[str, Any] = {
        "index": int(index),
        "iteration": int(iteration),
        "tool": str(tool),
        "status": "error" if isError else "ok",
        "elapsedMs": max(0, int(elapsedMs)),
        "args": summarizeArgs(tool, args),
        "resultSummary": summary,
    }
    if error:
        step["error"] = error
    return step


def buildTrace(
    *,
    steps: Sequence[dict[str, Any]],
    terminatedReason: str,
    iterations: int,
    totalMs: float,
) -> dict[str, Any]:
    """把逐步轨迹封成顶层快照；超上限截断并显式标记 ``truncated``。"""
    capped = list(steps)[:MAX_TRACE_STEPS]
    trace: dict[str, Any] = {
        "kind": KIND,
        "status": str(terminatedReason),
        "iterations": int(iterations),
        "totalMs": max(0, int(totalMs)),
        "stepCount": len(capped),
        "steps": capped,
    }
    if len(steps) > MAX_TRACE_STEPS:
        trace["truncated"] = True
    return trace
