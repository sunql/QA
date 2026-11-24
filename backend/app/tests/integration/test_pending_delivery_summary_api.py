"""待到货场景的轻量统计端点（欢迎态业务现状卡）。

设计动机（2026-11-24）：场景工作区的「业务现状」卡需要**进入即出数**，
走编排 run 要烧几千 token 且等 worker —— 统计卡直接调 handler（只读 SQL），
零 LLM 成本。路由只挂登录守卫；ACL 语义与 Agent 工具面一致（只读）。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.services.agent_tool_types import ToolResult
from app.tests.integration._agent_runs_api_helpers import OWNER_HEADERS
from app.tests.integration._agent_runs_api_helpers import client as client

SUMMARY_PATH = "/api/v1/procurement/pending-delivery/summary"


@pytest.mark.asyncio
async def test_summary_returns_structured_payload(
    client: AsyncClient, monkeypatch
) -> None:
    """默认参数：200 + summary/bySupplier/topLines 三段结构。"""
    captured: dict = {}

    async def _fakeHandler(session, args, ctx):
        captured["args"] = args
        return ToolResult(
            data={
                "summary": {"openLines": 12, "openQty": 5000.0,
                            "overdueLines": 7, "excludedRows": 3},
                "bySupplier": [{"supplierCode": "F109", "openLines": 5,
                                "openQty": 3000.0, "overdueLines": 4}],
                "topLines": [],
            },
            answer="ok",
        )

    from app.api.v1 import procurement_analytics

    monkeypatch.setattr(procurement_analytics, "pendingDeliveryHandler", _fakeHandler)

    resp = await client.get(SUMMARY_PATH, headers=OWNER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["openLines"] == 12
    assert body["bySupplier"][0]["supplierCode"] == "F109"
    assert "topLines" in body
    assert captured["args"]["monthsBack"] == 12


@pytest.mark.asyncio
async def test_summary_honors_query_slots(client: AsyncClient, monkeypatch) -> None:
    """datasourceId/monthsBack 透传 handler（槽位语义与场景一致）。"""
    captured: dict = {}

    async def _fakeHandler(session, args, ctx):
        captured["args"] = args
        return ToolResult(
            data={"summary": {"openLines": 0, "openQty": 0,
                              "overdueLines": 0, "excludedRows": 0},
                  "bySupplier": [], "topLines": []},
            answer="ok",
        )

    from app.api.v1 import procurement_analytics

    monkeypatch.setattr(procurement_analytics, "pendingDeliveryHandler", _fakeHandler)

    resp = await client.get(
        SUMMARY_PATH,
        headers=OWNER_HEADERS,
        params={"datasourceId": 7, "monthsBack": 3},
    )
    assert resp.status_code == 200
    assert captured["args"]["datasourceId"] == 7
    assert captured["args"]["monthsBack"] == 3


@pytest.mark.asyncio
async def test_summary_requires_login(client: AsyncClient) -> None:
    """未登录 401/403（与采购域读端点同口径）。"""
    resp = await client.get(SUMMARY_PATH)
    # 守卫面生效即可：未带合法会话 ⇒ 401/403/409（种子用户撞 mustChangePassword）
    assert resp.status_code in (401, 403, 409)
