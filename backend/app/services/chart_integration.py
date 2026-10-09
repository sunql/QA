"""报告侧 chart 构建：复用 chat 侧 chart 链路，让 report 不再 chartType=None.

包装 chart_decision + chart_spec_builder + chart_renderer，输出前端 ECharts 友好的 dict。
不复制 chat_service.buildChart 的全功能（不调 LLM 消歧、不重排、不过 coerceSpec）：
只覆盖报告侧需求 —— plan=None + 决策引擎走最朴素路径「行/列→kind」。
失败降级为 TABLE 兜底，**绝不抛错**（不影响整份报告）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.chart_spec import ChartSpec
from app.domain.query_plan import QueryPlan
from app.services.chart_decision import buildChartSignals, decideChartKind
from app.services.chart_renderer import renderChartOption
from app.services.chart_spec_builder import buildSpec
from app.services.chart_thresholds import loadChartThresholds

logger = logging.getLogger(__name__)


async def buildReportChart(
    session: AsyncSession,
    *,
    rows: list[dict[str, Any]],
    columns: list[str],
    question: str = "",
    plan: QueryPlan | None = None,
    aliasOverrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """报告侧 chart 构建。返回 chat-style 形态（kind/columns/data/echartsOption）。

    永不抛错：决策引擎/渲染任一环节失败 → 退化为 TABLE 兜底。

    `aliasOverrides`（默认 None）：「结果集列名 → 显示名」，报告无 QueryPlan 时
    由调用方注入，否则图表标签只能是英文列名。
    """
    if not rows or not columns:
        return _emptyChart(columns)

    try:
        signals = buildChartSignals(plan, list(columns), rows, question)
        thresholds = await loadChartThresholds(session)
        decision = decideChartKind(signals, thresholds)
        spec: ChartSpec = buildSpec(decision, signals, rows, plan, aliasOverrides=aliasOverrides)
        echartsOption = renderChartOption(spec, rows)
        kindStr = spec.kind.value if hasattr(spec.kind, "value") else str(spec.kind)
        return {
            "title": spec.title,
            "chartType": kindStr,
            "columns": list(columns),
            "data": rows,  # R3-B 统一键名为 'data'
            "echartsOption": echartsOption,
        }
    except Exception:  # noqa: BLE001 — 报告渲染失败退化为 TABLE，不让整份报告炸
        logger.warning("buildReportChart 失败，退化为 TABLE 兜底", exc_info=True)
        return _emptyChart(columns, fallback_rows=rows)


def _emptyChart(
    columns: list[str], *, fallback_rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """空 / 失败兜底：TABLE 形态，让前端能渲染 + 后端 payload 不缺键."""
    return {
        "title": "（无图表数据）",
        "chartType": "table",
        "columns": list(columns),
        "data": fallback_rows or [],
        "echartsOption": None,
    }
