"""JSONB 落库前的值归一 SSOT（多步落库与上下文压缩共用）。

**为什么需要**：`multi_step_step` 的 `data` / `data_compressed` / `chart_option`
都是**裸 JSONB** 列（无 `default=str` 编码器）。而 SQL 结果里的 NUMERIC 列经
asyncpg 回来是 `Decimal`、时间列是 `datetime` —— 直接赋值会在 SQLAlchemy 编译
语句时（`json_serializer`）抛
`StatementError: (builtins.TypeError) Object of type Decimal is not JSON serializable`，
**整条 UPDATE 一起失败**，该步既不落 SQL 也不落 data（静默的落库缺口）。

**数值不得退化成字符串**：前端与报告按数值消费（排序、图表、汇总），落成
`"12.5"` 会让消费方拿到字符串。故 `Decimal → float`（JSON 里就是数值）；只有
非有限值（NaN/±Infinity）才落 `str` —— PostgreSQL 的 jsonb 拒绝裸
`NaN`/`Infinity`（`select '{"v": NaN}'::jsonb` → invalid input syntax for type
json），宁可留个可读痕迹也不静默丢值。

**不可变**：容器只在确有子值发生变化时才重建；无变化时原样返回入参，保持对象
同一性（调用方拿它发给前端的那份不受影响）。

与 `app.services.chat_chart_persist._jsonSafe` 的关系：两者归一规则一致，那是
图表负载（session_message 两列）的落库路径，属另一特性；本模块是多步落库路径的
SSOT，供 `multi_step_persistence` 与 `multi_step_compressor` 复用，避免同一套
Decimal/datetime 规则在多步链路里各写一份而漂移。
"""
from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def jsonSafe(value: Any) -> Any:
    """把 DB 原值递归归一为 JSON 可序列化值（供 JSONB 落库）。

    标量（str/int/bool/None）原样返回；有限 float 原样返回、非有限 float 落
    `str(value)`；`Decimal → float`（非有限同样落 `str`）；`datetime` / `date`
    落 `isoformat()`；tuple 恒产出 list（类型本身就是变化）；dict/list 递归，
    仅在有子值变化时重建；未知类型落 `str(value)`。
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        asFloat = float(value)
        return asFloat if math.isfinite(asFloat) else str(asFloat)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        changed = False
        result: dict = {}
        for key, item in value.items():
            normalized = jsonSafe(item)
            result[key] = normalized
            if normalized is not item:
                changed = True
        return result if changed else value
    if isinstance(value, (list, tuple)):
        normalizedList = [jsonSafe(item) for item in value]
        changed = isinstance(value, tuple) or any(
            newItem is not oldItem
            for newItem, oldItem in zip(normalizedList, value, strict=True)
        )
        return normalizedList if changed else value
    return str(value)
