"""KPI_METRIC_TIER_A_ENABLED 双闸：env 杀闸优先，system_config 行缺失/非法 ⇒ 关。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import kpi_metric_switches as switches


class _FakeSettings:
    def __init__(self, enabled: bool) -> None:
        self.kpiMetricTierAEnabled = enabled


class _FakeSavepoint:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeResult:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        if self._value is None:
            return None
        return SimpleNamespace(value=self._value)


class _FakeSession:
    """只实现 readKpiMetricSwitch 需要的 begin_nested / execute。"""

    def __init__(self, value: str | None) -> None:
        self._value = value

    def begin_nested(self) -> _FakeSavepoint:
        return _FakeSavepoint()

    async def execute(self, _stmt: object) -> _FakeResult:
        return _FakeResult(self._value)


@pytest.mark.asyncio()
async def test_env_kill_switch_wins(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings(False))
    assert await switches.isKpiMetricTierAEnabled(_FakeSession("true")) is False


@pytest.mark.asyncio()
async def test_row_missing_is_false(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings(True))
    assert await switches.isKpiMetricTierAEnabled(_FakeSession(None)) is False


@pytest.mark.asyncio()
async def test_row_true_is_true(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings(True))
    assert await switches.isKpiMetricTierAEnabled(_FakeSession("true")) is True


@pytest.mark.asyncio()
async def test_none_session_is_false(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings(True))
    assert await switches.isKpiMetricTierAEnabled(None) is False
