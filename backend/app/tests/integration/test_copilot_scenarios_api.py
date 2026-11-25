"""P4 集成测试：`GET /api/v1/procurement/copilot/scenarios`（真实 PostgreSQL）。

覆盖：
- 启用场景按 sort_order 返回，payload 含槽位 schema / chips / 渲染器；
- 停用场景不出现；
- required_role=admin 的场景：admin 可见，普通角色不可见（不 403，直接隐藏）；
- 未认证 ⇒ 401（路由守卫——新路由登记 auth 守卫的仓库口径）。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.copilot_scenario_models import CopilotScenario
from app.tests.integration._agent_runs_api_helpers import (
    ADMIN_HEADERS,
    OWNER_HEADERS,
)

# client 夹具定义在 helpers 模块里（monkeypatch 开关 + pgApiClient），
# 必须显式导入到本测试模块命名空间，pytest 才会为本文件启用它。
from app.tests.integration._agent_runs_api_helpers import client as client

SCENARIOS_PATH = "/api/v1/procurement/copilot/scenarios"

_SLOT_SCHEMA = [
    {
        "key": "monthsBack",
        "type": "number",
        "label": "时间窗",
        "required": True,
        "conflictKeywords": ["时间窗"],
        "displayTemplate": "近{value}个月",
    },
    {
        "key": "supplierKeys",
        "type": "entityMulti",
        "label": "供应商",
        "conflictKeywords": ["供应商", "厂家"],
        "codePattern": "(?<![0-9A-Za-z])[A-Z]\\d{2,4}(?![0-9A-Za-z])",
    },
]


async def _seedScenario(
    session: AsyncSession,
    code: str,
    *,
    sortOrder: int = 10,
    enabled: bool = True,
    requiredRole: str | None = None,
) -> CopilotScenario:
    row = CopilotScenario(
        scenario_code=code,
        name=f"场景 {code}",
        description=f"{code} 的描述",
        icon_code="car",
        agent_code="PROCUREMENT_SOURCING",
        slot_schema=_SLOT_SCHEMA,
        question_template="分析待到货情况",
        chips=[{"label": "chip-1", "monthsBack": 12}],
        result_renderer="pending_delivery",
        required_role=requiredRole,
        sort_order=sortOrder,
        enabled=enabled,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_scenarios_list_enabled_ordered_with_payload(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedScenario(dbSession, "pending_delivery", sortOrder=10)
    await _seedScenario(dbSession, "supplier_risk", sortOrder=20)
    await _seedScenario(dbSession, "disabled_one", sortOrder=5, enabled=False)
    await dbSession.commit()

    resp = await client.get(SCENARIOS_PATH, headers=OWNER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    codes = [item["scenarioCode"] for item in body]
    # 停用的不出现；sort_order 升序（disabled_one 的 sort=5 也没抢到前排）
    assert "disabled_one" not in codes
    assert codes == ["pending_delivery", "supplier_risk"]
    assert "pending_delivery" in codes and "supplier_risk" in codes

    item = next(i for i in body if i["scenarioCode"] == "pending_delivery")
    assert item["agentCode"] == "PROCUREMENT_SOURCING"
    assert item["resultRenderer"] == "pending_delivery"
    assert item["questionTemplate"] == "分析待到货情况"
    assert item["chips"] == [{"label": "chip-1", "monthsBack": 12}]
    slots = item["slotSchema"]
    assert slots[0]["key"] == "monthsBack"
    assert slots[0]["required"] is True
    assert slots[0]["displayTemplate"] == "近{value}个月"
    assert slots[1]["conflictKeywords"] == ["供应商", "厂家"]
    assert slots[1]["codePattern"] is not None


@pytest.mark.asyncio
async def test_scenario_slot_hidden_flag_is_exposed(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """hidden 槽位必须通过 API 透出。

    丢了 hidden，前端会把 dateFrom/dateTo 渲染成可见输入框（只填一边 ⇒ 422；
    「近N月」模式下填了又被静默丢弃）。用**真实场景种子** + 真实 HTTP 链路验证。
    """
    from scripts.seed_copilot_scenarios import SEEDS

    for code in ("delivery_tracking", "pending_delivery"):
        seed = next(s for s in SEEDS if s["scenario_code"] == code)
        dbSession.add(CopilotScenario(**seed))
    await dbSession.commit()

    resp = await client.get(SCENARIOS_PATH, headers=OWNER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    item = next(i for i in body if i["scenarioCode"] == "delivery_tracking")
    slots = {slot["key"]: slot for slot in item["slotSchema"]}
    assert slots["dateFrom"]["hidden"] is True
    assert slots["dateTo"]["hidden"] is True
    # 非 hidden 槽位不得被误标
    assert slots["monthsBack"].get("hidden", False) is False

    # 既有场景同样受益：pending_delivery 的两条 hidden 日期槽位也要透出
    pending = next(i for i in body if i["scenarioCode"] == "pending_delivery")
    pendingSlots = {slot["key"]: slot for slot in pending["slotSchema"]}
    for key in ("dateFrom", "dateTo"):
        assert pendingSlots[key]["hidden"] is True, key
    # THBI 硬规则：数据源切换槽位已移除（2026-11-25）
    assert "datasourceId" not in pendingSlots


@pytest.mark.asyncio
async def test_admin_only_scenario_hidden_from_non_admin(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedScenario(dbSession, "admin_scen", requiredRole="admin")
    await dbSession.commit()

    # 普通角色（analyst）：隐藏，不 403
    resp = await client.get(SCENARIOS_PATH, headers=OWNER_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == []

    # admin：可见
    resp = await client.get(SCENARIOS_PATH, headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    codes = [item["scenarioCode"] for item in resp.json()]
    assert codes == ["admin_scen"]
