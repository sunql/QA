"""Tier A 只读执行器：Guard / 超时 / 行数 / 归一化 / 证据 best-effort。"""

from __future__ import annotations

import logging

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


async def test_adapter_provider_failure_degrades_without_raising() -> None:
    """adapter 构造失败（如 SECRET_KEY 轮换后解密异常）也必须降级，不得上抛。"""

    def boom(_datasourceId, _datasource):
        raise RuntimeError("decrypt failed")

    svc = KpiExecutionService(adapterProvider=boom)
    result = await svc.execute(
        formula="SELECT 1", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert result.ok is False and result.reason == "execution_error"


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


# =============================================================================
# 执行器 → 证据 接线
# =============================================================================


async def test_success_schedules_metric_result_evidence() -> None:
    """成功时把执行结果按上游匹配参数原样交给证据调度器（不重新匹配、不改写）。"""
    calls: list[dict] = []
    svc = KpiExecutionService(
        adapterProvider=lambda _i, _d: _StubAdapter(rows=[{"rate": 0.93}]),
        evidenceScheduler=lambda **kwargs: calls.append(kwargs),
    )
    result = await svc.execute(
        formula="SELECT 0.93 AS rate", datasourceId=7, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=0.9,
    )
    assert result.ok is True
    assert len(calls) == 1
    assert calls[0] == {
        "metricCode": "KPI_X",
        "metricName": "x",
        "confidence": 0.9,
        "rows": ({"rate": 0.93},),
        "datasourceId": 7,
    }


async def test_failure_does_not_schedule_evidence() -> None:
    """空结果或执行异常都不产生 METRIC_RESULT 证据（只对成功执行留痕）。"""
    calls: list[dict] = []

    def record(**kwargs) -> None:
        calls.append(kwargs)

    empty = KpiExecutionService(
        adapterProvider=lambda _i, _d: _StubAdapter(rows=[]),
        evidenceScheduler=record,
    )
    emptyResult = await empty.execute(
        formula="SELECT 1", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert emptyResult.ok is False

    failed = KpiExecutionService(
        adapterProvider=lambda _i, _d: _StubAdapter(exc=RuntimeError("boom")),
        evidenceScheduler=record,
    )
    failedResult = await failed.execute(
        formula="SELECT 1", datasourceId=1, datasource=object(),
        metricCode="KPI_X", metricName="x", confidence=1.0,
    )
    assert failedResult.ok is False
    assert calls == []


async def test_log_line_contains_kpi_and_datasource(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 成功行走 logger.info；不抬高捕获阈值 caplog 看不到。
    caplog.set_level(logging.INFO, logger="app.services.kpi_execution_service")

    success = KpiExecutionService(
        adapterProvider=lambda _i, _d: _StubAdapter(rows=[{"v": 1}]),
        evidenceScheduler=lambda **_kwargs: None,
    )
    await success.execute(
        formula="SELECT 1 AS v", datasourceId=9, datasource=object(),
        metricCode="KPI_LOG", metricName="log", confidence=1.0,
    )
    successLines = [r.getMessage() for r in caplog.records if "TierA 执行成功" in r.getMessage()]
    assert len(successLines) == 1
    for fragment in ("kpi=KPI_LOG", "datasource_id=9", "rows=", "elapsed_ms="):
        assert fragment in successLines[0]

    caplog.clear()
    failure = KpiExecutionService(
        adapterProvider=lambda _i, _d: _StubAdapter(exc=RuntimeError("boom")),
        evidenceScheduler=lambda **_kwargs: None,
    )
    await failure.execute(
        formula="SELECT 1", datasourceId=9, datasource=object(),
        metricCode="KPI_LOG", metricName="log", confidence=1.0,
    )
    failureLines = [r.getMessage() for r in caplog.records if "TierA 执行失败" in r.getMessage()]
    assert len(failureLines) == 1
    assert "kpi=KPI_LOG" in failureLines[0]
    assert "datasource_id=9" in failureLines[0]
