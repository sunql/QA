"""KPI 契约注入总闸口径统一（2026-10-30）。

修复的历史缺陷：同一个字符串 ``KPI_CONTRACT_INJECTION_ENABLED`` 曾被两套互不
相干的机制读取 ——

- env（``Settings.kpiContractInjectionEnabled``，默认 true）→ 管 nl2sql 侧的
  B3 治理契约块（``kpi_contract_reconcile.buildKpiContractBlock``）；
- system_config 行 → 管 chat 侧的 Tier B 签字口径块
  （``kpi_contract_builder.buildKpiContractBlock``）。

于是「先关后开」把 system_config 置 false 只关掉了 Tier B 那扇门，B3 那扇仍
默认开着 ⇒ 灰度前置状态（两扇门都关）从未真正生效。

统一后的语义（见 ``app/services/kpi_metric_switches.isKpiContractInjectionEnabled``）：

1. env 紧急杀闸 false ⇒ 两条面一律不注入；
2. system_config 行值为真才开；**行缺失 / 非法 / 读取异常 ⇒ 关**（fail-closed）。

测试策略（含反向守卫）：断言「关」不足以证明实现正确 —— 一个恒不注入的实现
能让所有「关」断言全绿。故每个面都配一条「开 ⇒ 确实产生了内容/确实越过了
闸门」的正向断言。

⚠️ fail-closed 与「行存在」的耦合：system_config 行在测试库里默认不存在
（TRUNCATE 清空 + 无迁移播种）⇒ 除专门测「开」的用例外，其余用例都要显式
播种开关行，否则会被 fail-closed 拦下。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.kpi_contract_builder as builder_module
import app.services.kpi_metric_switches as switches_module
from app.domain.models import KpiCatalog, OntologyMetric
from app.models.system_config import SystemConfig
from app.services.kpi_contract_reconcile import buildKpiContractBlock as buildReconcileBlock
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_metric_switches import (
    SWITCH_CONTRACT_INJECTION,
    isKpiContractInjectionEnabled,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 夹具 / 工具
# ---------------------------------------------------------------------------


async def _setSwitch(session: AsyncSession, value: str | None) -> None:
    """写 / 删 system_config 开关行（value=None ⇒ 删行，模拟「行缺失」）。"""
    await session.execute(
        delete(SystemConfig).where(SystemConfig.key == SWITCH_CONTRACT_INJECTION)
    )
    await session.commit()
    if value is not None:
        session.add(SystemConfig(key=SWITCH_CONTRACT_INJECTION, value=value))
        await session.commit()


async def _readSwitchValue(session: AsyncSession) -> str | None:
    row = (
        await session.execute(
            select(SystemConfig).where(SystemConfig.key == SWITCH_CONTRACT_INJECTION)
        )
    ).scalar_one_or_none()
    return None if row is None else row.value


def _envKillOff(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 env 紧急杀闸置为 false（进程级配置在测试里用替身注入）。"""

    class _FakeSettings:
        kpiContractInjectionEnabled = False

    monkeypatch.setattr(switches_module, "getSettings", lambda: _FakeSettings())


async def _seedMetric(
    session: AsyncSession, *, name: str, formula: str
) -> OntologyMetric:
    metric = OntologyMetric(
        metric_name=name, formula=formula, agg_function="SUM", created_by="switch-test"
    )
    session.add(metric)
    await session.commit()
    return metric


async def _seedKpi(
    session: AsyncSession,
    code: str,
    *,
    name: str | None = None,
    keywords: list[str] | None = None,
    numerator: str | None = None,
    denominator: str | None = None,
    grain: str | None = None,
    metricId: int | None = None,
) -> KpiCatalog:
    from decimal import Decimal

    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=name or code,
        status="PUBLISHED",
        metric_id=metricId,
        numerator=numerator,
        denominator=denominator,
        grain=grain,
        unit="%",
        semantic_keywords=keywords or [],
        match_threshold=Decimal("0.75"),
    )
    session.add(kpi)
    await session.commit()
    return kpi


async def _warmCache(session: AsyncSession) -> None:
    """warmUp（refreshOne 在冷缓存上是 no-op，测试必须显式整库预热）。"""
    cache = get_kpi_match_cache()
    cache.onKpiChanged()
    await cache.warmUp(session)


# ---------------------------------------------------------------------------
# 总闸语义
# ---------------------------------------------------------------------------


