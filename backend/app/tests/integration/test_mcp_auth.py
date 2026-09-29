"""MCP HTTP 端点（``/mcp``）鉴权集成测试。

背景（2026-09-30 安全批次）：``/mcp`` 由 ``main.py`` 用 Starlette ``Mount("")``
挂到 ASGI 树末端，**绕过 FastAPI 依赖注入** —— router 级
``dependencies=[Depends(getCurrentUser)]`` 对它完全不生效。生产容器实测
（``AUTH_MODE=real`` + 无任何鉴权头）匿名可 ``initialize`` / ``tools/list`` /
抵达 ``tools/call`` handler，其中含 2 个**写工具**。

本测试钉住两件事：
1. 匿名 / 非 Bearer / 无效 Bearer 一律 403，且 body 是 ``ErrorResponse`` 形状
2. 有效 Bearer 走通完整握手，且 ``wiki_status`` 回报的 ``userId`` 来自该 token
   （不是 ``anonymous`` 桩身份）—— 这是「鉴权生效」+「身份链路打通」的双重验收

真实 PostgreSQL + 真实 ``app.main.app``（与生产同一条 Mount 链路）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import bcrypt
import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.error_messages import (
    MSG_AUTH_REQUIRED,
    MSG_TOKEN_INVALID,
)
from app.models.rbac import Role, User, UserRole, UserSession
from app.services.jwt_codec import sign_jwt

# streamable-http 必须同时接受 JSON 与 SSE（否则 406）
_JSON_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

_INITIALIZE_PARAMS: dict[str, Any] = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "test-mcp-auth", "version": "0"},
}


# ---------------------------------------------------------------------------
# 装配：真实 app + 真实 PG 会话工厂
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ensureJwtSecret(monkeypatch: pytest.MonkeyPatch) -> None:
    """空 secret 无法签名/验签 —— 与 test_get_current_user_bearer 同口径。"""
    if not getSettings().jwtSecret:
        monkeypatch.setenv("JWT_SECRET", "x" * 32)


def _innerLifespanApp(mountedApp: Any) -> Any:
    """剥掉 ``McpAuthMiddleware`` 外壳，拿到带 ``lifespan`` 的 fastmcp 子 app。

    中间件是纯 ASGI 包装（对非 http scope 直接透传），但它自身不暴露
    ``lifespan``；而 streamable-http 的 session manager 必须在子 app 的
    lifespan 里初始化（否则请求报 "task group was not initialized"）。
    """
    while not hasattr(mountedApp, "lifespan"):
        mountedApp = mountedApp.app
    return mountedApp


@asynccontextmanager
async def _mcpHttpClient() -> AsyncIterator[AsyncClient]:
    """指向真实 ``app.main.app`` 的客户端，带 fastmcp 子 app 的 lifespan。

    **不用 async fixture**：anyio 的 cancel scope 要求进入/退出在同一个 task，
    而 pytest-asyncio 的 async generator fixture 的 setup 与 teardown 不在
    同一 task（实测报 "Attempted to exit cancel scope in a different task"）。
    在测试函数体内 ``async with`` 就没有这个问题。

    调用方必须先拿到 ``dbSession`` fixture —— ``pgApiClient`` 会把全局会话工厂
    换成真实 PG；中间件内部用 ``getSessionFactory()`` 建 session，不走 FastAPI
    依赖注入，只能靠这个全局替换。
    """
    from starlette.routing import Mount

    from app.main import app as realApp

    mount = next(route for route in realApp.router.routes if isinstance(route, Mount))
    innerApp = _innerLifespanApp(mount.app)
    async with innerApp.lifespan(innerApp):
        transport = ASGITransport(app=realApp)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ---------------------------------------------------------------------------
# JSON-RPC / 握手 helper
# ---------------------------------------------------------------------------


def _parseRpc(response: Response) -> Any:
    """解析 JSON-RPC 响应：streamable-http 可能回 SSE（text/event-stream）。"""
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"SSE 响应里没有 data 行: {response.text[:200]!r}")
    return response.json()


async def _rpc(
    ac: AsyncClient,
    method: str,
    *,
    rpcId: int | None = None,
    params: dict[str, Any] | None = None,
    authHeader: str | None = None,
    sessionId: str | None = None,
) -> Response:
    """发一条 JSON-RPC 消息到 ``/mcp``。"""
    headers = dict(_JSON_HEADERS)
    if authHeader is not None:
        headers["Authorization"] = authHeader
    if sessionId is not None:
        headers["mcp-session-id"] = sessionId
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if rpcId is not None:
        payload["id"] = rpcId
    if params is not None:
        payload["params"] = params
    return await ac.post("/mcp", json=payload, headers=headers)


async def _initialize(
    ac: AsyncClient, *, authHeader: str | None = None
) -> tuple[Response, str | None]:
    response = await _rpc(
        ac,
        "initialize",
        rpcId=1,
        params=_INITIALIZE_PARAMS,
        authHeader=authHeader,
    )
    return response, response.headers.get("mcp-session-id")


def _assertForbidden(response: Response, expectedMessage: str) -> None:
    """403 + ErrorResponse 形状（而不是 400/200，也不是裸 text）。"""
    assert response.status_code == 403, (
        f"期望 403，实得 {response.status_code}: {response.text[:300]!r}"
    )
    assert "application/json" in response.headers.get("content-type", "")
    body = response.json()
    assert set(body) == {"success", "error", "detail", "details"}, body
    assert body["success"] is False
    assert body["error"] == expectedMessage


# ---------------------------------------------------------------------------
# 造数：带角色 + 活跃 session 的 DB 用户
# ---------------------------------------------------------------------------


async def _seedUserWithSession(
    session: AsyncSession, *, username: str
) -> tuple[User, str]:
    """造一个 enabled + 带角色 + 活跃 user_sessions 的用户，返回 (user, token)。"""
    user = User(
        username=username,
        display_name=username.title(),
        email=None,
        enabled=True,
        password_hash=bcrypt.hashpw(b"Abcd1234", bcrypt.gensalt(rounds=4)).decode(),
    )
    session.add(user)
    await session.flush()

    role = (
        await session.execute(select(Role).where(Role.code == "analyst"))
    ).scalar_one_or_none()
    if role is None:
        role = Role(code="analyst", name="analyst", description="test")
        session.add(role)
        await session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))

    token, jti, expiresAt = sign_jwt(user_id=user.id, username=username)
    session.add(
        UserSession(
            jti=jti,
            user_id=user.id,
            issued_at=datetime.now(timezone.utc),
            expires_at=expiresAt,
        )
    )
    await session.commit()
    await session.refresh(user)
    return user, token


# ---------------------------------------------------------------------------
# 1) 匿名：三步全被拦
# ---------------------------------------------------------------------------


async def test_anonymousInitialize_forbidden() -> None:
    """匿名 initialize 必须 403 —— 连 session 都不该发出去。"""
    async with _mcpHttpClient() as mcpHttp:
        response, sessionId = await _initialize(mcpHttp)
    _assertForbidden(response, MSG_AUTH_REQUIRED)
    assert sessionId is None


async def test_anonymousHandshakeAndToolsCall_forbidden() -> None:
    """完整握手三步（initialize / initialized / tools/call）匿名全部 403。

    修复前实测：initialize 200 + session id、initialized 202、tools/list 200
    返回 10 个工具、tools/call 抵达 handler。
    """
    async with _mcpHttpClient() as mcpHttp:
        initResponse, sessionId = await _initialize(mcpHttp)
        # 即使伪造一个 session id，notifications/initialized 也必须被拦
        initialized = await _rpc(
            mcpHttp,
            "notifications/initialized",
            authHeader=None,
            sessionId=sessionId or "forged-session",
        )
        toolsList = await _rpc(
            mcpHttp, "tools/list", rpcId=2, sessionId=sessionId or "forged-session"
        )
        toolsCall = await _rpc(
            mcpHttp,
            "tools/call",
            rpcId=3,
            params={"name": "wiki_status", "arguments": {}},
            sessionId=sessionId or "forged-session",
        )
    _assertForbidden(initResponse, MSG_AUTH_REQUIRED)
    _assertForbidden(initialized, MSG_AUTH_REQUIRED)
    _assertForbidden(toolsList, MSG_AUTH_REQUIRED)
    _assertForbidden(toolsCall, MSG_AUTH_REQUIRED)


async def test_nonBearerAuthorizationHeader_forbidden() -> None:
    """非 Bearer 前缀（如 Basic）同样 403，不得落到 stub 兜底。"""
    async with _mcpHttpClient() as mcpHttp:
        response, _ = await _initialize(mcpHttp, authHeader="Basic dXNlcjpwYXNz")
    _assertForbidden(response, MSG_AUTH_REQUIRED)


# ---------------------------------------------------------------------------
# 2) 无效 Bearer
# ---------------------------------------------------------------------------


async def test_invalidBearer_forbidden() -> None:
    """无效 Bearer（不是合法 JWT）→ 403 + MSG_TOKEN_INVALID。"""
    async with _mcpHttpClient() as mcpHttp:
        response, sessionId = await _initialize(
            mcpHttp, authHeader="Bearer not-a-real-jwt"
        )
    _assertForbidden(response, MSG_TOKEN_INVALID)
    assert sessionId is None


# ---------------------------------------------------------------------------
# 3) 有效 Bearer：核心验收（鉴权生效 + 身份链路打通）
# ---------------------------------------------------------------------------


async def test_validBearer_handshakeSucceedsWithTokenIdentity(
    dbSession: AsyncSession,
) -> None:
    """有效 Bearer → 握手成功，且 wiki_status 的 userId 来自 token。"""
    _, token = await _seedUserWithSession(dbSession, username="mcp_bearer_alice")
    authHeader = f"Bearer {token}"

    async with _mcpHttpClient() as mcpHttp:
        initResponse, sessionId = await _initialize(mcpHttp, authHeader=authHeader)
        assert initResponse.status_code == 200, initResponse.text[:300]
        assert sessionId, "initialize 未返回 mcp-session-id"

        initialized = await _rpc(
            mcpHttp,
            "notifications/initialized",
            authHeader=authHeader,
            sessionId=sessionId,
        )
        assert initialized.status_code in (200, 202), initialized.text[:300]

        toolsList = await _rpc(
            mcpHttp, "tools/list", rpcId=2, authHeader=authHeader, sessionId=sessionId
        )
        assert toolsList.status_code == 200, toolsList.text[:300]
        toolNames = {tool["name"] for tool in _parseRpc(toolsList)["result"]["tools"]}
        assert "wiki_status" in toolNames
        assert "wiki_update_dimension" in toolNames

        toolsCall = await _rpc(
            mcpHttp,
            "tools/call",
            rpcId=3,
            params={"name": "wiki_status", "arguments": {}},
            authHeader=authHeader,
            sessionId=sessionId,
        )
        assert toolsCall.status_code == 200, toolsCall.text[:300]
        result = _parseRpc(toolsCall)["result"]
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["userId"] == "mcp_bearer_alice", payload
