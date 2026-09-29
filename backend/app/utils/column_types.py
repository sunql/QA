"""列类型推断 SSOT（被 chart_service 与 data_summary 复用）。

本模块是「SQL 结果数据列的类型推断」单一事实源。chart_service 原本持有
_isNumber / _looksLikeDatetime / _toNumber / _inferColumnType / _DATETIME_RE /
_COLUMN_TYPE_* 一组私有 helper，2026-09-18 feat-smart-data-summary 抽出到本
模块，让 chat_service 的数据摘要、chart_service 的图表推荐共用同一套判定。

设计约束：
- 无副作用纯函数（除 re.compile 模块级常量）。
- 判定顺序：NUMBER > TIME > STRING；None 永远跳过。
- 不可变性：不修改入参 list。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

# 列类型常量（chart_service 复用）
COLUMN_TYPE_STRING = "STRING"
COLUMN_TYPE_TIME = "TIME"
COLUMN_TYPE_NUMBER = "NUMBER"

# 时间列匹配：2026-08-01 / 2026/08/01 / 2026-08-01T10:00:00 等以日期开头
_DATETIME_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")


def is_number(value: Any) -> bool:
    """判断值是否为数值（int/float/Decimal，bool 视为非数值）。"""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def looks_like_datetime(value: Any) -> bool:
    """判断值是否非数值。"""
    if isinstance(value, (datetime, date)):
        return True
    if isinstance(value, str) and _DATETIME_RE.match(value):
        return True
    return False


def to_json_number(value: Any) -> Any:
    """将值转为 JSON 安全的数值表示（Decimal -> str；int/float 原样；其余 str）。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return str(value)


def infer_column_type(values: list[Any]) -> str:
    """推断单列类型：NUMBER > TIME > STRING（None 跳过，全 None 视为 STRING）。"""
    sample = [v for v in values if v is not None]
    if not sample:
        return COLUMN_TYPE_STRING
    if any(is_number(v) for v in sample):
        return COLUMN_TYPE_NUMBER
    if all(looks_like_datetime(v) for v in sample):
        return COLUMN_TYPE_TIME
    return COLUMN_TYPE_STRING


def infer_column_types(columns: list[str], data: list[dict]) -> dict[str, str]:
    """对多列批量推断类型，返回 {column: type} 字典。data 为空时所有列 STRING。"""
    return {col: infer_column_type([row.get(col) for row in data]) for col in columns}