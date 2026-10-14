"""消歧分类器：让 LLM 只回答一个**语义标签**。

**这是本特性里 LLM 唯一的职责。** 它不定图型、不写 ECharts、不填 spec 字段 ——
只在一个问题上表态：「这份结果该按什么语义读」。规则判形状（1 维 1 指标），
语义分意图（占比 vs 分类比较），这是纯形状判据做不到的那一半。

**为什么值得**：`供应商 + 数量` 这三行在「占比」与「分类比较」两种问句下**形状完全
相同**。没有这一层，只能二选一固定成柱状或饼图，永远错一半。

**四条失败路径全部安全**（异常 / 超时 / 不在白名单 / 返回图型名）→ `label=None`，
调用方沿用规则原判。**LLM 挂了绝不影响出图**，最坏结果是「按规则选」。

**prompt 只带 ≤5 行样本**：分类要的是形状直觉，塞全量数据只烧 token 不提准确率。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.infrastructure.llm.base_client import LlmMessage
from app.services.chart_decision import SEMANTIC_LABELS
from app.utils.column_types import infer_column_types

logger = logging.getLogger(__name__)

# 样本行数上限。分类只需要看几行判断「这是明细还是聚合」。
LABEL_SAMPLE_ROWS = 5

# 单元格文本截断：一行里塞 2000 字的长文本对分类毫无帮助，只是 token。
_CELL_MAX_CHARS = 40

_SYSTEM_PROMPT = (
    "你是图表语义分类器。你只输出一个词，不输出任何其他内容，"
    "不输出图表类型名称，不输出任何 ECharts 配置或代码。"
)

_LABEL_HINTS = {
    "TREND": "时间趋势（随日期/月份变化）",
    "SHARE": "占比构成（各部分占总量的比例）",
    "RANK": "排名（谁最多/最少、前 N）",
    "COMPARE": "分类比较（几个类别之间比大小）",
    "RELATION": "两个指标之间的关系/相关性",
    "DETAIL": "明细清单（逐行原始记录）",
    "KPI": "单个指标值（一行一个数）",
}

# 解析前要剥掉的包裹物：代码围栏、引号、星号、句末标点。
_STRIP_CHARS = "`*\"'。.,;：: \t\r\n"


@dataclass(frozen=True)
class LabelResult:
    """分类结果。label=None 表示「没拿到可用标签」，等价于「按规则走」。"""

    label: str | None
    promptTokens: int
    completionTokens: int
    cachedTokens: int | None


def _normalizeLabel(content: str) -> str | None:
    """把回复规整成大写标签；不在白名单返回 None。

    只做「剥壳 + 大写 + 精确匹配」：契约要求模型只输出一个词，所以**不做模糊
    匹配**。宁可判为解析失败（退回规则，结果稳定），也不要猜模型想说什么。
    """
    if not content:
        return None
    normalized = content.strip().strip(_STRIP_CHARS).strip().upper()
    return normalized if normalized in SEMANTIC_LABELS else None


def _sampleRows(data: list[dict], columns: list[str]) -> str:
    """把前几行渲染成紧凑表格文本（截断长单元格）。"""
    lines: list[str] = []
    for row in data[:LABEL_SAMPLE_ROWS]:
        cells = []
        for col in columns:
            value = row.get(col)
            text = "" if value is None else str(value)
            cells.append(text[:_CELL_MAX_CHARS])
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def _buildPrompt(question: str, columns: list[str], data: list[dict]) -> str:
    columnTypes = infer_column_types(columns, data)
    labels = "\n".join(
        f"- {label}：{_LABEL_HINTS[label]}" for label in sorted(SEMANTIC_LABELS)
    )
    return "\n".join(
        [
            f"用户问句：{question}",
            f"结果列：{', '.join(columns)}",
            "列类型：" + ", ".join(f"{c}={columnTypes.get(c)}" for c in columns),
            f"结果行数：{len(data)}",
            f"前 {min(len(data), LABEL_SAMPLE_ROWS)} 行：",
            _sampleRows(data, columns),
            "",
            "请从下列标签中选恰好一个，表示这份结果适合怎样阅读：",
            labels,
            "",
            "只输出标签本身（例如 SHARE），不要输出别的。",
        ]
    )


async def classifySemanticLabel(
    *,
    question: str,
    columns: list[str],
    data: list[dict],
    llmClient: Any,
    modelConfig: Any,
) -> LabelResult:
    """调 LLM 拿一个语义标签。**绝不抛错、绝不阻塞出图**。

    token 计量沿用既有口径（promptTokens/completionTokens/cachedTokens）：
    调用成功但**解析失败**时仍要记账 —— token 已经花了，不记就是漏账。
    """
    prompt = _buildPrompt(question, columns, data)
    try:
        response = await llmClient.complete(
            messages=[
                LlmMessage(role="system", content=_SYSTEM_PROMPT),
                LlmMessage(role="user", content=prompt),
            ],
            model=modelConfig.model_name,
        )
    except Exception as exc:  # noqa: BLE001 - 分类失败必须优雅降级
        logger.warning("图表语义分类调用失败，按规则原判继续: %s", exc)
        return LabelResult(
            label=None, promptTokens=0, completionTokens=0, cachedTokens=0
        )

    label = _normalizeLabel(getattr(response, "content", "") or "")
    if label is None:
        logger.info(
            "图表语义分类未命中白名单（%r），按规则原判继续",
            (getattr(response, "content", "") or "")[:40],
        )
    return LabelResult(
        label=label,
        promptTokens=int(getattr(response, "promptTokens", 0) or 0),
        completionTokens=int(getattr(response, "completionTokens", 0) or 0),
        cachedTokens=getattr(response, "cachedTokens", None),
    )
