"""SQL 结果 → 结构化摘要：总行数 + 列类型 + 数值列统计 + 分类列 distinct + 头尾样本。

替代 chat_service._buildAnswerPrompt 与 step_aggregator._build_prompt 的
硬编码 data[:_DATA_SAMPLE_LIMIT]，让 answer LLM 拿到"全量统计 + 关键样本"，
prompt token 受控但能基于真实数据回答"共 X 行 / X 个供应商 / 数量范围 Y~Z"。

设计要点（2026-09-18 feat-smart-data-summary）：
- 纯函数：无 LLM、无 DB、无副作用，便于单测与跨模块复用
- 不可变：仅读 data，不修改；返回新 dict
- 数值统计只对 NUMBER 列，distinct 计数对 STRING/TIME 列；列数 cap 防 schema 列爆炸
- truncated 标志 = total > head_size + tail_size（小数据集 head/tail 可重叠）
- JSON 可序列化：min/max/avg/sum 统一为 float（Decimal 已转 float）；datetime/date
  在 samples 里保留原始对象，由调用方 default=str 处理

不做（明示）：
- 不做排序、过滤、采样——上游 SQL 已做；这里只是切片 + 统计
- 不生成自然语言摘要——LLM 自己读结构化字段
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from app.utils.column_types import (
    COLUMN_TYPE_NUMBER,
    COLUMN_TYPE_STRING,
    COLUMN_TYPE_TIME,
    infer_column_types,
)

# 默认采样与列 cap：保证单次 prompt 注入 token 受控。
DEFAULT_HEAD_SAMPLE_SIZE = 5
DEFAULT_TAIL_SAMPLE_SIZE = 5
DEFAULT_NUMERIC_COLUMNS_CAP = 5
DEFAULT_STRING_COLUMNS_CAP = 5

# v2 2026-09-18：小数据全量展示阈值。
# 行数 ≤ 此值时 samples.head 直接放全部 data，不截断、不进 tail、不打 truncated。
# 触发：用户报告 27 行时 head/tail 采样丢中间 → B125 看不见、图表 D1 不一致。
# 100 行 ≈ 5K tokens（按 200 字符/行），完全装得下；chart_service 也 import 此常量做同源判断。
FULL_DATA_THRESHOLD = 100


def _to_float(value: Any) -> float | None:
    """转 float 用于统计；None/NaN 跳过。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        f = float(value)
        if math.isnan(f):
            return None
        return f
    return None


def summarize_data(
    data: list[dict],
    *,
    head_size: int = DEFAULT_HEAD_SAMPLE_SIZE,
    tail_size: int = DEFAULT_TAIL_SAMPLE_SIZE,
    numeric_columns_cap: int = DEFAULT_NUMERIC_COLUMNS_CAP,
    string_columns_cap: int = DEFAULT_STRING_COLUMNS_CAP,
) -> dict[str, Any]:
    """构造 SQL 结果的结构化摘要。

    Args:
        data: SQL 查询结果行列表；空数据返回全空结构（total=0, truncated=False）
        head_size: 头部样本行数；<=0 视为 0
        tail_size: 尾部样本行数；<=0 视为 0
        numeric_columns_cap: NUMBER 列统计的列数上限；<=0 表示不限
        string_columns_cap: STRING/TIME 列 distinct 统计的列数上限；<=0 表示不限

    Returns:
        {
          "total": int,             # len(data)
          "truncated": bool,        # total > head_size + tail_size
          "columns": list[str],     # 按 data[0] 出现顺序
          "column_types": dict[str, str],  # 列名 -> "NUMBER"/"STRING"/"TIME"
          "numeric_stats": dict[str, {"min","max","avg","sum"}],  # NUMBER 列
          "distinct_counts": dict[str, int],  # STRING/TIME 列
          "samples": {"head": [...], "tail": [...]},  # 原始行 dict
        }
    """
    total = len(data)
    if not data:
        return {
            "total": 0,
            "truncated": False,
            "columns": [],
            "column_types": {},
            "numeric_stats": {},
            "distinct_counts": {},
            "samples": {"head": [], "tail": []},
        }

    head_n = max(0, head_size)
    tail_n = max(0, tail_size)
    # columns 按 data[0] 的 key 顺序；保留空字符串列（如 SELECT ''）也保留
    columns = list(data[0].keys())
    # cap=0 视为"不限"，回退为全部列数；正整数为上限
    num_cap = numeric_columns_cap if numeric_columns_cap > 0 else len(columns)
    str_cap = string_columns_cap if string_columns_cap > 0 else len(columns)

    column_types = infer_column_types(columns, data)

    # 数值列统计：取前 num_cap 个 NUMBER 列
    numeric_cols = [c for c in columns if column_types.get(c) == COLUMN_TYPE_NUMBER][:num_cap]
    numeric_stats: dict[str, dict[str, float]] = {}
    for col in numeric_cols:
        vals = [f for f in (_to_float(row.get(col)) for row in data) if f is not None]
        if not vals:
            continue
        numeric_stats[col] = {
            "min": min(vals),
            "max": max(vals),
            "avg": sum(vals) / len(vals),
            "sum": sum(vals),
        }

    # 分类列 distinct 计数：STRING/TIME 列
    categorical_cols = [
        c for c in columns
        if column_types.get(c) in (COLUMN_TYPE_STRING, COLUMN_TYPE_TIME)
    ][:str_cap]
    distinct_counts: dict[str, int] = {}
    for col in categorical_cols:
        distinct_counts[col] = len({row.get(col) for row in data})

    # v2 2026-09-18：数据量小时全量展示（≤ FULL_DATA_THRESHOLD 行），
    # 避免 head/tail 采样把中间行丢了（如 27 行时 B125 全在中间）。
    # 超过阈值才退回 head 5 + tail 5 + truncated=True。
    if total <= FULL_DATA_THRESHOLD:
        head_sample = list(data)
        tail_sample = []
        truncated = False
    else:
        head_sample = list(data[:head_n]) if head_n > 0 else []
        tail_sample = list(data[-tail_n:]) if tail_n > 0 else []
        truncated = True

    return {
        "total": total,
        "truncated": truncated,
        "columns": columns,
        "column_types": column_types,
        "numeric_stats": numeric_stats,
        "distinct_counts": distinct_counts,
        "samples": {"head": head_sample, "tail": tail_sample},
    }