async def test_switch_off_when_row_missing(dbSession: AsyncSession) -> None:
    """行缺失 ⇒ 关（fail-closed）。这是本次口径统一的核心行为变更。"""
    await _setSwitch(dbSession, None)
    assert await _readSwitchValue(dbSession) is None
    assert await isKpiContractInjectionEnabled(dbSession) is False


async def test_switch_on_when_row_true(dbSession: AsyncSession) -> None:
    """行值 true ⇒ 开。"""
    await _setSwitch(dbSession, "true")
    assert await isKpiContractInjectionEnabled(dbSession) is True


async def test_switch_on_when_row_numeric_one(dbSession: AsyncSession) -> None:
    """行值 "1" ⇒ 开（复用既有布尔解析口径）。"""
    await _setSwitch(dbSession, "1")
    assert await isKpiContractInjectionEnabled(dbSession) is True


async def test_switch_off_when_row_false(dbSession: AsyncSession) -> None:
    """行值 false ⇒ 关。"""
    await _setSwitch(dbSession, "false")
    assert await isKpiContractInjectionEnabled(dbSession) is False


async def test_switch_off_when_row_invalid(dbSession: AsyncSession) -> None:
    """行值非法 ⇒ 关（fail-closed：配置面故障不得让注入「自动打开」）。"""
    await _setSwitch(dbSession, "maybe")
    assert await isKpiContractInjectionEnabled(dbSession) is False


async def test_switch_off_when_row_empty_string(dbSession: AsyncSession) -> None:
    """行值为空串 ⇒ 关（空值不是「开」的隐含表示）。"""
    await _setSwitch(dbSession, "")
    assert await isKpiContractInjectionEnabled(dbSession) is False


