"""问题范围感知的行数限制（从 nl2sql_service 拆出）。

纯函数模块：按问题/计划判断查询是否被收窄（时间范围、显式条数/最值、过滤条件），
据此决定 rowLimit 兜底，返回新计划（frozen dataclass，绝不原地修改）。

未来扩展：新增一种「范围判定」信号（如某实体类型、某地域）加一个纯函数，
在 _applyScopeRowLimit 的决策顺序里登记即可。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from app.config import getSettings
from app.domain.query_plan import QueryPlan
from app.services.nl2sql_refine import _extractLimit


# 时间范围表达：只认"带数字/带指示词"的确定表达，不认裸粒度词
# （「按月」「年份」是分组粒度而非范围，见 refs._TIME_BUCKET_TOKENS）。
_SCOPE_TIME_RE = re.compile(
    r"(?:19|20)\d{2}\s*[-/年]"                              # 2024年 / 2024-05
    r"|[〇零一二三四五六七八九]{4}\s*年"                       # 二〇二四年 / 二零二四年
    r"|(?<!\d)\d{1,2}\s*月"                                 # 5月（"3个月"不命中）
    r"|[一二三四五六七八九十]{1,3}月"                          # 十二月
    r"|Q[1-4](?![0-9A-Za-z])|第?[一二三四1-4]\s*季度"          # Q1 / 第一季度
    r"|(?:今|本|去|上|前|明|下)\s*(?:年|月|周|季度|季)"         # 今年 / 上月 / 去年
    r"|(?:最近|近|过去|未来)\s*\d+\s*(?:年|个月|月|周|天|日|季度)"  # 近30天
    r"|今天|昨天|前天|明天|后天|本周|上周|至今|以来|截至",
    re.IGNORECASE,
)

# 显式条数/最值语义：命中时行数由用户意图决定，策略不介入。
# （"前 N/top N/限 N" 复用 refine._extractLimit，口径与捷径一致）
_EXPLICIT_ROW_INTENT_RE = re.compile(r"最多|最少|最大|最小|最高|最低|最新|排名|榜")


def _coerceRowLimit(value: Any) -> int | None:
    """把 rowLimit 归一化为正整数或 None。

    QueryPlan.from_dict 对 rowLimit 不做类型校验（query_plan.py:153），
    LLM 可能给出 "100" / -1 / 对象；不归一会让 planToText 渲染出
    「行数限制：{'a': 1}」这类噪音喂回模型。
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _hasTimeScope(text: str) -> bool:
    """问题是否限定了时间范围（某年/某月/某季度/相对时间）。"""
    return bool(text) and _SCOPE_TIME_RE.search(text) is not None


def _hasExplicitRowIntent(text: str) -> bool:
    """问题是否已表达条数或最值意图（前 10 条 / top 5 / 采购额最高的供应商）。"""
    return bool(text) and (
        _extractLimit(text) is not None or _EXPLICIT_ROW_INTENT_RE.search(text) is not None
    )


def _hasQueryScope(question: str, plan: QueryPlan) -> bool:
    """查询是否被收窄：问题含时间范围，或计划声明了过滤条件。

    不按"供应商/客户/产品"等类型名词判定——名词出现不等于有过滤
    （"各供应商采购汇总"是全表扫描）；plan.conditions 才是模型看过 schema 后
    对 WHERE 的结构化声明，是查询是否真的被收窄的可靠事实。
    """
    return _hasTimeScope(question) or bool(plan.conditions)


def _applyScopeRowLimit(
    plan: QueryPlan, question: str, *, defaultLimit: int | None = None
) -> QueryPlan:
    """按问题范围决定行数限制，返回新计划（frozen dataclass，绝不原地修改）。

    决策顺序见 changes/feat-scope-aware-row-limit/summary.md：
    无法回答 > 显式条数/最值 > 有范围 > 聚合分组 > 无范围兜底。
    defaultLimit 为 None 时取 settings.nl2sqlNoScopeRowLimit；该值 <= 0 表示关闭兜底。
    """
    current = _coerceRowLimit(plan.rowLimit)
    normalized = plan if plan.rowLimit == current else replace(plan, rowLimit=current)
    if plan.isUnanswerable or _hasExplicitRowIntent(question):
        return normalized
    if _hasQueryScope(question, plan):
        return normalized if normalized.rowLimit is None else replace(normalized, rowLimit=None)
    if plan.aggregations or plan.groupBy:
        return normalized
    limit = getSettings().nl2sqlNoScopeRowLimit if defaultLimit is None else defaultLimit
    return normalized if limit <= 0 else replace(normalized, rowLimit=limit)
