"""M6-1 ERP/SRM 命令端口与模拟器单元守卫。

判据要点（设计稿 §6.6 + §6.3）：
1. **PROHIBITED_AUTO 代码层不可达**：端口协议**不提供** autoAward / signContract /
   payInvoice ⇒ 模拟器与任何实现都无法凭空造出该能力（属性探测断言）；
2. **幂等**：同 idempotencyKey ⇒ 同 externalRef（不重复发送）；
3. **确定性外部引用**：同 draft + 同 key ⇒ 同引用（可追溯、可对账）；
4. **默认端口 = 模拟器**（真实适配器接入前不产生真实副作用）；
5. 状态查询返回同一外部引用（状态回写锚点）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.procurement_command_port import (
    ProcurementCommandPort,
    RfqSendResult,
    SimulatedErpSrmAdapter,
    defaultCommandPort,
)


def _draft() -> dict[str, Any]:
    return {
        "rfqDraftId": "d-1",
        "requestId": "PR-2026-0001",
        "supplierKeys": [900001],
        "lineItems": [{"materialKey": 500001, "quantity": 100, "unit": "TON"}],
    }


@pytest.mark.asyncio
async def test_simulator_is_idempotent_for_same_key() -> None:
    adapter = SimulatedErpSrmAdapter()
    first = await adapter.sendRfq(_draft(), idempotencyKey="k-1")
    second = await adapter.sendRfq(_draft(), idempotencyKey="k-1")
    assert first.externalRef == second.externalRef, "同幂等键 ⇒ 同一外部引用（不重复发送）"
    assert first.status == "SENT"


@pytest.mark.asyncio
async def test_simulator_different_key_gives_different_ref() -> None:
    adapter = SimulatedErpSrmAdapter()
    a = await adapter.sendRfq(_draft(), idempotencyKey="k-1")
    b = await adapter.sendRfq(_draft(), idempotencyKey="k-2")
    assert a.externalRef != b.externalRef, "不同幂等键 ⇒ 不同外部引用"


@pytest.mark.asyncio
async def test_simulator_external_ref_is_deterministic() -> None:
    a = SimulatedErpSrmAdapter()
    b = SimulatedErpSrmAdapter()
    refA = await a.sendRfq(_draft(), idempotencyKey="k-1")
    refB = await b.sendRfq(_draft(), idempotencyKey="k-1")
    assert refA.externalRef == refB.externalRef, "确定性引用（同 draft + 同 key）"
    assert refA.externalRef.startswith("SIM-"), "模拟引用前缀可辨识"


@pytest.mark.asyncio
async def test_query_status_returns_same_ref() -> None:
    adapter = SimulatedErpSrmAdapter()
    sent = await adapter.sendRfq(_draft(), idempotencyKey="k-1")
    status = await adapter.querySendStatus(sent.externalRef)
    assert status.externalRef == sent.externalRef
    assert status.status == "SENT"


def test_prohibited_auto_actions_are_not_on_the_port() -> None:
    """PROHIBITED_AUTO（自动定标/签署/付款）**代码层不可达**。"""
    forbidden = ("autoAward", "signContract", "payInvoice", "award", "sign", "pay")
    for name in forbidden:
        assert not hasattr(ProcurementCommandPort, name), (
            f"端口协议不得提供 {name}（PROHIBITED_AUTO 首期禁止）"
        )
    adapter = SimulatedErpSrmAdapter()
    for name in forbidden:
        assert not hasattr(adapter, name), (
            f"模拟器不得提供 {name}（PROHIBITED_AUTO 首期禁止）"
        )


def test_default_port_is_simulator() -> None:
    port = defaultCommandPort()
    assert isinstance(port, SimulatedErpSrmAdapter), "默认端口 = 模拟器（真实适配器未接入）"
    assert isinstance(port, ProcurementCommandPort), "模拟器满足端口协议"


@pytest.mark.asyncio
async def test_failed_status_is_representable() -> None:
    adapter = SimulatedErpSrmAdapter(status="FAILED")
    result = await adapter.sendRfq(_draft(), idempotencyKey="k-1")
    assert result.status == "FAILED"
    assert isinstance(result, RfqSendResult)
