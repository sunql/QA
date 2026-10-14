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
    boundedTableOption,
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

    def test_decimal_rows_become_json_safe(self) -> None:
        """TABLE 负载存的是原始行：NUMERIC 列是 Decimal，裸 json.dumps 会炸，必须归一。"""
        import json
        from decimal import Decimal

        option = {
            "columns": ["NAME", "QTY"],
            "rows": [{"NAME": "A", "QTY": Decimal("10")}],
        }

        bounded = boundedChartOption(option)

        assert bounded["rows"][0]["QTY"] == 10.0
        assert isinstance(bounded["rows"][0]["QTY"], float)
        json.dumps(bounded)  # 不抛 TypeError 即通过

    def test_datetime_and_date_become_isoformat(self) -> None:
        """DATE/TIMESTAMP 列经 SQLAlchemy 返回 date/datetime，须归一为 ISO 字符串。"""
        import json
        from datetime import date, datetime

        option = {
            "columns": ["NAME", "AT"],
            "rows": [{"NAME": "A", "AT": datetime(2026, 10, 1, 12, 0, 0)}],
            "meta": {"day": date(2026, 10, 1)},
        }

        bounded = boundedChartOption(option)

        assert bounded["rows"][0]["AT"] == "2026-10-01T12:00:00"
        assert bounded["meta"]["day"] == "2026-10-01"
        json.dumps(bounded)

    def test_unknown_type_stringified_not_dropped(self) -> None:
        """兜底：未知类型落 str(value)，留可读痕迹，不静默丢值。"""

        class _Unknown:
            def __str__(self) -> str:
                return "unknown-marker"

        bounded = boundedChartOption({"v": _Unknown()})

        assert bounded["v"] == "unknown-marker"

    def test_nested_decimal_is_normalized(self) -> None:
        """归一递归进嵌套容器，只测顶层会漏掉递归分支。"""
        import json
        from decimal import Decimal

        option = {"rows": [{"deep": {"v": Decimal("1.5")}}]}

        bounded = boundedChartOption(option)

        assert bounded["rows"][0]["deep"]["v"] == 1.5
        json.dumps(bounded)

    def test_non_finite_floats_become_serializable_strings(self) -> None:
        """非有限浮点（NaN/±Infinity）必须归一，不能原样透传。

        PG 的 jsonb 拒绝裸 NaN/Infinity（`select '{"v": NaN}'::jsonb` 报
        invalid input syntax）。`json.dumps` 默认却不抛（输出裸 NaN），所以用
        `allow_nan=False` 对齐 PG 的拒绝口径 —— 这里不抛即证明已无非有限浮点。
        """
        import json

        bounded = boundedChartOption(
            {"v": float("nan"), "w": float("inf"), "x": float("-inf")}
        )

        assert bounded["v"] == "nan"
        assert bounded["w"] == "inf"
        assert bounded["x"] == "-inf"
        json.dumps(bounded, allow_nan=False)  # PG 同口径：非有限浮点必须已归一


class TestBoundedTableOption:
    def test_truncates_rows_over_the_cap_and_marks_it(self) -> None:
        """表负载落库前同样要过 200 行上界（与图负载共用同一道闸）。

        平时 `assembleTableOption` 已按 FULL_DATA_THRESHOLD（默认 100）截过一轮，
        100 ≤ 200 ⇒ 这道闸只在运维把阈值调到 >200 时才生效 —— 是纵深兜底，不是死代码。
        """
        option = {
            "columns": ["NAME", "QTY"],
            "rows": [{"NAME": f"n{i}", "QTY": i} for i in range(500)],
        }

        bounded = boundedTableOption(option)

        assert len(bounded["rows"]) == _PERSIST_MAX_TABLE_ROWS
        assert bounded["truncated"] is True

    def test_does_not_mutate_the_caller_copy(self) -> None:
        """调用方手里那份还要发给前端（实时响应发全量），不能被就地截断。"""
        option = {
            "columns": ["NAME", "QTY"],
            "rows": [{"NAME": f"n{i}", "QTY": i} for i in range(500)],
        }

        boundedTableOption(option)

        assert len(option["rows"]) == 500
        assert "truncated" not in option

    def test_rows_at_the_cap_are_untouched(self) -> None:
        """边界：正好等于上限不算超限，不加 truncated 标记（避免谎报截断）。"""
        option = {
            "columns": ["NAME"],
            "rows": [{"NAME": i} for i in range(_PERSIST_MAX_TABLE_ROWS)],
        }

        assert boundedTableOption(option) is option
        assert "truncated" not in boundedTableOption(option)

    def test_decimal_rows_become_json_safe(self) -> None:
        """rows 是 SQL 原始数据（NUMERIC → Decimal），JSONB 裸序列化会炸，必须归一。"""
        from decimal import Decimal

        option = {
            "columns": ["NAME", "QTY"],
            "rows": [{"NAME": "A", "QTY": Decimal("10")}],
        }

        bounded = boundedTableOption(option)

        assert bounded["rows"][0]["QTY"] == 10.0
        assert isinstance(bounded["rows"][0]["QTY"], float)

    def test_malformed_payload_does_not_raise(self) -> None:
        """表负载形状由装配层保证，这里只兜体量 —— 不做类型猜测、不能炸。"""
        assert boundedTableOption(None) is None
        assert boundedTableOption({"rows": "not a list"}) == {"rows": "not a list"}

    def test_decimal_nan_becomes_serializable(self) -> None:
        """`float(Decimal("NaN"))` 也产出 nan —— 与 Task 10 的 Decimal 崩溃同源。

        NUMERIC 列里出现 NaN/Infinity 时（触发条件比 Decimal 更窄），两条路径
        （boundedChartOption / boundedTableOption）都经 `_jsonSafe` 的
        `Decimal→float` 中招，必须归一成可序列化形态。
        """
        import json
        from decimal import Decimal

        bounded = boundedTableOption({"rows": [{"qty": Decimal("NaN")}]})

        assert bounded["rows"][0]["qty"] == "nan"
        json.dumps(bounded, allow_nan=False)
