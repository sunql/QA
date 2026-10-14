"""可视化输出策略的「表」负载装配（纯函数）。

**它是什么**：把 SQL 结果集裁成一张前端能直接渲染的数据表负载（`{"columns",
"rows", "truncated"}`）。它与 `chart_option` 是同一份 `data` 的**两个投影**：
图（ECharts option）给「看趋势/占比」，表（本模块）给「查明细/抄数字」。

**为什么图也要带表**（可视化输出策略，决策 8）：图把数据压缩成形状，用户看不到
原始数字；表把行原样交出去，两者互补。但表会占用传输体积，所以按
`fullDataThreshold` 截断：超过阈值只带前 N 行 + `truncated=True`，前端据此给
「完整数据见导出」的提示。

**为什么 TABLE/KPI 返回 None**：
- TABLE 的表已经在 `chartOption` 里（`{"columns", "rows"}`），再发一份是重复载荷；
- KPI 是单值卡，没有可附的明细表。

**约束**：
- 零 LLM、零 IO —— 纯函数，不 import 任何 DB / HTTP 模块（阈值由调用方现读传入）。
- 不可变：返回**新 dict**，`data` 只读不写（切片产新列表，绝不原地改）。
- 不抛异常。
"""

from __future__ import annotations

from typing import Any

from app.domain.enums import ChartType


def assembleTableOption(
    *,
    specKind: ChartType,
    columns: list[str],
    data: list[dict],
    fullDataThreshold: int,
) -> dict | None:
    """把结果集裁成数据表负载；TABLE / KPI 返回 None（表已在 chartOption / 不附表）。

    `specKind` 必须是 `coerceSpec` **之后**的最终 `spec.kind`（即发出去的
    `chartType`）：降级成 TABLE 时这里必须返 None，否则前端同一份数据会拿到两份表。
    """
    if specKind is ChartType.TABLE or specKind is ChartType.KPI:
        return None
    rows = data[:fullDataThreshold]
    return {
        "columns": list(columns),
        "rows": rows,
        "truncated": len(data) > fullDataThreshold,
    }
