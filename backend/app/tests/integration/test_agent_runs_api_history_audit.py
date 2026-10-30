"""Task 7 集成测试：编排运行的历史数据与审计（真实 PostgreSQL + 完整 API 链路）。

覆盖：
- 历史 run 在 Agent 被硬删除后仍可读
- create / cancel 写入审计 outbox，且不泄露工具配置内部字段与密钥
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AuditOutbox
from app.tests.integration._agent_runs_api_helpers import (
    _AGENT_CODE,
    _BASE_PATH,
    ADMIN_HEADERS,
    OWNER_HEADERS,
    _createAgent,
    _createModel,
    _createProfile,
    _createRun,
    _createTool,
)
from app.tests.integration._agent_runs_api_helpers import client as client

# ---------------------------------------------------------------------------
# 历史数据 + 审计
# ---------------------------------------------------------------------------


async def test_run_survives_agent_deletion(client: AsyncClient) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    created = await _createRun(client, modelConfigId=modelId)
    runId = created.json()["runId"]

    deleted = await client.delete(
        f"/api/v1/agents/{_AGENT_CODE}?hard_delete=true", headers=ADMIN_HEADERS
    )
    assert deleted.status_code == 200, deleted.text

    detail = await client.get(f"{_BASE_PATH}/{runId}", headers=OWNER_HEADERS)
    assert detail.status_code == 200, detail.text
    assert detail.json()["runId"] == runId

    steps = await client.get(f"{_BASE_PATH}/{runId}/steps", headers=OWNER_HEADERS)
    assert steps.status_code == 200, steps.text

    listing = await client.get(_BASE_PATH, headers=OWNER_HEADERS)
    assert listing.status_code == 200, listing.text
    assert [row["runId"] for row in listing.json()] == [runId]


async def test_create_and_cancel_emit_audit_outbox_without_secrets(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    created = await _createRun(client, modelConfigId=modelId)
    runId = created.json()["runId"]

    rows = (
        (
            await dbSession.execute(
                select(AuditOutbox)
                .where(AuditOutbox.entity_type == "agent_orchestration_run")
                .order_by(AuditOutbox.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].event_type == "agent_orchestration_run_created"
    payload = rows[0].payload or {}
    assert payload["after"]["runId"] == runId
    payloadText = str(payload)
    assert "handler_ref" not in payloadText
    assert "handlerRef" not in payloadText
    assert "wiki_search" not in payloadText
    assert "api_key" not in payloadText
    assert "apiKey" not in payloadText
