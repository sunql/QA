"""KPI 同步/向量匹配开关：env 杀闸 + system_config 灰度（方向见各 docstring）。

两个开关的**默认方向相反**（见 ``kpi_metric_switches`` 内 docstring）：

- ``isKpiSyncEnabled``：system_config 行缺失/非法/异常 ⇒ **on**
  （同步是存储面能力，缺行不应让新建 KPI 静默失去向量/图）。
- ``isKpiMatchVectorEnabled``：system_config 行缺失/非法/异常 ⇒ **off**
  （fail-closed：向量召回改变 chat 行为）。

本文件只测开关读取逻辑（纯内存 fake session），不碰 Milvus / Neo4j。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import kpi_metric_switches as switches


class _FakeResult:
    def __init__(self, cfg) -> None:
        self._cfg = cfg

    def scalar_one_or_none(self):
        return self._cfg


class _FakeNested:
    def __enter__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class _FakeSession:
    """满足 readKpiMetricSwitch 的最小面：begin_nested + execute。"""

    def __init__(self, cfg) -> None:
        self._cfg = cfg

    def begin_nested(self):
        return _FakeNested()

    async def execute(self, *_a, **_k):
        return _FakeResult(self._cfg)


def _settings(**overrides) -> SimpleNamespace:
    """构造 getSettings() 的替身（默认两个开关的 env 值都打开）。"""
    base = {"kpiSyncEnabled": True, "kpiMatchVectorEnabled": True}
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_sync_enabled_env_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        switches,
        "getSettings",
        lambda: _settings(kpiSyncEnabled=False),
    )
    assert await switches.isKpiSyncEnabled(_FakeSession(None)) is False


@pytest.mark.asyncio
async def test_sync_enabled_default_on_when_row_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """方向反转：system_config 行缺失 ⇒ on（与 fail-closed 开关相反）。"""
    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    assert await switches.isKpiSyncEnabled(_FakeSession(None)) is True


@pytest.mark.asyncio
async def test_sync_enabled_row_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    cfg = SimpleNamespace(value="false")
    assert await switches.isKpiSyncEnabled(_FakeSession(cfg)) is False


@pytest.mark.asyncio
async def test_sync_enabled_row_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    cfg = SimpleNamespace(value="true")
    assert await switches.isKpiSyncEnabled(_FakeSession(cfg)) is True


@pytest.mark.asyncio
async def test_sync_enabled_invalid_row_falls_back_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法取值 ⇒ 回落默认（on）：缺行/非法都不该静默关掉同步。"""
    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    cfg = SimpleNamespace(value="not-a-bool")
    assert await switches.isKpiSyncEnabled(_FakeSession(cfg)) is True


@pytest.mark.asyncio
async def test_sync_enabled_read_error_falls_back_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取异常 ⇒ on（readKpiMetricSwitch 内部兜底为默认值）。"""

    class _BoomSession(_FakeSession):
        async def execute(self, *_a, **_k):
            raise RuntimeError("boom")

    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    assert await switches.isKpiSyncEnabled(_BoomSession(None)) is True


@pytest.mark.asyncio
async def test_match_vector_default_off_when_row_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fail-closed：行缺失 ⇒ off（消费面灰度惯例）。"""
    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    assert await switches.isKpiMatchVectorEnabled(_FakeSession(None)) is False


@pytest.mark.asyncio
async def test_match_vector_env_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        switches,
        "getSettings",
        lambda: _settings(kpiMatchVectorEnabled=False),
    )
    assert await switches.isKpiMatchVectorEnabled(_FakeSession(None)) is False


@pytest.mark.asyncio
async def test_match_vector_row_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    cfg = SimpleNamespace(value="true")
    assert await switches.isKpiMatchVectorEnabled(_FakeSession(cfg)) is True


@pytest.mark.asyncio
async def test_match_vector_row_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    cfg = SimpleNamespace(value="false")
    assert await switches.isKpiMatchVectorEnabled(_FakeSession(cfg)) is False


def test_switch_keys_are_stable() -> None:
    """开关 key 常量固定（system_config 行名是运维契约，不得漂移）。"""
    assert switches.SWITCH_KPI_SYNC == "KPI_SYNC_ENABLED"
    assert switches.SWITCH_MATCH_VECTOR == "KPI_MATCH_VECTOR_ENABLED"
