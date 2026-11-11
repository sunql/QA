"""KPI_METRIC_TIER_A_ENABLED 双闸：env 杀闸优先，system_config 行缺失/非法 ⇒ 关。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import kpi_metric_switches as switches


class _FakeSettings:
    def __init__(
        self,
        enabled: bool = True,
        *,
        contractInjection: bool = True,
        containment: bool = True,
    ) -> None:
        self.kpiMetricTierAEnabled = enabled
        self.kpiContractInjectionEnabled = contractInjection
        self.kpiMatchContainmentEnabled = containment


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

    def __init__(self, value: str | None, *, raises: bool = False) -> None:
        self._value = value
        self._raises = raises

    def begin_nested(self) -> _FakeSavepoint:
        return _FakeSavepoint()

    async def execute(self, _stmt: object) -> _FakeResult:
        if self._raises:
            raise RuntimeError("db down")
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


# =============================================================================
# readKpiMetricSwitch 取值语义（Tier A 之外的两个同构开关复用同一读取器）
# =============================================================================


@pytest.mark.asyncio()
@pytest.mark.parametrize("raw", ["0", "false", "off", "NO", "  False  "])
async def test_falsy_values_are_false(raw: str) -> None:
    assert await switches.readKpiMetricSwitch(_FakeSession(raw), "K", True) is False


@pytest.mark.asyncio()
@pytest.mark.parametrize("raw", ["1", "true", "on", "YES", "  True  "])
async def test_truthy_values_are_true(raw: str) -> None:
    assert await switches.readKpiMetricSwitch(_FakeSession(raw), "K", False) is True


@pytest.mark.asyncio()
@pytest.mark.parametrize(("raw", "expected"), [("3", True), ("0", False)])
async def test_numeric_values_parse(raw: str, expected: bool) -> None:
    assert await switches.readKpiMetricSwitch(_FakeSession(raw), "K", not expected) is expected


@pytest.mark.asyncio()
async def test_empty_value_falls_back_to_default() -> None:
    assert await switches.readKpiMetricSwitch(_FakeSession("   "), "K", True) is True


@pytest.mark.asyncio()
async def test_invalid_value_falls_back_to_default() -> None:
    assert await switches.readKpiMetricSwitch(_FakeSession("maybe"), "K", True) is True


@pytest.mark.asyncio()
async def test_read_exception_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings(True))
    assert await switches.isKpiMetricTierAEnabled(
        _FakeSession(None, raises=True)
    ) is False


# =============================================================================
# 读取语句本身：必须命中 system_config + 对应 key（防「读错表/读错键」仍绿）
# =============================================================================


class _CapturingSession(_FakeSession):
    """记录 execute 收到的语句，供编译后断言。"""

    def __init__(self, value: str | None) -> None:
        super().__init__(value)
        self.statements: list[object] = []

    async def execute(self, stmt: object) -> _FakeResult:
        self.statements.append(stmt)
        return await super().execute(stmt)


@pytest.mark.asyncio()
async def test_tier_a_switch_statement_targets_system_config_by_key(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings(True))
    session = _CapturingSession("true")
    assert await switches.isKpiMetricTierAEnabled(session) is True
    assert len(session.statements) == 1

    compiled = session.statements[0].compile(
        compile_kwargs={"literal_binds": True}
    )
    sql = str(compiled)
    assert "system_config" in sql
    assert switches.SWITCH_TIER_A in sql


# =============================================================================
# 两个同构灰度开关：env 杀闸与 system_config 行
# =============================================================================


@pytest.mark.asyncio()
async def test_contract_injection_env_kill_switch(monkeypatch) -> None:
    monkeypatch.setattr(
        switches, "getSettings", lambda: _FakeSettings(contractInjection=False)
    )
    assert await switches.isKpiContractInjectionEnabled(_FakeSession("true")) is False


@pytest.mark.asyncio()
async def test_contract_injection_row_true(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings())
    assert await switches.isKpiContractInjectionEnabled(_FakeSession("true")) is True


@pytest.mark.asyncio()
async def test_contract_injection_row_missing_is_false(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings())
    assert await switches.isKpiContractInjectionEnabled(_FakeSession(None)) is False


@pytest.mark.asyncio()
async def test_match_containment_env_kill_switch(monkeypatch) -> None:
    monkeypatch.setattr(
        switches, "getSettings", lambda: _FakeSettings(containment=False)
    )
    assert await switches.isKpiMatchContainmentEnabled(_FakeSession("true")) is False


@pytest.mark.asyncio()
async def test_match_containment_row_true(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings())
    assert await switches.isKpiMatchContainmentEnabled(_FakeSession("true")) is True


@pytest.mark.asyncio()
async def test_match_containment_row_missing_is_false(monkeypatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _FakeSettings())
    assert await switches.isKpiMatchContainmentEnabled(_FakeSession(None)) is False
