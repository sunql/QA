"""图表服务。

- recommendChartType：基于列类型与数据量规则推荐图表类型（不调 LLM）
- generateChartOption：LLM 生成 ECharts option，失败回退到纯规则 _fallbackOption
- _fallbackOption：纯 Python 生成 TABLE/PIE/BAR/LINE 的 ECharts option
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.domain.enums import ChartType
from app.infrastructure.llm.base_client import LlmMessage
from app.services.messages_zh import MSG_CHART_TITLE_PIE, MSG_CHART_TITLE_RESULT

logger = logging.getLogger(__name__)

# 提取回复中的 JSON 对象（贪婪匹配首个 { 到最后一个 }）
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# 时间列匹配：2026-08-01 / 2026/08/01
_DATETIME_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")

# 饼图最多展示的维度行数；超过则建议柱状
_PIE_ROW_LIMIT = 6

_COLUMN_TYPE_STRING = "STRING"
_COLUMN_TYPE_TIME = "TIME"
_COLUMN_TYPE_NUMBER = "NUMBER"


class ChartService:
    """图表类型推荐与 ECharts option 生成。"""

    def recommendChartType(self, columns: list[str], data: list[dict]) -> ChartType:
        """按列类型与数据量规则推荐图表类型。"""
        if not columns or not data:
            return ChartType.TABLE
        types = self._inferColumnTypes(columns, data)
        stringCols = [c for c, t in types.items() if t == _COLUMN_TYPE_STRING]
        timeCols = [c for c, t in types.items() if t == _COLUMN_TYPE_TIME]
        numberCols = [c for c, t in types.items() if t == _COLUMN_TYPE_NUMBER]

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
        return parsed, promptTokens, completionTokens

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
        stringCols = [c for c in columns if types[c] == _COLUMN_TYPE_STRING]
        timeCols = [c for c in columns if types[c] == _COLUMN_TYPE_TIME]
        numberCols = [c for c in columns if types[c] == _COLUMN_TYPE_NUMBER]

        dimCol = stringCols[0] if stringCols else (timeCols[0] if timeCols else columns[0])
        measureCol = numberCols[0] if numberCols else columns[-1]
        names = [str(row.get(dimCol)) for row in data]
        values = [self._toNumber(row.get(measureCol)) for row in data]

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
        return {col: self._inferColumnType([row.get(col) for row in data]) for col in columns}

    def _inferColumnType(self, values: list[Any]) -> str:
        sample = [v for v in values if v is not None]
        if not sample:
            return _COLUMN_TYPE_STRING
        if any(self._isNumber(v) for v in sample):
            return _COLUMN_TYPE_NUMBER
        if all(self._looksLikeDatetime(v) for v in sample):
            return _COLUMN_TYPE_TIME
        return _COLUMN_TYPE_STRING

    @staticmethod
    def _isNumber(value: Any) -> bool:
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)

    @staticmethod
    def _looksLikeDatetime(value: Any) -> bool:
        if isinstance(value, (datetime, date)):
            return True
        if isinstance(value, str) and _DATETIME_RE.match(value):
            return True
        return False

    @staticmethod
    def _toNumber(value: Any) -> Any:
        """将值转换为 JSON 安全的数值表示（Decimal -> str）。"""
        if value is None:
            return None
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        return str(value)

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
        sample = json.dumps(data[:20], ensure_ascii=False, default=str)
        return (
            "你是一名前端数据可视化专家，请根据查询结果生成一份 ECharts 配置。\n"
            f"图表类型：{chartType.value}\n"
            f"列：{columns}\n"
            f"数据样本（最多 20 行）：\n{sample}\n"
            f"原始问题：{question}\n\n"
            "要求：\n"
            "1. 只返回一个合法的 JSON 对象（ECharts option），不要包含 markdown 代码块或额外文字。\n"
            "2. option 必须包含 title、tooltip、series，且 series[0].type 与指定图表类型一致。\n"
            "3. 数值统一为 JSON 数字类型。\n"
            "4. 若图表类型为 table，返回 {\"columns\": [...], \"rows\": [...]}。"
        )
