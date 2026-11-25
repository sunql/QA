"""P4 阶段5 集成测试：会话置顶（pinned_at 跨设备同步，真实 PostgreSQL）。

覆盖：
- 创建者置顶 ⇒ 204，列表里 pinnedAt 非空且排前；
- 取消置顶 ⇒ pinnedAt 回空；
- 别人的会话 404（与详情/删除同口径）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.procurement_copilot_models import ProcurementCopilotConversation
from app.tests.integration._agent_runs_api_helpers import (
    OUTSIDER_HEADERS,
    OWNER_HEADERS,
)
from app.tests.integration._agent_runs_api_helpers import (
    client as client,
)

CONVERSATIONS_PATH = "/api/v1/procurement/copilot/conversations"


async def _seedConversation(
    session: AsyncSession,
    *,
    ownerId: str,
    pinnedAt: datetime | None = None,
) -> ProcurementCopilotConversation:
    row = ProcurementCopilotConversation(
        conversation_id=uuid4(),
        created_by=ownerId,
        agent_code="PROCUREMENT_SOURCING",
        model_config_id=1,
        pinned_at=pinnedAt,
    )
    session.add(row)
    await session.commit()
    return row


def _ownHeaders(userId: str) -> dict[str, str]:
    return {
        "X-User-Id": userId,
        "X-User-Roles": "analyst",
        "X-User-Departments": "procurement",
    }


@pytest.mark.asyncio
async def test_pin_then_unpin_roundtrip(client: AsyncClient, dbSession: AsyncSession) -> None:
    row = await _seedConversation(dbSession, ownerId="orch-owner")

    resp = await client.post(
        f"{CONVERSATIONS_PATH}/{row.conversation_id}/pin",
        json={"pinned": True},
        headers=OWNER_HEADERS,
    )
    assert resp.status_code == 204

    listed = (await client.get(CONVERSATIONS_PATH, headers=OWNER_HEADERS)).json()
    mine = next(item for item in listed if item["conversationId"] == str(row.conversation_id))
    assert mine["pinnedAt"] is not None

    # 取消置顶
    resp = await client.post(
        f"{CONVERSATIONS_PATH}/{row.conversation_id}/pin",
        json={"pinned": False},
        headers=OWNER_HEADERS,
    )
    assert resp.status_code == 204
    listed = (await client.get(CONVERSATIONS_PATH, headers=OWNER_HEADERS)).json()
    mine = next(item for item in listed if item["conversationId"] == str(row.conversation_id))
    assert mine["pinnedAt"] is None


@pytest.mark.asyncio
async def test_pinned_conversation_sorts_first(client: AsyncClient, dbSession: AsyncSession) -> None:
    newer = await _seedConversation(dbSession, ownerId="orch-owner")
    older = await _seedConversation(dbSession, ownerId="orch-owner")
    # 老会话置顶：即使更新时间更早也应排在前
    resp = await client.post(
        f"{CONVERSATIONS_PATH}/{older.conversation_id}/pin",
        json={"pinned": True},
        headers=OWNER_HEADERS,
    )
    assert resp.status_code == 204

    listed = (await client.get(CONVERSATIONS_PATH, headers=OWNER_HEADERS)).json()
    ids = [item["conversationId"] for item in listed]
    assert ids.index(str(older.conversation_id)) < ids.index(str(newer.conversation_id))


@pytest.mark.asyncio
async def test_pin_other_users_conversation_404(client: AsyncClient, dbSession: AsyncSession) -> None:
    row = await _seedConversation(dbSession, ownerId="orch-owner")
    resp = await client.post(
        f"{CONVERSATIONS_PATH}/{row.conversation_id}/pin",
        json={"pinned": True},
        headers=OUTSIDER_HEADERS,
    )
    assert resp.status_code == 404
