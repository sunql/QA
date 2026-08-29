"""基于 slowapi 的限流基础设施。

限制维度：客户端地址（get_remote_address）。限额与开关从 Settings 读取；
limit 值为 callable，slowapi 按请求实时求值（便于测试压低限额触发 429，
生产环境限额始终跟随最新配置）。开关 `enabled` 在启动时绑定一次（需重启生效）。

为什么 headers_enabled=False：
slowapi 0.1.10 装饰器路径（async_wrapper）在 headers_enabled=True 时会对每个响应
调用 _inject_headers(kwargs.get("response"), ...)；对返回 Pydantic 模型的 FastAPI
端点（不声明 `response: Response` 参数）该值为 None，直接抛异常，即使请求未超限。
因此关闭内置头注入，改在统一 429 处理器中手动注入 Retry-After。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import getSettings
from app.domain.error_messages import MSG_RATE_LIMITED
from app.domain.schemas import ErrorResponse

logger = logging.getLogger(__name__)


def rateLimitValue() -> str:
    """返回当前配置的限额字符串，如 "30/minute"（按请求实时求值）。"""
    settings = getSettings()
    return f"{settings.rateLimitRequests}/{settings.rateLimitWindow}"


limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[rateLimitValue],
    enabled=getSettings().rateLimitEnabled,
    storage_uri="memory://",
    headers_enabled=False,
)


def _retryAfterSeconds(currentLimit: tuple[Any, list[str]]) -> int | None:
    """计算当前窗口重置剩余秒数，供 Retry-After 使用；失败时返回 None。

    依赖同步内存存储（storage_uri="memory://"）：get_window_stats 为纯内存查找，
    不阻塞事件循环。若未来改用 Redis 等网络存储，此函数须改为 async。
    """
    try:
        windowStats = limiter.limiter.get_window_stats(currentLimit[0], *currentLimit[1])
        resetIn = 1 + windowStats[0]
        return int(resetIn - time.time())
    except Exception:
        logger.warning("计算 Retry-After 失败", exc_info=True)
        return None


def rateLimitExceededHandler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """限流触发的统一响应：429 + 错误包络 + Retry-After。

    必须为同步函数：slowapi 中间件路径（非装饰路由）用 sync_check_limits，
    会丢弃 async handler 回退到 slowapi 默认响应（非标准包络）。
    """
    logger.warning("限流触发 path=%s detail=%s", request.url.path, exc)
    response = JSONResponse(
        status_code=429,
        content=ErrorResponse(error=MSG_RATE_LIMITED, detail=str(exc)).model_dump(by_alias=True),
    )
    # slowapi 在触发限流时（extension.py:530）先于异常设置 request.state.view_rate_limit
    currentLimit = getattr(request.state, "view_rate_limit", None)
    if currentLimit is not None:
        retryAfter = _retryAfterSeconds(currentLimit)
        if retryAfter is not None:
            response.headers["Retry-After"] = str(retryAfter)
    return response
