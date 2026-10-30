"""S5 的总闸口径（2026-10-30 fail-closed 之后）。

**为什么单独一个文件**：S5 的判据是「契约块空/非空」，而**空块有两种完全
不同的原因** ——「总闸关闭」与「对账 mismatch 拦截」。2026-10-30 之前 S5 不
判闸门直接看块 ⇒

- 全部 mismatch 时「块必须为空」这条**恒真**（闸门关着也是空）⇒ 假绿；
- 存在非 mismatch 时「块必须非空」这条**恒假**（闸门关着必然空）⇒ 假红。

总闸改成 fail-closed（默认关）之后这个坑变成常态（不翻开关就一直关），
所以 S5 入口必须先判闸门，并把「闸门关」与「对账拦截」在输出里分开。

本文件覆盖**分支语义**（走哪条路、临时开关怎么用），不覆盖块文本本身
（块文本由 realdata 脚本在真库上验）。
"""

from __future__ import annotations

import pytest

from app.services.kpi_metric_switches import SWITCH_CONTRACT_INJECTION
from scripts.kpi_metric_realdata_lib import SKIPS


@pytest.fixture(autouse=True)
def _cleanSkips():
    SKIPS.clear()
    try:
        yield
    finally:
        SKIPS.clear()


def _patchCommon(monkeypatch: pytest.MonkeyPatch, *, switchOn: bool) -> dict:
    """把 S5 的外部依赖换成可控替身；返回调用记录容器。"""
    from scripts import kpi_metric_realdata_inject as s5Module

    calls: dict[str, list] = {"switch": [], "verify": [], "simulate": []}

    async def _cacheFresh(*_args: object, **_kwargs: object) -> None:
        return None

    async def _countLinked(*_args: object, **_kwargs: object) -> int:
        return 1

    async def _switchEnabled(*_args: object, **_kwargs: object) -> bool:
        return switchOn

    async def _runWithSwitch(session, key, value, action, label):  # noqa: ANN001
        calls["switch"].append((key, value, label))
        await action()

    async def _verify(*_args: object, **_kwargs: object) -> None:
        calls["verify"].append(True)

    async def _simulate(*_args: object, **_kwargs: object) -> None:
        calls["simulate"].append(True)

    monkeypatch.setattr(
        s5Module.get_kpi_match_cache(), "ensureFresh", _cacheFresh
    )
    monkeypatch.setattr(s5Module, "countLinkedKpis", _countLinked)
    monkeypatch.setattr(s5Module, "isKpiContractInjectionEnabled", _switchEnabled)
    monkeypatch.setattr(s5Module, "runWithSwitch", _runWithSwitch)
    monkeypatch.setattr(s5Module, "_verifyInjection", _verify)
    monkeypatch.setattr(s5Module, "_reviewSimulation", _simulate)
    return calls


async def test_switch_on_runs_verification_without_touching_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总闸已开 ⇒ 直接验证，**不**临时改写开关行。"""
    from scripts import kpi_metric_realdata_inject as s5Module

    calls = _patchCommon(monkeypatch, switchOn=True)

    await s5Module.step5_contractInjection(object(), isProd=False)  # type: ignore[arg-type]

    assert calls["switch"] == [], "总闸已开时不该改写开关行"
    assert calls["verify"] == [True], "总闸已开时必须真的跑注入验证"
    assert calls["simulate"] == [True], "审核模拟也要跑"


async def test_switch_off_in_test_db_uses_temporary_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总闸关 + 隔离测试库 ⇒ 临时把开关写成 true 再验（退出时还原）。

    判据：临时开关必须用**契约注入总闸那个 key**、值必须是 ``"true"``，
    且验证与审核模拟都要在临时窗口内跑（模拟在窗口外跑 ⇒「对齐后块非空」
    这条必然假红）。
    """
    from scripts import kpi_metric_realdata_inject as s5Module

    calls = _patchCommon(monkeypatch, switchOn=False)

    await s5Module.step5_contractInjection(object(), isProd=False)  # type: ignore[arg-type]

    assert len(calls["switch"]) == 1, "测试库路径必须临时改写一次开关"
    key, value, _label = calls["switch"][0]
    assert key == SWITCH_CONTRACT_INJECTION
    assert value == "true"
    assert calls["verify"] == [True]
    assert calls["simulate"] == [True]


async def test_switch_off_in_prod_skips_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总闸关 + 正式库 ⇒ SKIP，且理由要说清「空块是闸门不是对账拦截」。"""
    from scripts import kpi_metric_realdata_inject as s5Module

    calls = _patchCommon(monkeypatch, switchOn=False)

    await s5Module.step5_contractInjection(object(), isProd=True)  # type: ignore[arg-type]

    assert calls["switch"] == [], "正式库不得改写开关行"
    assert calls["verify"] == [], "正式库跳过时不该跑注入验证"
    reasons = [reason for name, reason in SKIPS if "S5" in name]
    assert reasons, "必须登记 SKIP（否则会被读成「通过」）"
    assert "闸门" in reasons[0]
    assert "不是对账拦截" in reasons[0]
