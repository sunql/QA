"""报告表示层：块/章节数据类 + payload 序列化 + MD 渲染（纯字符串模板，零 LLM）。

自 `report_planner.py` 拆出（R4 Task 1）：装配（取数 / 标签 / LLM 文本）留在
`report_planner`，本模块只负责**把已装配好的 payload 渲染成人和前端都能读的形态**。
搬迁是纯移动，零行为变化 —— `report_planner` 以 re-export 保持既有导入路径可用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "BLOCK_BULLET",
    "BLOCK_CHART",
    "BLOCK_TABLE",
    "BLOCK_TEXT",
    "CHART_HINT",
    "DEFAULT_TITLE",
    "REF_FINDING",
    "REF_WIKI",
    "ReportBlock",
    "ReportSection",
    "SourceRef",
    "blockToDict",
    "renderMarkdown",
    "sectionToDict",
]

# --- 段落标题（mode 模板用） --------------------------------------------------
_SECTION_TITLES: dict[str, str] = {
    "executive_summary": "执行摘要",
    "conclusion": "结论",
    "data": "数据章节",
    "knowledge": "引用知识",
    "methodology": "方法学",
    "hypothesis_table": "假设验证表",
    "alternative": "备选假设",
    "comparison_table": "对比维度表",
    "diff_analysis": "差异分析",
}

# --- 块类型 / SourceRef 类型 --------------------------------------------------
BLOCK_TEXT = "text"
BLOCK_CHART = "chart"
BLOCK_TABLE = "table"
BLOCK_BULLET = "bullet_list"
REF_FINDING = "finding"
REF_WIKI = "wiki"

# --- 文案 / 上限 --------------------------------------------------------------
DEFAULT_TITLE = "研究报告"
CHART_HINT = "图表见 payload"


# ---------------------------------------------------------------------------
# 报告形状（设计 §4.7 段落形状；frozen dataclass，序列化 camelCase 键）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """块级溯源：结论 / 知识条目（设计 §4.7 kind: step | finding | wiki | kpi）。"""

    kind: str
    refId: str
    label: str


@dataclass(frozen=True)
class ReportBlock:
    """段落内的一个块：text | chart | table | bullet_list。"""

    type: str
    content: Any
    sourceRefs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True)
class ReportSection:
    """报告段落（id/kind 同值，便于前端按 kind 取段）。"""

    id: str
    kind: str
    title: str
    blocks: tuple[ReportBlock, ...] = ()


# ---------------------------------------------------------------------------
# 序列化 / 取值助手
# ---------------------------------------------------------------------------


def blockToDict(block: ReportBlock) -> dict[str, Any]:
    return {
        "type": block.type,
        "content": block.content,
        "sourceRefs": [
            {"kind": ref.kind, "refId": ref.refId, "label": ref.label} for ref in block.sourceRefs
        ],
    }


def sectionToDict(section: ReportSection) -> dict[str, Any]:
    return {
        "id": section.id,
        "kind": section.kind,
        "title": section.title,
        "blocks": [blockToDict(block) for block in section.blocks],
    }


# ---------------------------------------------------------------------------
# MD 渲染（纯字符串模板，零 LLM）
# ---------------------------------------------------------------------------


def escapeCell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def mdTable(columns: list[str], rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(escapeCell(column) for column in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines += [
        "| " + " | ".join(escapeCell(row.get(column)) for column in columns) + " |" for row in rows
    ]
    return lines


def renderBlock(block: dict[str, Any]) -> list[str]:
    """单块 → MD 行。chart 块渲染为表格 + 「图表见 payload」提示（brief 指定）。

    R3-B：chart 块 key 统一为 'data'（不是 'rows'）；并打印 chartType 让读者在 MD
    里直接看到图表语义（BAR / PIE / ...），无需打开 payload。
    """
    kind, content = block["type"], block["content"]
    if kind == BLOCK_TEXT:
        return [str(content), ""]
    if kind == BLOCK_BULLET:
        return [f"- {item}" for item in content] + [""]
    if kind == BLOCK_TABLE:
        return mdTable(content["columns"], content["rows"]) + [""]
    if kind == BLOCK_CHART:
        data = content.get("data") or []
        if not data:
            chartType = content.get("chartType") or "table"
            return ["", f"> {CHART_HINT}（chartType={chartType}）", ""]
        columns = content.get("columns") or (list(data[0].keys()) if data else [])
        table = mdTable(columns, data)
        chartType = content.get("chartType") or "table"
        return [*table, "", f"> {CHART_HINT}（chartType={chartType}）", ""]
    logger.warning("报告 MD 渲染遇到未知块类型，跳过: %s", kind)
    return []


def renderMarkdown(payload: dict[str, Any]) -> str:
    """payload → Markdown（章节标题 + 块渲染；可下载存档）。"""
    lines = [
        f"# {payload.get('title') or DEFAULT_TITLE}",
        "",
        f"> 模式：{payload.get('mode')}｜问题：{payload.get('question')}",
        "",
    ]
    for section in payload.get("sections") or []:
        lines += [f"## {section['title']}", ""]
        for block in section["blocks"]:
            lines += renderBlock(block)
    return "\n".join(lines).strip() + "\n"
