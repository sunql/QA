"""MCP HTTP 端点（``/mcp``）的 Bearer 鉴权中间件 + 身份 ContextVar。

## 为什么不是 FastAPI 依赖

``/mcp`` 由 ``main.py`` 用 Starlette ``Mount("")`` 挂到 ASGI 树末端 ——
``Mount`` 不是 ``APIRoute``，**完全绕过 FastAPI 的依赖注入**，所以 router 级
``dependencies=[Depends(getCurrentUser)]`` 对它不生效。实测（2026-09-30 安全批次，
``AUTH_MODE=real`` + 无任何鉴权头）：匿名可 ``initialize`` 拿到 session id、
``tools/list`` 拿到全部 10 个工具、``tools/call`` 抵达 handler —— 其中含 2 个
**写工具**（``wiki_update_dimension`` / ``wiki_update_community_topic``）。

这里用纯 ASGI 中间件在进子 app **之前**完成鉴权，并把身份写进 ContextVar
（``mcpCurrentUser``）供 MCP tool 读取，让 HTTP 路径与 FastAPI 路由走**同一条**
身份链路（``authenticateBearer``），不另造一套。

## 为什么用 ContextVar 而不是 request state

fastmcp 的 tool 函数签名里没有 Starlette ``Request``，拿不到 ``request.state``；
ContextVar 由 ASGI 中间件在调用子 app 前 ``set``、返回后 ``reset``，正是
「随请求生命周期存在」的语义。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar, Token
from typing import Any

from fastapi.responses import JSONResponse

from app.dependencies import CurrentUser, authenticateBearer
from app.domain.error_messages import MSG_AUTH_REQUIRED
from app.domain.exceptions import PermissionDeniedError
from app.domain.schemas import ErrorResponse
from app.infrastructure.database import getSessionFactory

logger = logging.getLogger(__name__)

_ASGIScope = MutableMapping[str, Any]
_ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
_ASGISend = Callable[[dict[str, Any]], Awaitable[None]]

_BEARER_PREFIX = "bearer "
_FORBIDDEN_STATUS = 403

mcpCurrentUser: ContextVar[CurrentUser | None] = ContextVar(
    "mcpCurrentUser", default=None
)
"""当前 MCP 请求的用户身份；仅由 :class:`McpAuthMiddleware` 写入。"""


def getMcpCurrentUser() -> CurrentUser:
    """读当前 MCP 请求的用户身份。

    **fail-closed**：未设置时抛 ``RuntimeError``，绝不回退到 stub / anonymous
    身份 —— 静默降级会把写工具以匿名 admin 暴露出去，正是本模块要消灭的洞。
    """
    current = mcpCurrentUser.get()
    if current is None:
        raise RuntimeError(
            "MCP 当前用户未设置：HTTP 请求必须经 McpAuthMiddleware 鉴权后才会"
            "写入身份；若这是 stdio 模式，请确认进程入口已注入身份。"
        )
    return current


def _headerValue(scope: _ASGIScope, name: bytes) -> str | None:
    """从 ASGI ``scope["headers"]`` 取头值（键名小写、值为 bytes）。"""
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def _sendForbidden(
    message: str, scope: _ASGIScope, receive: _ASGIReceive, send: _ASGISend
) -> None:
    """统一 403 响应：``ErrorResponse`` 形状（与 FastAPI 异常处理器同契约）。

    直接 ``await response(...)`` 而不是 ``return`` —— 纯 ASGI 中间件没有返回值
    通道，只能自己把响应写进 ``send``。
    """
    response = JSONResponse(
        status_code=_FORBIDDEN_STATUS,
        content=ErrorResponse(error=message).model_dump(by_alias=True),
    )
    await response(scope, receive, send)


class McpAuthMiddleware:
    """纯 ASGI 中间件：``/mcp`` 的 Bearer 鉴权闸门。

    只处理 ``http`` scope；其余（``lifespan`` 等）直接透传 —— 否则 fastmcp
    streamable-http 的 session manager 初始化会被拦掉。``OPTIONS`` 也透传
    （CORS 预检不带凭据）。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self, scope: _ASGIScope, receive: _ASGIReceive, send: _ASGISend
    ) -> None:
        if scope.get("type") != "http" or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        authorization = _headerValue(scope, b"authorization")
        if not authorization or not authorization.lower().startswith(_BEARER_PREFIX):
            await _sendForbidden(MSG_AUTH_REQUIRED, scope, receive, send)
            return

        try:
            user = await self._authenticate(authorization)
        except PermissionDeniedError as exc:
            logger.warning(
                "MCP 鉴权失败: %s (path=%s)", exc.message, scope.get("path")
            )
            await _sendForbidden(exc.message, scope, receive, send)
            return

        token: Token[CurrentUser | None] = mcpCurrentUser.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            mcpCurrentUser.reset(token)

    @staticmethod
    async def _authenticate(authorization: str) -> CurrentUser:
        """走与 FastAPI 路由同一条身份链路（``authenticateBearer``）。"""
        async with getSessionFactory()() as session:
            return await authenticateBearer(authorization, None, session)
