"""多步最终报告的结构化分节（2026-10-31）。

StepAggregator 产出的汇总是**一段纯文字**（0107），每步的图只活在 steps[i]。
本模块把文字按「### 步骤 N」切成 text 块，并在对应节末尾插入 chart 块；图负载
不在这里复制，只给 stepIndex，前端从 message.steps 取。

纯函数、无 IO、绝不抛错：模型输出是外部输入，格式出错就退回「原文 + 文末图」，
不能让一次渲染把整条回答带崩（与 chart_renderer 的「绝不抛错」同一原则）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# 宽松匹配 `## 步骤 1：描述` / `#### Step 1. 描述` / 全角冒号与顿号。
_HEADING_RE = re.compile(
    r"^#{1,4}\s*(?:步骤|Step)\s*(\d+)\s*[:：.、]?\s*(.*)$",
    re.IGNORECASE,
)
# 任意 Markdown 标题行（「### 结论」「## 分析报告」等）。非步骤标题也要收口上一节，
# 否则上一节的图会被推到结论之后。
_ANY_HEADING_RE = re.compile(r"^#{1,4}\s*\S")
_ORPHAN_TITLE = "关键图表"


def _field(step: Any, snake: str, camel: str | None = None) -> Any:
    """同时支持 ORM/DTO 对象（snake_case 属性）与回放快照（camelCase dict）。"""
    if isinstance(step, Mapping):
        if camel is not None and camel in step:
            return step[camel]
        return step.get(snake)
    return getattr(step, snake, None)


def _hasChart(step: Any) -> bool:
    return (
        _field(step, "sql") is not None
        and _field(step, "chart_type", "chartType") is not None
        and _field(step, "chart_option", "chartOption") is not None
    )


def _chartBlock(step: Any) -> dict[str, Any]:
    index = _field(step, "step_index", "stepIndex")
    title = _field(step, "description") or f"步骤 {int(index) + 1}"
    return {"kind": "chart", "stepIndex": int(index), "title": title}


def _splitSections(
    text: str, byIndex: Mapping[int, Any]
) -> tuple[list[dict[str, Any]], set[int]]:
    blocks: list[dict[str, Any]] = []
    buffer: list[str] = []
    pending: int | None = None
    referenced: set[int] = set()

    def flushText() -> None:
        content = "\n".join(buffer).strip("\n")
        if content.strip():
            blocks.append({"kind": "text", "content": content})
        buffer.clear()

    def flushChart() -> None:
        nonlocal pending
        if pending is not None:
            blocks.append(_chartBlock(byIndex[pending]))
            referenced.add(pending)
            pending = None

    for line in text.split("\n"):
        stripped = line.strip()
        match = _HEADING_RE.match(stripped)
        isHeading = match is not None or _ANY_HEADING_RE.match(stripped) is not None
        # 碰到任何标题且上一节确实要插图时才收口：正文与图一起在节末落地。上一节无图
        # （导语 / 失败步）则继续并入同一段 —— 否则「## 分析报告」会单独成块。
        if isHeading and pending is not None:
            flushText()
            flushChart()
        buffer.append(line)
        if match is None:
            continue
        index = int(match.group(1)) - 1
        if index in byIndex and index not in referenced:
            pending = index
    flushText()
    flushChart()
    return blocks, referenced


def buildReportSections(
    answer: str, steps: Sequence[Any] | None
) -> list[dict[str, Any]]:
    """把最终回答切成 text/chart 块；不合法输入一律降级，不抛错。"""
    text = answer.strip() if isinstance(answer, str) else ""
    if not text:
        return []
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
        return []

    byIndex: dict[int, Any] = {}
    for step in steps:
        index = _field(step, "step_index", "stepIndex")
        if isinstance(index, int) and _hasChart(step):
            byIndex.setdefault(index, step)
    if not byIndex:
        return []

    blocks, referenced = _splitSections(text, byIndex)
    if not referenced:
        # 模型完全没按格式输出：原文 + 文末图，功能不丢。
        return [{"kind": "text", "content": text}] + [
            _chartBlock(step) for _, step in sorted(byIndex.items())
        ]

    # 任何有图但未被引用的成功步骤都补齐，按 step_index 升序 —— 图绝不静默丢失。
    missing = [(index, step) for index, step in sorted(byIndex.items()) if index not in referenced]
    if missing:
        blocks.append({"kind": "text", "content": f"### {_ORPHAN_TITLE}"})
        blocks.extend(_chartBlock(step) for _, step in missing)
    return blocks
