"""Task 7 集成测试：编排运行的 list 路径（真实 PostgreSQL + 完整 API 链路）。

覆盖：
- 按 agentCode/status 过滤、limit/offset 有界、只返回调用者自己的 run
"""

from __future__ import annotations

from httpx import AsyncClient

from app.tests.integration._agent_runs_api_helpers import (
    _AGENT_CODE,
    _BASE_PATH,
    OUTSIDER_HEADERS,
    OWNER_HEADERS,
    _createAgent,
    _createModel,
    _createProfile,
    _createRun,
    _createTool,
)
from app.tests.integration._agent_runs_api_helpers import client as client

# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_returns_only_callers_runs_and_filters(
    client: AsyncClient,
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)

    created = await _createRun(client, modelConfigId=modelId)
    assert created.status_code == 202, created.text
    mine = created.json()["runId"]

    outsider = await _createRun(
        client, modelConfigId=modelId, headers=OUTSIDER_HEADERS
    )
    assert outsider.status_code == 202, outsider.text
    theirs = outsider.json()["runId"]

    listing = await client.get(_BASE_PATH, headers=OWNER_HEADERS)
    assert listing.status_code == 200, listing.text
    ids = [row["runId"] for row in listing.json()]
    assert ids == [mine]
    assert theirs not in ids

    byAgent = await client.get(
        f"{_BASE_PATH}?agentCode={_AGENT_CODE}", headers=OWNER_HEADERS
    )
    assert byAgent.status_code == 200, byAgent.text
    assert [row["runId"] for row in byAgent.json()] == [mine]

    otherAgent = await client.get(
        f"{_BASE_PATH}?agentCode=OTHER_AGENT", headers=OWNER_HEADERS
    )
    assert otherAgent.status_code == 200, otherAgent.text
    assert otherAgent.json() == []

    byStatus = await client.get(f"{_BASE_PATH}?status=PAUSED", headers=OWNER_HEADERS)
    assert byStatus.status_code == 200, byStatus.text
    assert byStatus.json() == []

    byQueued = await client.get(f"{_BASE_PATH}?status=QUEUED", headers=OWNER_HEADERS)
    assert byQueued.status_code == 200, byQueued.text
    assert [row["runId"] for row in byQueued.json()] == [mine]


async def test_list_honors_bounded_limit_and_offset(client: AsyncClient) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)

    createdIds = []
    for _ in range(3):
        resp = await _createRun(client, modelConfigId=modelId)
        assert resp.status_code == 202, resp.text
        createdIds.append(resp.json()["runId"])

    limited = await client.get(f"{_BASE_PATH}?limit=2", headers=OWNER_HEADERS)
    assert limited.status_code == 200, limited.text
    assert len(limited.json()) == 2

    offset = await client.get(f"{_BASE_PATH}?limit=2&offset=1", headers=OWNER_HEADERS)
    assert offset.status_code == 200, offset.text
    assert len(offset.json()) == 2
    assert set(createdIds) >= {row["runId"] for row in offset.json()}

    # limit 被压到上限 200：请求 5000 也只返回全部 3 条，不报错
    capped = await client.get(f"{_BASE_PATH}?limit=5000", headers=OWNER_HEADERS)
    assert capped.status_code == 200, capped.text
    assert len(capped.json()) == 3
