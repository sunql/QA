"""SSE 帧序列化契约（Task 3）：真实驱动标量必须活着到达浏览器。

走**完整 HTTP 链路**：真实 PG 只读查询取出驱动产物标量（`Decimal` / `date` /
`datetime` / `uuid.UUID`），经真实事件总线投递，从真实 SSE 端点
`GET /api/v1/research/stream?sessionId=...`（`api/v1/research.py:117` 的 router
prefix `/research` + 路由 `/stream`，`sessionId` 是**查询参数**）收帧并 `json.loads`。

为什么在 PG（元数据库）上取标量而不是 Oracle（业务库）：本用例要证明的是
**帧渲染**对驱动返回类型的兼容性，取的是「与业务库同构的驱动产物类型」
（asyncpg 的 numeric/date/timestamp/uuid 与 Oracle 驱动同族）；业务库连接
不在集成测试环境内，而值本身是**驱动产物、非手工构造**（前置断言钉住类型）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.enums import DataSourceType
from app.domain.models import DataSource, User
from app.infrastructure.security.crypto import encryptApiKey
from app.services.research_agent_ports import EVENT_DONE, EVENT_STEP_DATA
from app.services.research_event_bus import EVENT_CONNECTED

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/research"
_SUBSCRIBE_TIMEOUT = 5.0

_DRIVER_SCALARS_SQL = (
    "SELECT 1.5::numeric AS amount,"
    " DATE '2024-01-15' AS created_on,"
    " TIMESTAMP '2024-01-15 10:30:00' AS created_at,"
    " '11111111-1111-1111-1111-111111111111'::uuid AS row_id"
)


async def _seedUserAndDatasource(dbSession: AsyncSession, username: str) -> dict[str, str]:
    dbSession.add(
        User(
            username=username,
            display_name=username,
            email=None,
            enabled=True,
            password_hash=None,
        )
    )
    dbSession.add(
        DataSource(
            name=f"sse-serialization-{uuid.uuid4().hex[:8]}",
            type=DataSourceType.ORACLE,
            host="oracle-test",
            port=1521,
            database_name="THBIDB",
            username="THBI",
            password_encrypted=encryptApiKey("test-password"),
            is_active=True,
            is_default=True,
            oracle_version="19.0.0.0.0",
        )
    )
    await dbSession.commit()
    return {"X-User-Id": username}


@pytest.fixture()
async def authHeaders(dbSession: AsyncSession) -> dict[str, str]:
    return await _seedUserAndDatasource(dbSession, "sse-serialization-user")


async def _driverProducedRows(dbSession: AsyncSession) -> list[dict[str, Any]]:
    """真实只读查询取驱动产物标量（不是手工构造的 Decimal/date/UUID）。"""
    result = await dbSession.execute(text(_DRIVER_SCALARS_SQL))
    return [dict(row) for row in result.mappings()]


def _streamUrl(sessionId: str) -> str:
    """SSE 端点真实路径：router prefix `/research` + `/stream`，`sessionId` 为查询参数。"""
    return f"{_BASE}/stream?sessionId={sessionId}"


async def _createSession(client: AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(f"{_BASE}/sessions", json={"question": "序列化契约会话"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _waitForSubscriber(
    sessionId: str, request: asyncio.Task[Any], timeout: float = _SUBSCRIBE_TIMEOUT
) -> None:
    """等端点真的订阅上总线（否则 publish 打在空集上，用例会假绿）。

    `request` 提前结束（路由级 404/401 等）时立即失败并把响应体带出来：否则
    用例只会「等超时」，把 HTTP 层错误伪装成订阅超时。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if researchModule._bus.subscriberCount(sessionId) >= 1:
            return
        if request.done():
            raise AssertionError(f"SSE 请求在订阅前就结束了: {request.exception() or request.result().text}")
        await asyncio.sleep(0.01)
    raise AssertionError(f"SSE 端点未在 {timeout}s 内订阅会话 {sessionId}")


