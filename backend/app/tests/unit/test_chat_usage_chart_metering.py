"""`_chartStep` 的记账失败边界：图表照出，已知 token 数照带。

背景：多步路径（`chat_multistep._stepChart`）的兜底 except 会把 token 记成
`0, 0, 0`。若异常发生在**记账之后**（分类器已经真的消耗了 token），那个 0 就是
少报：整轮用量少了这一步的量，且台账里没有对应行。所以记账这一步自己兜住 ——
失败要吵（error 日志），但不能把图表、也不能把已知的 token 数一起丢掉。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.domain.enums import ChartType
from app.services.chat_usage import UsageMixin as ChatUsageMixin


@dataclass(frozen=True)
class _Build:
    chartType: ChartType = ChartType.BAR
    option: dict = None  # type: ignore[assignment]
    promptTokens: int = 120
    completionTokens: int = 30
    cachedTokens: int = 64

    def __post_init__(self) -> None:
        object.__setattr__(self, "option", {"series": [{"type": "bar"}]})


class _FakeChartService:
    """只实现 `_chartStep` 用到的那一个方法：buildChart。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def buildChart(self, **kwargs) -> _Build:
        self.calls.append(kwargs)
        return _Build()


class _Dto:
    chartType = None
    question = "各供应商的收货量"
    sessionId = "s-chart-usage"


class _Context:
    client = None
    selected = None


class _Service(ChatUsageMixin):
    def __init__(self, *, recordFails: bool = False) -> None:
        self._chart = _FakeChartService()
        self.recordFails = recordFails
        self.recorded: list[tuple[int, int, int | None]] = []

    def _columns(self, data: list[dict]) -> list[str]:
        return list(data[0].keys()) if data else []

    async def _recordChartUsage(
        self, session, dto, config, chartPt: int, chartCt: int, chartCached: int | None = None,
    ) -> None:
        if self.recordFails:
            raise RuntimeError("usage ledger is down")
        self.recorded.append((chartPt, chartCt, chartCached))


_ROWS = [{"SUPPLIER_NAME": "B125", "RCV_QTY_PUU": 9812}]


async def test_chart_step_records_usage_and_returns_counts() -> None:
    service = _Service()

    chartType, option, promptTokens, completionTokens, cachedTokens = await service._chartStep(
        None, _Dto(), _Context(), _ROWS,
    )

    assert chartType is ChartType.BAR
    assert option == {"series": [{"type": "bar"}]}
    assert (promptTokens, completionTokens, cachedTokens) == (120, 30, 64)
    assert service.recorded == [(120, 30, 64)]


async def test_chart_step_keeps_chart_and_counts_when_metering_fails(caplog) -> None:
    service = _Service(recordFails=True)

    with caplog.at_level("ERROR"):
        chartType, option, promptTokens, completionTokens, cachedTokens = await service._chartStep(
            None, _Dto(), _Context(), _ROWS,
        )

    # 图表与已知用量都保住：不会因为记账失败而把这一步报成 0
    assert chartType is ChartType.BAR
    assert option == {"series": [{"type": "bar"}]}
    assert (promptTokens, completionTokens, cachedTokens) == (120, 30, 64)
    # 账没记上必须留下痕迹（合规硬约束：每次 LLM 调用都要计量）
    assert any("记账失败" in record.message for record in caplog.records)
