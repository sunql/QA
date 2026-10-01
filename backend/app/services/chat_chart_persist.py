"""图表负载「从响应对象落到 session_message 两列」的归一（0105，图表进最终报告）。

**为什么单独一个模块**：这几个函数描述的是**落库契约**（列里存的是哪种字符串、
存进去的负载最大多大），不是聊天上下文的职责。它们原本住在 `chat_context.py`，
但那里是「多轮上下文与会话持久化」的编排层 —— 纯归一逻辑混在编排里，既让
`chat_context` 越过 800 行上限，也让「改了一个转换规则」的 diff 看起来像在改流程。

不可变：这些函数**绝不原地改**调用方手里那份（它还要发给前端）；无归一/截断时
原样返回入参，有变化时才返回新对象。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

# TABLE 负载落库时的行数上界。**防御性兜底，不是可调策略**，故按《魔数治理》
# 「判别不清」档留在源码里（不迁 system_config）：它不是运营想调的边界，是
# 「别把库里写爆」的闸。
# 为什么必须有：`QUERY_ROW_LIMIT` 默认 0 = 不限行（config.py），而
# `chart_renderer._buildTable` 返回的 rows 是**全量** data —— 一条「列出所有…」
# 的明细查询能把十万行塞进 session_message 的一行（DB 膨胀 + 导出 PDF 时同步
# 构建巨型表格会阻塞事件循环）。
# 注意这只截**落库份**：实时响应仍发全量（用户当场要的就是全部行）；历史回放
# 与导出 PDF 拿到的是截断份，由 `truncated` 标记如实告知，不假装是全部。
_PERSIST_MAX_TABLE_ROWS = 200


def chartTypeName(chartType: Any) -> str | None:
    """`ChartType` 枚举或裸字符串 → 列里存的那份字符串。

    调用点手里两种形状都有：`ChatResponse.chartType` 是枚举（pydantic 保留成员），
    多步路径回读的 `run.result.chart_type` 同样是枚举，而流式 `StreamEvent` 里早已
    `.value` 成字符串。列是 VARCHAR(20)，契约是「线上 chartType 的值」，所以在**唯一
    落库点**统一归一，而不是让六个调用点各自记得写 `.value` —— 忘了写不会报错，
    只会静默存进 `"ChartType.BAR"` 这种脏值。
    """
    if chartType is None:
        return None
    value = getattr(chartType, "value", chartType)
    return value if isinstance(value, str) else str(value)


def boundedChartOption(chartOption: dict | None) -> dict | None:
    """把 chart 负载归一到可落库形态：JSON 安全归一 + rows 截到上界。

    TABLE 类负载存的是原始行（`chart_renderer._buildTable` 返回全量 data），
    NUMERIC 列在 SQLAlchemy 手里是 `Decimal`，裸 ``json.dumps`` 会抛
    ``TypeError`` —— 与 `boundedTableOption` 同走 `_jsonSafe` 归一
    （`Decimal→float`、`datetime/date→isoformat`、dict/list 递归）。ECharts 类
    （bar/line/pie）渲染器已归一成 float，`_jsonSafe` 原样透传。

    不可变：有归一/截断时才返回新 dict；无变化时原样返回入参（调用方手里那份
    还要发给前端，不能被就地改）。
    """
    return _jsonSafe(_boundedRows(chartOption))


def boundedTableOption(tableOption: dict | None) -> dict | None:
    """把表负载（tableOption）变成可落库形态：JSON 安全归一 + rows 截到上界。

    **JSON 安全归一**：tableOption 的 rows 是 SQL 原始数据 —— PG/MySQL 的 NUMERIC
    列经 SQLAlchemy 返回 `Decimal`、DATE/TIMESTAMP 返回 `datetime`/`date`，而
    SQLAlchemy JSONB 序列化器是裸 ``json.dumps``（无 default），遇 `Decimal` 直接
    抛 ``TypeError: Object of type Decimal is not JSON serializable``。这里把
    `Decimal → float`（与图表渲染器 `toNumber` 同口径）、`datetime/date → isoformat`
    、dict/list 递归归一，str/int/float/bool/None 原样 —— TABLE 负载存的是原始
    data（`Decimal`/`datetime`），ECharts 类渲染器已走 `toNumber` 归一成 float；
    两路在此合流，已安全的值原样透传。

    **上界**：rows 截到 `_PERSIST_MAX_TABLE_ROWS`（200），超限置 truncated=True。
    平时 `assembleTableOption` 已按 FULL_DATA_THRESHOLD（默认 100）截过一轮并置
    truncated，100 ≤ 200 ⇒ 这道闸只在运维把阈值调到 >200 时才生效 —— 是纵深兜底。

    不可变：返回新 dict，绝不原地改调用方手里那份（它还要发给前端）。
    """
    if not isinstance(tableOption, dict):
        return tableOption
    # 先截后归一：只对保留的那 200 行做 Decimal→float，避免超限时先归一全量再丢弃。
    return _jsonSafe(_boundedRows(tableOption))


def _boundedRows(payload: dict | None) -> dict | None:
    """把 ``{..., "rows": [...]}`` 形状的 rows 截到上界，超限时返回新 dict 并置 truncated。

    不可变：截断时返回新 dict（不改调用方手里那份，它还要发给前端）。
    只在 `rows` 确实是 list 且超限时动手，不做类型猜测 —— 负载形状由服务端
    渲染器/装配层保证，这里只做体量兜底。
    """
    if not isinstance(payload, dict):
        return payload
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) <= _PERSIST_MAX_TABLE_ROWS:
        return payload
    return {
        **payload,
        "rows": rows[:_PERSIST_MAX_TABLE_ROWS],
        "truncated": True,
    }


def _jsonSafe(value: Any) -> Any:
    """把 SQL 原始值递归归一为 JSON 可序列化值（供 JSONB 落库）。

    不可变：容器只在确有子值发生变化时才重建（无变化时原样返回入参，保持对象
    同一性，让调用方能把「无归一」与「是同一份对象」划等号）；绝不原地改。标量
    （str/int/float/bool/None）原样返回；`Decimal→float`、`datetime/date→isoformat`；
    tuple 恒产出 list（类型本身就是变化）；未知类型落 `str(value)`（宁可留个可读
    痕迹，也不静默丢值）。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        changed = False
        result: dict = {}
        for key, item in value.items():
            normalized = _jsonSafe(item)
            result[key] = normalized
            if normalized is not item:
                changed = True
        return result if changed else value
    if isinstance(value, (list, tuple)):
        normalized = [_jsonSafe(item) for item in value]
        changed = isinstance(value, tuple) or any(
            newItem is not oldItem for newItem, oldItem in zip(normalized, value)
        )
        return normalized if changed else value
    return str(value)