def _parseFrames(body: str) -> list[dict[str, Any]]:
    """解析 SSE 文本为帧列表（跳过注释行心跳）。"""
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        if not block or block.startswith(":"):
            continue
        eventLine, dataLine = block.split("\n", 1)
        assert eventLine.startswith("event: "), block
        assert dataLine.startswith("data: "), block
        frames.append(
            {
                "event": eventLine.removeprefix("event: "),
                "data": json.loads(dataLine.removeprefix("data: ")),
            }
        )
    return frames


async def test_stream_frame_carries_driver_scalars_as_json_strings(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
) -> None:
    """驱动标量（Decimal/date/datetime/UUID）经 SSE 到达客户端时是字符串，且帧不炸。

    RED 时的失败形态：`_sseFrame` 的 `json.dumps` 对这些值 `TypeError` ⇒ 整帧
    渲染失败，`GET /stream` 直接抛错（连已协商好的帧格式都收不全）。
    """
    sessionId = await _createSession(client, authHeaders)
    rows = await _driverProducedRows(dbSession)
    assert len(rows) == 1

    # 前置断言：这些值确实是驱动产物（非 JSON 原生），否则用例前提不成立
    row = rows[0]
    assert isinstance(row["amount"], Decimal)
    assert isinstance(row["created_on"], date) and not isinstance(row["created_on"], datetime)
    assert isinstance(row["created_at"], datetime)
    assert isinstance(row["row_id"], uuid.UUID)

    task = asyncio.create_task(client.get(_streamUrl(sessionId), headers=authHeaders))
    try:
        await _waitForSubscriber(sessionId, task)
        # 与 research_agent_execution.py 的 EVENT_STEP_DATA 载荷同形
        await researchModule._bus.publish(
            sessionId, EVENT_STEP_DATA, {"index": 0, "data": rows, "rowCount": len(rows)}
        )
        await researchModule._bus.publish(sessionId, EVENT_DONE, {"degraded": False})
        resp = await task
    finally:
        if not task.done():
            task.cancel()

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _parseFrames(resp.text)
    assert [frame["event"] for frame in frames] == [EVENT_CONNECTED, EVENT_STEP_DATA, EVENT_DONE]

    payload = frames[1]["data"]
    assert payload["index"] == 0
    assert payload["rowCount"] == 1
    wireRow = payload["data"][0]
    assert wireRow["amount"] == "1.5"
    assert wireRow["created_on"] == "2024-01-15"
    assert wireRow["created_at"] == "2024-01-15T10:30:00"
    assert wireRow["row_id"] == "11111111-1111-1111-1111-111111111111"
    # 反向：不得有任何一格仍是驱动对象（JSON 里只可能存活字符串）
    assert all(isinstance(value, str) for value in wireRow.values())


async def test_stream_frame_for_json_safe_payload_is_unchanged(
    client: AsyncClient,
    authHeaders: dict[str, str],
) -> None:
    """JSON 安全载荷的输出与 `json.dumps(..., ensure_ascii=False)` 逐字节一致（drop-in）。

    守住「换 encoder 不改变既有帧格式」这条回归面：中文不转义、键序不变。
    """
    sessionId = await _createSession(client, authHeaders)
    payload = {"metrics": [{"name": "供应商准时率", "value": 0.95}], "degraded": False}

    task = asyncio.create_task(client.get(_streamUrl(sessionId), headers=authHeaders))
    try:
        await _waitForSubscriber(sessionId, task)
        await researchModule._bus.publish(sessionId, "research.esl", payload)
        await researchModule._bus.publish(sessionId, EVENT_DONE, {"degraded": False})
        resp = await task
    finally:
        if not task.done():
            task.cancel()

    assert resp.status_code == 200, resp.text
    expectedLine = f"data: {json.dumps(payload, ensure_ascii=False)}"
    assert expectedLine in resp.text
    assert "供应商准时率" in resp.text  # ensure_ascii=False 下中文不转义
