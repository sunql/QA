"""可视化输出策略的 rationale 纯函数模块。

**它是什么**：把「为什么画这个图」压缩成一个不可变的 `VisualRationale`。下游
Task 3/4/5/6 全从这里的 `code` 拿判断依据，前端 i18n 用 `params` 里的插值变量
（rows/kind）拼文案。所以 code 取值必须与 `chart_decision` 的 ruleId 逐字一致。

**code 全集（21 个）**：
- `chart_decision` 的 18 个 ruleId（R00–R14）
- `chart_service` 的 `R_FORCED_CLIENT`（客户端/意图强制指定图型）
- 2 个本模块新常量：`DEGRADE_SPEC_INVALID`（spec 校验失败降级）、
  `SUMMARY_TEXT_ONLY`（多步汇总步骤，由 `summaryTextOnlyRationale()` 产出）

**约束**：
- 零 LLM、零 IO、零数据库 —— 纯函数，不 import 任何 DB / HTTP 模块。
- 未知 ruleId **不抛异常**，落 `R14_DEFAULT_TABLE` 兜底并 `logger.warning`。
- 不可变：`VisualRationale` 为 frozen dataclass，每次调用返回新对象。
- `params` 只放插值变量，不放文案；文案在前端 i18n。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.domain.enums import ChartType

logger = logging.getLogger(__name__)

# 需要携带行数插值变量的 code（前端 i18n 用它显示「共 N 行」）。
_ROWS_RULE_IDS: frozenset[str] = frozenset(
    {
        "R02_SHARE_DONUT",
        "R03_SHARE_OVERFLOW_HBAR",
        "R04_TOPN_HBAR",
        "R11_HBAR_MANY_ROWS",
    }
)

# chart_decision 的 18 个 ruleId + chart_service 的 R_FORCED_CLIENT。
# 两个新常量不在此列：它们只经 degradeReason / summaryTextOnlyRationale 产出。
_KNOWN_RULE_IDS: frozenset[str] = frozenset(
    {
        "R00_EMPTY_TABLE",
        "R01_SINGLE_VALUE_KPI",
        "R01S_SINGLE_ROW_TABLE",
        "R02_SHARE_DONUT",
        "R03_SHARE_OVERFLOW_HBAR",
        "R04_TOPN_HBAR",
        "R05_WATERFALL",
        "R06_COMBO",
        "R07_TREND_LINE",
        "R08_RELATION_SCATTER",
        "R09_MULTIDIM_HEATMAP",
        "R10_MULTIDIM_BAR",
        "R11_HBAR_MANY_ROWS",
        "R12S_QUESTION_SHARE_DONUT",
        "R12S_QUESTION_SHARE_HBAR",
        "R12_CATEGORY_BAR",
        "R13_RAW_DETAIL_TABLE",
        "R14_DEFAULT_TABLE",
        "R_FORCED_CLIENT",
    }
)

# 新常量：spec 校验失败降级 / 多步汇总步骤。code 值逐字透传到前端 i18n。
DEGRADE_SPEC_INVALID = "DEGRADE_SPEC_INVALID"
SUMMARY_TEXT_ONLY = "SUMMARY_TEXT_ONLY"

# 未知 ruleId 的兜底终点（同时也是 chart_decision 的最后一个规则）。
_FALLBACK_RULE_ID = "R14_DEFAULT_TABLE"


@dataclass(frozen=True)
class VisualRationale:
    """可视化判断依据：code（21 个之一）+ 插值变量（无文案）。"""

    code: str
    params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """序列化为线上契约形状 ``{"code": str, "params": {k: 原始值}}``。

        ``params`` 里可能装 ``ChartType`` 枚举成员（``(str, Enum)``）：它的真值是
        ``"heatmap"``，而 ``str(成员)`` 是 ``"ChartType.HEATMAP"``。SSE 层用
        ``json.dumps(..., default=str)`` 序列化裸 dict，不归一就会把
        ``"ChartType.HEATMAP"`` 发上线，前端 i18n 据此渲染「不满足
        ChartType.HEATMAP 的绘图要求」。所以这里在**唯一出口**把枚举成员显式
        归一成 ``.value``，其余值原样透传 —— 四个下线点（chart 事件 / step_result
        事件 / ChatResponse / StepResultRead）都调这一处，规则只写一遍。
        """
        return {
            "code": self.code,
            "params": {
                k: (v.value if isinstance(v, Enum) else v)
                for k, v in self.params.items()
            },
        }


def buildVisualRationale(
    *,
    ruleId: str,
    kind: ChartType,
    rowCount: int,
    degradeReason: str | None = None,
) -> VisualRationale:
    """把 ruleId 归一成 rationale。

    - `degradeReason` 非 None → `DEGRADE_SPEC_INVALID`（params.kind）。
    - 未知 ruleId → `R14_DEFAULT_TABLE` 兜底 + warning（绝不抛异常）。
    - 其余 → code=ruleId；params 按需带 rows（R02/R03/R04/R11）或 kind
      （R_FORCED_CLIENT）。
    """
    if degradeReason is not None:
        return VisualRationale(code=DEGRADE_SPEC_INVALID, params={"kind": kind})
    if ruleId not in _KNOWN_RULE_IDS:
        logger.warning("未知 ruleId %r → 兜底 %s", ruleId, _FALLBACK_RULE_ID)
        return VisualRationale(code=_FALLBACK_RULE_ID, params={})
    params: dict[str, Any] = {}
    if ruleId in _ROWS_RULE_IDS:
        params["rows"] = rowCount
    elif ruleId == "R_FORCED_CLIENT":
        params["kind"] = kind
    return VisualRationale(code=ruleId, params=params)


def summaryTextOnlyRationale() -> VisualRationale:
    """多步汇总步骤专用的 rationale：只有文字，没有图。"""
    return VisualRationale(code=SUMMARY_TEXT_ONLY, params={})
