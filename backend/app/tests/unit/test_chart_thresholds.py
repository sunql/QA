"""图表决策引擎的 4 个受治理阈值：读取函数 + 汇总装载。

按 ``Harness/rules/魔数治理.md`` §4：**每个读取函数必须有 3 例** ——
key 缺席 / 格式错 / 正常。另加非正例（闸门静默失效）与「DB 挂了不阻断主链路」。

为什么不复用 chat_recall 上的 `_getClassFilterMaxClasses` 方法群：那一组是
`ChatService` 的方法，读的是召回口径的键；图表阈值属图表子系统，就近放在
`chart_thresholds` 模块里，调用方（`chart_service` 门面）显式装载一次后把
不可变 dataclass 往下传，决策引擎本身**不碰 session**（纯函数可测）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.services.chart_thresholds import (
    _CHART_HBAR_MIN_ROWS_DEFAULT,
    _CHART_HEATMAP_MIN_COVERAGE_DEFAULT,
    _CHART_PIE_MAX_ROWS_DEFAULT,
    _CHART_TOP_N_MAX_DEFAULT,
    ChartThresholds,
    _getChartHbarMinRows,
    _getChartHeatmapMinCoverage,
    _getChartPieMaxRows,
    _getChartTopNMax,
    loadChartThresholds,
)

_KEYS = {
    "CHART_PIE_MAX_ROWS": "CHART_PIE_MAX_ROWS",
    "CHART_HBAR_MIN_ROWS": "CHART_HBAR_MIN_ROWS",
    "CHART_HEATMAP_MIN_COVERAGE": "CHART_HEATMAP_MIN_COVERAGE",
    "CHART_TOP_N_MAX": "CHART_TOP_N_MAX",
}


class _FakeResult:
    def __init__(self, raw: object) -> None:
        self._raw = raw

    def scalar_one_or_none(self) -> object:
        return self._raw


class _FakeSession:
    """按 SQL 文本里的 key 分派返回值；未登记的 key 视为缺席（None）。"""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = values or {}
        self.queries: list[str] = []

    async def execute(self, stmt: object) -> _FakeResult:
        sql = str(stmt)
        self.queries.append(sql)
        for key, raw in self.values.items():
            if key in sql:
                return _FakeResult(raw)
        return _FakeResult(None)


class _BoomSession:
    async def execute(self, stmt: object) -> _FakeResult:
        raise RuntimeError("UndefinedTableError: system_config")


class _RowReturning:
    """无论 SQL 是什么都返回同一条 raw —— 用于测「值本身非法」。"""

    def __init__(self, raw: object) -> None:
        self._raw = raw

    async def execute(self, stmt: object) -> _FakeResult:
        return _FakeResult(self._raw)


class TestGetChartPieMaxRows:
    @pytest.mark.asyncio
    async def test_missing_key_returns_default(self) -> None:
        assert await _getChartPieMaxRows(_FakeSession()) == _CHART_PIE_MAX_ROWS_DEFAULT

    @pytest.mark.asyncio
    async def test_malformed_value_returns_default(self) -> None:
        for bad in ("not-an-int", "6.5", "  ", ""):
            got = await _getChartPieMaxRows(_RowReturning(bad))
            assert got == _CHART_PIE_MAX_ROWS_DEFAULT, f"raw={bad!r}"

    @pytest.mark.asyncio
    async def test_present_value_wins(self) -> None:
        session = _FakeSession({"CHART_PIE_MAX_ROWS": "9"})
        assert await _getChartPieMaxRows(session) == 9

    @pytest.mark.asyncio
    async def test_non_positive_falls_back(self) -> None:
        """0/负不是「切得更狠」而是闸门消失：0 会让环形图永不可达。"""
        for bad in ("0", "-3"):
            got = await _getChartPieMaxRows(_RowReturning(bad))
            assert got == _CHART_PIE_MAX_ROWS_DEFAULT, f"raw={bad!r}"

    @pytest.mark.asyncio
    async def test_db_error_does_not_break_pipeline(self) -> None:
        assert await _getChartPieMaxRows(_BoomSession()) == _CHART_PIE_MAX_ROWS_DEFAULT


class TestGetChartHbarMinRows:
    @pytest.mark.asyncio
    async def test_missing_key_returns_default(self) -> None:
        assert await _getChartHbarMinRows(_FakeSession()) == _CHART_HBAR_MIN_ROWS_DEFAULT

    @pytest.mark.asyncio
    async def test_malformed_value_returns_default(self) -> None:
        got = await _getChartHbarMinRows(_RowReturning("abc"))
        assert got == _CHART_HBAR_MIN_ROWS_DEFAULT

    @pytest.mark.asyncio
    async def test_present_value_wins(self) -> None:
        assert await _getChartHbarMinRows(_FakeSession({"CHART_HBAR_MIN_ROWS": "25"})) == 25

    @pytest.mark.asyncio
    async def test_non_positive_falls_back(self) -> None:
        assert await _getChartHbarMinRows(_RowReturning("0")) == _CHART_HBAR_MIN_ROWS_DEFAULT

    @pytest.mark.asyncio
    async def test_db_error_does_not_break_pipeline(self) -> None:
        assert await _getChartHbarMinRows(_BoomSession()) == _CHART_HBAR_MIN_ROWS_DEFAULT


class TestGetChartTopNMax:
    @pytest.mark.asyncio
    async def test_missing_key_returns_default(self) -> None:
        assert await _getChartTopNMax(_FakeSession()) == _CHART_TOP_N_MAX_DEFAULT

    @pytest.mark.asyncio
    async def test_malformed_value_returns_default(self) -> None:
        assert await _getChartTopNMax(_RowReturning("N/A")) == _CHART_TOP_N_MAX_DEFAULT

    @pytest.mark.asyncio
    async def test_present_value_wins(self) -> None:
        assert await _getChartTopNMax(_FakeSession({"CHART_TOP_N_MAX": "50"})) == 50

    @pytest.mark.asyncio
    async def test_non_positive_falls_back(self) -> None:
        assert await _getChartTopNMax(_RowReturning("-1")) == _CHART_TOP_N_MAX_DEFAULT

    @pytest.mark.asyncio
    async def test_db_error_does_not_break_pipeline(self) -> None:
        assert await _getChartTopNMax(_BoomSession()) == _CHART_TOP_N_MAX_DEFAULT


class TestGetChartHeatmapMinCoverage:
    @pytest.mark.asyncio
    async def test_missing_key_returns_default(self) -> None:
        got = await _getChartHeatmapMinCoverage(_FakeSession())
        assert got == pytest.approx(_CHART_HEATMAP_MIN_COVERAGE_DEFAULT)

    @pytest.mark.asyncio
    async def test_malformed_value_returns_default(self) -> None:
        for bad in ("high", "0.6x", ""):
            got = await _getChartHeatmapMinCoverage(_RowReturning(bad))
            assert got == pytest.approx(_CHART_HEATMAP_MIN_COVERAGE_DEFAULT), f"raw={bad!r}"

    @pytest.mark.asyncio
    async def test_present_value_wins(self) -> None:
        session = _FakeSession({"CHART_HEATMAP_MIN_COVERAGE": "0.35"})
        assert await _getChartHeatmapMinCoverage(session) == pytest.approx(0.35)

    @pytest.mark.asyncio
    async def test_non_positive_falls_back(self) -> None:
        """0 会让任何矩阵都「够稠密」→ 热力图门槛静默消失。"""
        for bad in ("0", "-0.2"):
            got = await _getChartHeatmapMinCoverage(_RowReturning(bad))
            assert got == pytest.approx(_CHART_HEATMAP_MIN_COVERAGE_DEFAULT), f"raw={bad!r}"

    @pytest.mark.asyncio
    async def test_above_one_falls_back(self) -> None:
        """完备度是比值，>1 不可能达成 → 热力图被永久禁用，同样视同非法。"""
        got = await _getChartHeatmapMinCoverage(_RowReturning("1.5"))
        assert got == pytest.approx(_CHART_HEATMAP_MIN_COVERAGE_DEFAULT)

    @pytest.mark.asyncio
    async def test_exactly_one_is_accepted(self) -> None:
        """1.0 是合法边界（要求完全稠密的矩阵），不能误判成非法。"""
        assert await _getChartHeatmapMinCoverage(_RowReturning("1.0")) == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_db_error_does_not_break_pipeline(self) -> None:
        got = await _getChartHeatmapMinCoverage(_BoomSession())
        assert got == pytest.approx(_CHART_HEATMAP_MIN_COVERAGE_DEFAULT)


class TestLoadChartThresholds:
    @pytest.mark.asyncio
    async def test_loads_all_four_keys_in_one_pass(self) -> None:
        session = _FakeSession(
            {
                "CHART_PIE_MAX_ROWS": "8",
                "CHART_HBAR_MIN_ROWS": "20",
                "CHART_HEATMAP_MIN_COVERAGE": "0.7",
                "CHART_TOP_N_MAX": "30",
            }
        )

        thresholds = await loadChartThresholds(session)

        assert thresholds == ChartThresholds(
            pieMaxRows=8, hbarMinRows=20, heatmapMinCoverage=0.7, topNMax=30
        )

    @pytest.mark.asyncio
    async def test_empty_config_yields_all_defaults(self) -> None:
        thresholds = await loadChartThresholds(_FakeSession())

        assert thresholds.pieMaxRows == _CHART_PIE_MAX_ROWS_DEFAULT
        assert thresholds.hbarMinRows == _CHART_HBAR_MIN_ROWS_DEFAULT
        assert thresholds.heatmapMinCoverage == pytest.approx(
            _CHART_HEATMAP_MIN_COVERAGE_DEFAULT
        )
        assert thresholds.topNMax == _CHART_TOP_N_MAX_DEFAULT

    @pytest.mark.asyncio
    async def test_db_error_yields_all_defaults(self) -> None:
        """一次装载四读，全挂也要返回可用对象 —— 出图不该因为配置表抖动而消失。"""
        thresholds = await loadChartThresholds(_BoomSession())

        assert thresholds.pieMaxRows == _CHART_PIE_MAX_ROWS_DEFAULT

    @pytest.mark.asyncio
    async def test_thresholds_are_frozen(self) -> None:
        thresholds = await loadChartThresholds(_FakeSession())

        with pytest.raises(FrozenInstanceError):
            thresholds.pieMaxRows = 1  # type: ignore[misc]
