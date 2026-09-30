"""图表负载落库归一（`chat_chart_persist`，0105）。

这两条规则此前没有任何测试：`_PERSIST_MAX_TABLE_ROWS` / `_chartTypeName` 在
`app/tests/` 里**零引用**。代价是「写了但没生效」不会被发现 —— 例如 `table` 的
十万行照旧灌进 `session_message`，或 `chart_type` 列里躺着 `"ChartType.BAR"`。

写入**链路**（真实 `/chat/stream` 之后 `session_message` 两列真的有值）由
`app/tests/integration/test_chat_stream_api.py` 走真 PG 覆盖；这里只钉纯函数。
"""

from __future__ import annotations

from app.domain.enums import ChartType
from app.services.chat_chart_persist import (
    _PERSIST_MAX_TABLE_ROWS,
    boundedChartOption,
    chartTypeName,
)


class TestChartTypeName:
    def test_enum_member_becomes_bare_string(self) -> None:
        """列是 VARCHAR(20) 且契约是「线上 chartType 的值」。

        pydantic 会**保留枚举成员**（`ChatResponse.chartType` 是 `ChartType | None`），
        不归一就会存进 `"ChartType.BAR"` —— 前端白名单认不出，渲染门直接不放行，
        而且不报任何错。
        """
        assert chartTypeName(ChartType.BAR) == "bar"
        assert chartTypeName(ChartType.DONUT) == "donut"

    def test_bare_string_passes_through(self) -> None:
        """流式路径早已 `.value` 过，二次归一无副作用。"""
        assert chartTypeName("hbar") == "hbar"

    def test_none_stays_none(self) -> None:
        """user 行 / 无图轮次：列留 NULL，不能写成 "None"。"""
        assert chartTypeName(None) is None

    def test_unexpected_object_is_stringified_not_dropped(self) -> None:
        """兜底：形状意外时留个可读的痕迹，而不是静默把图丢掉。"""
        assert chartTypeName(42) == "42"


class TestBoundedChartOption:
    def test_truncates_rows_over_the_cap_and_marks_it(self) -> None:
        """`QUERY_ROW_LIMIT` 默认 0 = 不限行，而表格 builder 返回**全量** data。"""
        option = {"columns": ["a"], "rows": [{"a": i} for i in range(500)]}

        bounded = boundedChartOption(option)

        assert len(bounded["rows"]) == _PERSIST_MAX_TABLE_ROWS
        assert bounded["truncated"] is True

    def test_does_not_mutate_the_caller_copy(self) -> None:
        """调用方手里那份还要发给前端（实时响应发全量），不能被就地截断。"""
        option = {"columns": ["a"], "rows": [{"a": i} for i in range(500)]}

        boundedChartOption(option)

        assert len(option["rows"]) == 500
        assert "truncated" not in option

    def test_keeps_the_leading_rows(self) -> None:
        """截尾不截头：前 200 行是查询真正排序出来的那部分。"""
        option = {"columns": ["a"], "rows": [{"a": i} for i in range(500)]}

        bounded = boundedChartOption(option)

        assert bounded["rows"][0] == {"a": 0}
        assert bounded["rows"][-1] == {"a": _PERSIST_MAX_TABLE_ROWS - 1}

    def test_rows_at_the_cap_are_untouched(self) -> None:
        """边界：正好等于上限不算超限，不加 truncated 标记（避免谎报截断）。"""
        option = {"columns": ["a"], "rows": [{"a": i} for i in range(_PERSIST_MAX_TABLE_ROWS)]}

        assert boundedChartOption(option) is option

    def test_non_table_payload_passes_through_unchanged(self) -> None:
        option = {"series": [{"type": "bar", "data": [1, 2]}]}

        assert boundedChartOption(option) is option

    def test_malformed_payload_does_not_raise(self) -> None:
        """负载形状由渲染器保证，这里只兜体量 —— 不做类型猜测、不能炸。"""
        assert boundedChartOption(None) is None
        assert boundedChartOption({"rows": "not a list"}) == {"rows": "not a list"}
