"""visual_payload.assembleTableOption 的纯函数单测。

钉住「图 + 表」双负载里「表」这一半的形状：
- 图形类 kind → {columns, rows, truncated}
- TABLE / KPI → None（表已经在 chartOption 里 / 指标卡不附表）
- 阈值切片、truncated 标志、不可变性
"""

from __future__ import annotations

from app.domain.enums import ChartType
from app.services.visual_payload import assembleTableOption


def _rows(n: int) -> list[dict]:
    return [{"SUPPLIER_NAME": f"B{i}", "RCV_QTY_PUU": i} for i in range(n)]


class TestAssembleTableOption:
    def test_chart_kind_returns_a_table_payload(self) -> None:
        data = _rows(3)
        result = assembleTableOption(
            specKind=ChartType.BAR,
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=data,
            fullDataThreshold=100,
        )

        assert result is not None
        assert result["columns"] == ["SUPPLIER_NAME", "RCV_QTY_PUU"]
        assert result["rows"] == data
        assert result["truncated"] is False

    def test_table_kind_returns_none(self) -> None:
        assert (
            assembleTableOption(
                specKind=ChartType.TABLE,
                columns=["A"],
                data=[{"A": 1}],
                fullDataThreshold=100,
            )
            is None
        )

    def test_kpi_kind_returns_none(self) -> None:
        assert (
            assembleTableOption(
                specKind=ChartType.KPI,
                columns=["A"],
                data=[{"A": 1}],
                fullDataThreshold=100,
            )
            is None
        )

    def test_over_threshold_truncates_and_flags(self) -> None:
        data = _rows(5)
        result = assembleTableOption(
            specKind=ChartType.HBAR,
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=data,
            fullDataThreshold=3,
        )

        assert result is not None
        assert result["truncated"] is True
        assert len(result["rows"]) == 3
        assert result["rows"] == data[:3]

    def test_exactly_at_threshold_is_not_truncated(self) -> None:
        data = _rows(3)
        result = assembleTableOption(
            specKind=ChartType.HBAR,
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=data,
            fullDataThreshold=3,
        )

        assert result is not None
        assert result["truncated"] is False
        assert len(result["rows"]) == 3

    def test_empty_data_still_returns_a_payload(self) -> None:
        result = assembleTableOption(
            specKind=ChartType.BAR,
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=[],
            fullDataThreshold=100,
        )

        assert result == {
            "columns": ["SUPPLIER_NAME", "RCV_QTY_PUU"],
            "rows": [],
            "truncated": False,
        }


class TestImmutability:
    def test_does_not_mutate_the_caller_data(self) -> None:
        data = _rows(5)
        columns = ["SUPPLIER_NAME", "RCV_QTY_PUU"]
        result = assembleTableOption(
            specKind=ChartType.BAR,
            columns=columns,
            data=data,
            fullDataThreshold=2,
        )

        # 调用方手里的 data 原地不动（5 行、无切片）。
        assert len(data) == 5
        # 返回的是新对象，不是调用方那份列表的别名。
        assert result is not None
        assert result["rows"] is not data
        assert result["columns"] is not columns
