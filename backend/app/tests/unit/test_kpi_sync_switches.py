"""KPI 同步/向量匹配开关：env 杀闸 + system_config 灰度（方向见各 docstring）。

两个开关的**默认方向相反**（见 ``kpi_metric_switches`` 内 docstring）：

- ``isKpiSyncEnabled``：system_config 行缺失/非法/异常 ⇒ **on**
  （同步是存储面能力，缺行不应让新建 KPI 静默失去向量/图）。
- ``isKpiMatchVectorEnabled``：system_config 行缺失/非法/异常 ⇒ **off**
  （fail-closed：向量召回改变 chat 行为）。

本文件只测开关读取逻辑（纯内存 fake session），不碰 Milvus / Neo4j。
fake session 的形状照抄 ``test_kpi_metric_tier_a_switch.py``（同构开关的既有样板）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import kpi_metric_switches as switches


class _FakeResult:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def scalar_one_or_none(self):
        if self._value is None:
            return None
        return SimpleNamespace(value=self._value)


class _FakeNested:
    def __enter__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class _FakeSession:
    """满足 readKpiMetricSwitch 的最小面：begin_nested + execute。

    ``executeCount`` 记录读库次数 ⇒ 供「env 杀闸**短路**了读库」这类断言使用
    （防止「压根没查库直接返回 False」也能让用例变绿）。
    """

    def __init__(self, value: str | None, *, raises: bool = False) -> None:
        self._value = value
        self._raises = raises
        self.executeCount = 0

    def begin_nested(self):
        return _FakeNested()

    async def execute(self, *_a, **_k):
        self.executeCount += 1
        if self._raises:
            raise RuntimeError("db down")
        return _FakeResult(self._value)


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
async def test_sync_enabled_env_kill_wins_over_true_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env 杀闸必须**压过 DB 行**（行写着 true 也得关）。

    这是「env 优先级」硬约束的正面证明：同一个行值 ``"true"``，
    env 开 ⇒ True（见 test_sync_enabled_row_true）、env 关 ⇒ False
    ⇒ 若将来实现改成「行存在就查行」，本用例立刻打红。

    附加断言 ``executeCount == 0``：env 杀闸是**短路**（压根不查库），
    与「查了库但行没生效」是两回事。
    """
    monkeypatch.setattr(
        switches,
        "getSettings",
        lambda: _settings(kpiSyncEnabled=False),
    )
    session = _FakeSession("true")
    assert await switches.isKpiSyncEnabled(session) is False
    assert session.executeCount == 0


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
    assert await switches.isKpiSyncEnabled(_FakeSession("false")) is False


@pytest.mark.asyncio
async def test_sync_enabled_row_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """行存在 ⇒ 真的**读到了行**（executeCount == 1）且行值为真 ⇒ True。

    与 test_sync_enabled_env_kill_wins_over_true_row 配对构成双向证明：
    同一行值 "true"，env 决定生死 ⇒ 行本身被读了、优先级也被钉住了。
    """
    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    session = _FakeSession("true")
    assert await switches.isKpiSyncEnabled(session) is True
    assert session.executeCount == 1


@pytest.mark.asyncio
async def test_sync_enabled_invalid_row_falls_back_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法取值 ⇒ 回落默认（on）：缺行/非法都不该静默关掉同步。"""
    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    assert await switches.isKpiSyncEnabled(_FakeSession("not-a-bool")) is True


@pytest.mark.asyncio
async def test_sync_enabled_read_error_falls_back_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取异常 ⇒ on（readKpiMetricSwitch 内部兜底为默认值）。"""
    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    assert await switches.isKpiSyncEnabled(_FakeSession(None, raises=True)) is True


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
async def test_match_vector_env_kill_wins_over_true_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env 杀闸必须**压过 DB 行**（行写着 true 也得关）——照 test_env_kill_switch_wins 形状。"""
    monkeypatch.setattr(
        switches,
        "getSettings",
        lambda: _settings(kpiMatchVectorEnabled=False),
    )
    session = _FakeSession("true")
    assert await switches.isKpiMatchVectorEnabled(session) is False
    assert session.executeCount == 0


@pytest.mark.asyncio
async def test_match_vector_row_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """与 env 杀闸用例配对：同一行值 "true"，env 开 ⇒ True 且行确实被读到。"""
    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    session = _FakeSession("true")
    assert await switches.isKpiMatchVectorEnabled(session) is True
    assert session.executeCount == 1


@pytest.mark.asyncio
async def test_match_vector_row_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(switches, "getSettings", lambda: _settings())
    assert await switches.isKpiMatchVectorEnabled(_FakeSession("false")) is False


def test_switch_keys_are_stable() -> None:
    """开关 key 常量固定（system_config 行名是运维契约，不得漂移）。"""
    assert switches.SWITCH_KPI_SYNC == "KPI_SYNC_ENABLED"
    assert switches.SWITCH_MATCH_VECTOR == "KPI_MATCH_VECTOR_ENABLED"


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings 默认值回归：``kpiSyncEnabled`` 默认 True、``kpiMatchVectorEnabled`` 默认 False。

    ⚠️ 陷阱：``app/tests/conftest.py`` 在 import 期强设 ``KPI_SYNC_ENABLED=false``
    ⇒ 测默认值必须先清掉该环境变量；``_env_file=None`` 另可屏蔽 ``.env`` 文件
    （本仓 backend/.env 可能不存在，但不该让测试依赖它的有无）。
    """
    monkeypatch.delenv("KPI_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("KPI_MATCH_VECTOR_ENABLED", raising=False)
    settings = Settings(_env_file=None)
    assert settings.kpiSyncEnabled is True
    assert settings.kpiMatchVectorEnabled is False
