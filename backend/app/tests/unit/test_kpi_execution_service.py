"""Tier A 只读执行器：Guard / 超时 / 行数 / 归一化 / 证据 best-effort。"""

from __future__ import annotations

import pytest

from app.services.kpi_execution_service import (
    KpiExecutionService,
    firstPresentValue,
)


class _StubAdapter:
    def __init__(self, rows=None, exc=None) -> None:
        self.rows = rows or []
        self.exc = exc
        self.calls = []

    async def execute_read_only(self, sql, *, maxRows=None):
        self.calls.append((sql, maxRows))
        if self.exc is not None:
            raise self.exc
        return self.rows


async def test_happy_path_returns_value_and_metadata() -> None:
    adapter = _StubAdapter(rows=[{"rate": 0.93}, {"rate": 0.5}])
    svc = KpiExecutionService(adapterProvider=lambda _id, _ds: adapter)
    result = await svc.execute(
        formula="SELECT 0.93 AS rate", datasourceId=7, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=0.9,
    )
    assert result.ok is True
    assert result.value == 0.93
    assert result.rowCount == 2
    assert result.reason is None
    assert adapter.calls == [("SELECT 0.93 AS rate", 1001)]  # rowLimit + 1 用于精确判截断


async def test_empty_rows_degrades() -> None:
    svc = KpiExecutionService(adapterProvider=lambda _i, _d: _StubAdapter(rows=[]))
    result = await svc.execute(
        formula="SELECT 1", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is False
    assert result.reason == "empty_result"
    assert result.rows == ()


async def test_all_none_value_degrades() -> None:
    svc = KpiExecutionService(
        adapterProvider=lambda _i, _d: _StubAdapter(rows=[{"v": None}])
    )
    result = await svc.execute(
        formula="SELECT NULL AS v", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is False and result.reason == "empty_result"


async def test_adapter_timeout_degrades() -> None:
    svc = KpiExecutionService(
        adapterProvider=lambda _i, _d: _StubAdapter(exc=TimeoutError("t"))
    )
    result = await svc.execute(
        formula="SELECT 1", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is False and result.reason == "execution_timeout"


async def test_adapter_error_degrades_without_raising() -> None:
    svc = KpiExecutionService(
        adapterProvider=lambda _i, _d: _StubAdapter(exc=RuntimeError("boom"))
    )
    result = await svc.execute(
        formula="SELECT 1", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is False and result.reason == "execution_error"


async def test_non_readonly_formula_is_rejected_before_adapter() -> None:
    adapter = _StubAdapter(rows=[{"x": 1}])
    svc = KpiExecutionService(adapterProvider=lambda _i, _d: adapter)
    result = await svc.execute(
        formula="DELETE FROM t", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is False and result.reason == "formula_not_read_only"
    assert adapter.calls == []  # 绝不允许落到 adapter


def test_first_present_value_skips_none() -> None:
    assert firstPresentValue([{"a": None, "b": 2}]) == 2
    assert firstPresentValue([]) is None


def test_first_present_value_uses_first_row_only() -> None:
    """与 chat_service._firstPresentValue 同口径：只看首行，不跨行捞值。"""
    assert firstPresentValue([{"a": None}, {"a": 2}]) is None


async def test_row_limit_marks_truncated() -> None:
    """多取 1 行的精确语义：1001 ⇒ truncated；恰好 1000 ⇒ 不算截断。"""
    over = _StubAdapter(rows=[{"v": i} for i in range(1001)])
    svc = KpiExecutionService(adapterProvider=lambda _i, _d: over)
    result = await svc.execute(
        formula="SELECT v FROM t", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.truncated is True
    assert len(result.rows) == 1000
    assert over.calls == [("SELECT v FROM t", 1001)]

    exact = _StubAdapter(rows=[{"v": i} for i in range(1000)])
    svc2 = KpiExecutionService(adapterProvider=lambda _i, _d: exact)
    result2 = await svc2.execute(
        formula="SELECT v FROM t", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result2.truncated is False
    assert len(result2.rows) == 1000


async def test_evidence_scheduler_failure_does_not_break() -> None:
    def boom(**_kwargs) -> None:
        raise RuntimeError("evidence down")

    svc = KpiExecutionService(
        adapterProvider=lambda _i, _d: _StubAdapter(rows=[{"v": 1}]),
        evidenceScheduler=boom,
    )
    result = await svc.execute(
        formula="SELECT 1 AS v", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is True
    assert result.value == 1


@pytest.mark.parametrize(
    ("formula", "reason"),
    [
        ("DELETE FROM t", "formula_not_read_only"),
        ("INSERT INTO t VALUES (1)", "formula_not_read_only"),
        ("", "formula_not_read_only"),
    ],
)
async def test_non_readonly_matrix(formula: str, reason: str) -> None:
    adapter = _StubAdapter(rows=[{"x": 1}])
    svc = KpiExecutionService(adapterProvider=lambda _i, _d: adapter)
    result = await svc.execute(
        formula=formula, datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is False and result.reason == reason
    assert adapter.calls == []
