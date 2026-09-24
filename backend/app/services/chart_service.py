"""图表服务。

- recommendChartType：基于列类型与数据量规则推荐图表类型（不调 LLM）
- generateChartOption：LLM 生成 ECharts option，失败回退到纯规则 _fallbackOption
- _fallbackOption：纯 Python 生成 TABLE/PIE/BAR/LINE 的 ECharts option
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.domain.enums import ChartType
from app.infrastructure.llm.base_client import LlmMessage
from app.services.messages_zh import MSG_CHART_TITLE_PIE, MSG_CHART_TITLE_RESULT
from app.services.data_summary import FULL_DATA_THRESHOLD
from app.utils.column_types import (
    COLUMN_TYPE_NUMBER,
    COLUMN_TYPE_STRING,
    COLUMN_TYPE_TIME,
    infer_column_type,
    infer_column_types,
    is_number,
    looks_like_datetime,
    to_json_number,
)

logger = logging.getLogger(__name__)

# 提取回复中的 JSON 对象（贪婪匹配首个 { 到最后一个 }）
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# 饼图最多展示的维度行数；超过则建议柱状
_PIE_ROW_LIMIT = 6


class ChartService:
    """图表类型推荐与 ECharts option 生成。"""

    def recommendChartType(self, columns: list[str], data: list[dict]) -> ChartType:
        """按列类型与数据量规则推荐图表类型。"""
        if not columns or not data:
            return ChartType.TABLE
        types = self._inferColumnTypes(columns, data)
        stringCols = [c for c, t in types.items() if t == COLUMN_TYPE_STRING]
        timeCols = [c for c, t in types.items() if t == COLUMN_TYPE_TIME]
        numberCols = [c for c, t in types.items() if t == COLUMN_TYPE_NUMBER]

        if len(stringCols) == 1 and not timeCols and len(numberCols) == 1:
            return ChartType.PIE if len(data) <= _PIE_ROW_LIMIT else ChartType.BAR
        if timeCols and numberCols:
            return ChartType.LINE
        if len(stringCols) >= 2 and numberCols:
            return ChartType.BAR
        return ChartType.TABLE

    async def generateChartOption(
        self,
        chartType: ChartType,
        columns: list[str],
        data: list[dict],
        question: str,
        llmClient: Any,
        modelConfig: Any,
    ) -> tuple[dict, int, int]:
        """生成 ECharts option。TABLE 走规则；其余类型 LLM 生成，失败回退规则。"""
        if chartType == ChartType.TABLE:
            return self._fallbackOption(chartType, columns, data), 0, 0

        prompt = self._buildOptionPrompt(chartType, columns, data, question)
        parsed: dict | None = None
        promptTokens = 0
        completionTokens = 0
        try:
            response = await llmClient.complete(
                messages=[
                    LlmMessage(role="system", content="你只输出合法的 JSON，不输出任何其他内容。"),
                    LlmMessage(role="user", content=prompt),
                ],
                model=modelConfig.model_name,
            )
            promptTokens = response.promptTokens
            completionTokens = response.completionTokens
            parsed = self._parseOptionJson(response.content)
        except Exception as exc:  # noqa: BLE001 - 图表失败应优雅降级
            logger.warning("图表 option 生成失败，回退到规则: %s", exc)
            parsed = None

        if parsed is None:
            parsed = self._fallbackOption(chartType, columns, data)
        else:
            # v3 2026-09-18：归一化 LLM 偶发写错的 ECharts 模板变量 `{d}`（仅 pie 百分比）
            # → 非 pie 场景下替换为 `{c}`（数值），防止柱图/线图显示字面量 `{d}%`。
            parsed = self._normalizeOptionFormatters(parsed, chartType)
        return parsed, promptTokens, completionTokens

    @staticmethod
    def _normalizeOptionFormatters(option: dict, chartType: ChartType) -> dict:
        """递归遍历 option，把所有字符串 formatter 里的 `{d}` 在非 pie 下替换为 `{c}`。

        ECharts 标准模板变量：
        - `{a}` series name / `{b}` category name / `{c}` value — 全图表可用
        - `{d}` 仅 pie 百分比 — 其他图表 ECharts 找不到替换目标，原样输出 `{d}` 字面量

        触发：用户报告问题 #1 柱图 label 显示字面量 `{d}%`、tooltip 仅 B019 有值（其他 series
        因为 formatter 失败回退到空字符串）。

        设计要点：
        - 不可变：返回新 dict，原 option 不变（CLAUDE.md 不可变数据原则）
        - 函数 formatter（`formatter: callable`）不动 —— LLM 写函数时意图明确
        - pie chart `{d}%` 保持原样 —— 是 ECharts 标准用法
        - 递归遍历 dict / list；其他类型原样保留
        """
        if chartType == ChartType.PIE:
            return option
        return _walkAndNormalizeFormatters(option)

    @staticmethod
    def _buildOptionPrompt(
        chartType: ChartType, columns: list[str], data: list[dict], question: str
    ) -> str:
        # v2 2026-09-18：数据量小（≤ FULL_DATA_THRESHOLD 行）时全量嵌入 prompt，
        # 让 LLM 生成的 ECharts option 与前端 EVENT_CHART.data 一致。
        # 否则截 data[:20]，与原行为一致（图表只看数据形状）。
        if len(data) <= FULL_DATA_THRESHOLD:
            sample = json.dumps(data, ensure_ascii=False, default=str)
            sampleLabel = f"数据（共 {len(data)} 行）："
        else:
            sample = json.dumps(data[:20], ensure_ascii=False, default=str)
            sampleLabel = "数据样本（最多 20 行）："
        return (
            "你是一名前端数据可视化专家，请根据查询结果生成一份 ECharts 配置。\n"
            f"图表类型：{chartType.value}\n"
            f"列：{columns}\n"
            f"{sampleLabel}\n{sample}\n"
            f"原始问题：{question}\n\n"
            "要求：\n"
            "1. 只返回一个合法的 JSON 对象（ECharts option），不要包含 markdown 代码块或额外文字。\n"
            "2. option 必须包含 title、tooltip、series，且 series[0].type 与指定图表类型一致。\n"
            "3. 数值统一为 JSON 数字类型。\n"
            "4. 若图表类型为 table，返回 {\"columns\": [...], \"rows\": [...]}。\n"
            "5. label / tooltip 的 formatter 若用字符串模板，柱图/线图/散点用 `{c}`（数值）或 "
            "`{b}`（类目），不要用 `{d}`（仅饼图百分比）。"
        )

    # =========================================================================
    # 规则 fallback
    # =========================================================================

    def _fallbackOption(
        self, chartType: ChartType, columns: list[str], data: list[dict]
    ) -> dict:
        """纯规则生成 ECharts option（LLM 失败时的兜底）。"""
        if chartType == ChartType.TABLE or not columns or not data:
            return {"columns": columns or [], "rows": data or []}

        types = self._inferColumnTypes(columns, data)
        stringCols = [c for c in columns if types[c] == COLUMN_TYPE_STRING]
        timeCols = [c for c in columns if types[c] == COLUMN_TYPE_TIME]
        numberCols = [c for c in columns if types[c] == COLUMN_TYPE_NUMBER]

        dimCol = stringCols[0] if stringCols else (timeCols[0] if timeCols else columns[0])
        measureCol = numberCols[0] if numberCols else columns[-1]
        names = [str(row.get(dimCol)) for row in data]
        values = [to_json_number(row.get(measureCol)) for row in data]

        if chartType == ChartType.PIE:
            return {
                "title": {"text": MSG_CHART_TITLE_PIE},
                "tooltip": {"trigger": "item"},
                "series": [
                    {
                        "type": "pie",
                        "radius": "60%",
                        "data": [
                            {"name": name, "value": value}
                            for name, value in zip(names, values, strict=True)
                        ],
                    }
                ],
            }

        if chartType in (ChartType.BAR, ChartType.LINE, ChartType.SCATTER):
            return {
                "title": {"text": MSG_CHART_TITLE_RESULT},
                "tooltip": {"trigger": "axis"},
                "xAxis": {"type": "category", "data": names},
                "yAxis": {"type": "value"},
                "series": [{"type": chartType.value, "data": values}],
            }

        return {"columns": columns, "rows": data}

    # =========================================================================
    # 列类型推断
    # =========================================================================

    def _inferColumnTypes(self, columns: list[str], data: list[dict]) -> dict[str, str]:
        # 委托给 SSOT（app/utils/column_types.infer_column_types），避免双份逻辑
        return infer_column_types(columns, data)

    def _inferColumnType(self, values: list[Any]) -> str:
        # 同上，单列版
        return infer_column_type(values)

    # =========================================================================
    # LLM 解析
    # =========================================================================

    @staticmethod
    def _parseOptionJson(content: str) -> dict | None:
        match = _JSON_OBJECT_RE.search(content)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and "series" in parsed:
            return parsed
        return None

    @staticmethod
    def _buildOptionPrompt(
        chartType: ChartType, columns: list[str], data: list[dict], question: str
    ) -> str:
        # v2 2026-09-18：数据量小（≤ FULL_DATA_THRESHOLD 行）时全量嵌入 prompt，
        # 让 LLM 生成的 ECharts option 与前端 EVENT_CHART.data 一致。
        # 否则截 data[:20]，与原行为一致（图表只看数据形状）。
        if len(data) <= FULL_DATA_THRESHOLD:
            sample = json.dumps(data, ensure_ascii=False, default=str)
            sampleLabel = f"数据（共 {len(data)} 行）："
        else:
            sample = json.dumps(data[:20], ensure_ascii=False, default=str)
            sampleLabel = "数据样本（最多 20 行）："
        return (
            "你是一名前端数据可视化专家，请根据查询结果生成一份 ECharts 配置。\n"
            f"图表类型：{chartType.value}\n"
            f"列：{columns}\n"
            f"{sampleLabel}\n{sample}\n"
            f"原始问题：{question}\n\n"
            "要求：\n"
            "1. 只返回一个合法的 JSON 对象（ECharts option），不要包含 markdown 代码块或额外文字。\n"
            "2. option 必须包含 title、tooltip、series，且 series[0].type 与指定图表类型一致。\n"
            "3. 数值统一为 JSON 数字类型。\n"
            "4. 若图表类型为 table，返回 {\"columns\": [...], \"rows\": [...]}。\n"
            "5. label / tooltip 的 formatter 若用字符串模板，柱图/线图/散点用 `{c}`（数值）或 "
            "`{b}`（类目），不要用 `{d}`（仅饼图百分比）。"
        )


def _walkAndNormalizeFormatters(node: Any) -> Any:
    """递归走 option 树，把所有字符串 formatter 里的 `{d}` → `{c}`（用于非 pie）。

    - dict：浅复制后递归每个 value（保留原 dict 不变）
    - list：浅复制后递归每个 item
    - key == "formatter" 且 value 是字符串：替换 `{d}` → `{c}`
    - 其他：原样保留
    """
    if isinstance(node, dict):
        newDict: dict = {}
        for key, value in node.items():
            if key == "formatter" and isinstance(value, str):
                newDict[key] = value.replace("{d}", "{c}")
            else:
                newDict[key] = _walkAndNormalizeFormatters(value)
        return newDict
    if isinstance(node, list):
        return [_walkAndNormalizeFormatters(item) for item in node]
    return node