async def test_env_kill_switch_overrides_row_true(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env 紧急杀闸 false ⇒ 即使行值 true 也关（杀闸优先级最高）。"""
    await _setSwitch(dbSession, "true")
    _envKillOff(monkeypatch)
    assert await isKpiContractInjectionEnabled(dbSession) is False


async def test_env_true_does_not_open_missing_row(dbSession: AsyncSession) -> None:
    """反向守卫：env 默认 true **不会**打开灰度开关。

    没有这条，一个把 env 当作唯一判据的实现（= 修复前的 B3 面语义）会在
    「行缺失 ⇒ 关」这条断言上假绿。
    """
    await _setSwitch(dbSession, None)
    # 不打补丁 ⇒ 走真实 getSettings()（默认 kpiContractInjectionEnabled=true）
    assert switches_module.getSettings().kpiContractInjectionEnabled is True
    assert await isKpiContractInjectionEnabled(dbSession) is False


# ---------------------------------------------------------------------------
# 注入面一：nl2sql 侧 B3 治理契约块（kpi_contract_reconcile）
# ---------------------------------------------------------------------------


async def test_reconcile_block_none_when_row_missing(dbSession: AsyncSession) -> None:
    """B3 面：行缺失 ⇒ 不注入（修复前这扇门默认开着 ⇒ 灰度前置状态失效）。"""
    metric = await _seedMetric(
        dbSession, name="总闸-metric", formula="SUM(received_qty)/SUM(order_qty)"
    )
    await _seedKpi(
        dbSession,
        "KPI_SWITCH_B3_OFF",
        name="供货及时率",
        keywords=["供货"],
        numerator="received_qty",
        denominator="order_qty",
        grain="按月+供应商",
        metricId=metric.id,
    )
    await _warmCache(dbSession)
    await _setSwitch(dbSession, None)

    assert await buildReconcileBlock(dbSession, "KPI_SWITCH_B3_OFF 是多少") is None


async def test_reconcile_block_injected_when_row_true(
    dbSession: AsyncSession,
) -> None:
    """B3 面：行值 true ⇒ 真的产出契约块（反向守卫：防止恒不注入的假实现）。"""
    metric = await _seedMetric(
        dbSession, name="总闸-metric-on", formula="SUM(received_qty)/SUM(order_qty)"
    )
    await _seedKpi(
        dbSession,
        "KPI_SWITCH_B3_ON",
        name="供货及时率乙",
        keywords=["供货"],
        numerator="received_qty",
        denominator="order_qty",
        grain="按月+供应商",
        metricId=metric.id,
    )
    await _warmCache(dbSession)
    await _setSwitch(dbSession, "true")

    block = await buildReconcileBlock(dbSession, "KPI_SWITCH_B3_ON 是多少")

    assert block is not None
    assert "供货及时率乙" in block


async def test_reconcile_block_none_when_env_kill_switch_off(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3 面：env 杀闸关闭 ⇒ 不注入（哪怕行值 true）。"""
    metric = await _seedMetric(
        dbSession, name="总闸-metric-kill", formula="SUM(received_qty)/SUM(order_qty)"
    )
    await _seedKpi(
        dbSession,
        "KPI_SWITCH_B3_KILL",
        name="供货及时率丙",
        keywords=["供货"],
        numerator="received_qty",
        denominator="order_qty",
        grain="按月+供应商",
        metricId=metric.id,
    )
    await _warmCache(dbSession)
    await _setSwitch(dbSession, "true")
    _envKillOff(monkeypatch)

    assert await buildReconcileBlock(dbSession, "KPI_SWITCH_B3_KILL 是多少") is None


# ---------------------------------------------------------------------------
# 注入面二：chat 侧 Tier B 签字口径块（kpi_contract_builder）
# ---------------------------------------------------------------------------


def _recordMatchCalls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """替换内容匹配步骤，记录是否被调用（闸门是否放行）。

    这里刻意只隔离「内容」这一步，断言的是**闸门**行为：开 ⇒ 越过闸门进入
    匹配；关 ⇒ 连匹配都不发起（不查库、不匹配）。
    """
    calls: list[str] = []

    async def _fakeMatch(_session, question: str, _limit: int) -> list[str]:
        calls.append(question)
        return []

    monkeypatch.setattr(builder_module, "_matchKpiCodes", _fakeMatch)
    return calls


async def test_builder_skips_matching_when_row_missing(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier B 面：行缺失 ⇒ 返回空串且不发起匹配。"""
    calls = _recordMatchCalls(monkeypatch)
    await _setSwitch(dbSession, None)

    block = await builder_module.buildKpiContractBlock(dbSession, "问题")

    assert block == ""
    assert calls == []


async def test_builder_enters_matching_when_row_true(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier B 面：行值 true ⇒ 越过闸门进入匹配（反向守卫）。"""
    calls = _recordMatchCalls(monkeypatch)
    await _setSwitch(dbSession, "true")

    block = await builder_module.buildKpiContractBlock(dbSession, "问题")

    assert block == ""  # 匹配返回空 ⇒ 无内容可注入（内容由其它用例覆盖）
    assert calls == ["问题"]


async def test_builder_skips_matching_when_env_kill_switch_off(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier B 面：env 杀闸关闭 ⇒ 不发起匹配（哪怕行值 true）。"""
    calls = _recordMatchCalls(monkeypatch)
    await _setSwitch(dbSession, "true")
    _envKillOff(monkeypatch)

    block = await builder_module.buildKpiContractBlock(dbSession, "问题")

    assert block == ""
    assert calls == []


# ---------------------------------------------------------------------------
# 两条面共用一个决策（不许各读各的）
# ---------------------------------------------------------------------------


async def test_both_surfaces_follow_the_same_row(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一行同时决定两条面（不是两个独立开关各读各的）。

    判据：行值 true ⇒ 两条面都越过闸门；行值 false ⇒ 两条面都不越过。
    若实现退化成「各自读各自的判据」，本用例会在其中一侧失败。
    """
    import app.services.kpi_semantic_match_service as match_module

    reconcileCalls: list[str] = []
    builderCalls: list[str] = []

    async def _fakeMatchAll(self, question: str, limit: int = 3) -> list:  # noqa: ANN001
        reconcileCalls.append(question)
        return []

    async def _fakeBuilderMatch(_session, question: str, _limit: int) -> list[str]:
        builderCalls.append(question)
        return []

    monkeypatch.setattr(
        match_module.KpiSemanticMatchService, "matchAll", _fakeMatchAll
    )
    monkeypatch.setattr(builder_module, "_matchKpiCodes", _fakeBuilderMatch)

    await _setSwitch(dbSession, "true")
    await buildReconcileBlock(dbSession, "问题A")
    await builder_module.buildKpiContractBlock(dbSession, "问题B")
    assert reconcileCalls == ["问题A"], "行值 true 时 B3 面必须越过闸门"
    assert builderCalls == ["问题B"], "行值 true 时 Tier B 面必须越过闸门"

    reconcileCalls.clear()
    builderCalls.clear()
    await _setSwitch(dbSession, "false")
    await buildReconcileBlock(dbSession, "问题A")
    await builder_module.buildKpiContractBlock(dbSession, "问题B")
    assert reconcileCalls == [], "行值 false 时 B3 面必须被拦下"
    assert builderCalls == [], "行值 false 时 Tier B 面必须被拦下"
