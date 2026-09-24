"""基于 slowapi 的限流基础设施。

限制维度：客户端地址（get_remote_address）或当前用户（feat-chat-concurrency-params
后由 system_config 行 RATE_LIMIT_KEY_STRATEGY 决定）。限额与开关从 Settings 读取；
limit 值为 callable，slowapi 按请求实时求值（便于测试压低限额触发 429，
生产环境限额始终跟随最新配置）。开关 `enabled` 在启动时绑定一次（需重启生效）。

为什么 headers_enabled=False：
slowapi 0.1.10 装饰器路径（async_wrapper）在 headers_enabled=True 时会对每个响应
调用 _inject_headers(kwargs.get("response"), ...)；对返回 Pydantic 模型的 FastAPI
端点（不声明 `response: Response` 参数）该值为 None，直接抛异常，即使请求未超限。
因此关闭内置头注入，改在统一 429 处理器中手动注入 Retry-After。

feat-chat-concurrency-params: RATE_LIMIT_KEY_STRATEGY
- 缓存于 module-level dict ``_rate_limit_strategy_cache``，request 路径只读 dict
  （同步、无 IO）。
- 写入点：
  1. main.py lifespan init 一次性读 system_config 写入；
  2. admin PUT /admin/system-config/{key=RATE_LIMIT_KEY_STRATEGY} 主动调
     ``set_rate_limit_strategy`` 立即覆盖；
- TTL 是防御性（30s），主要防止 lifespan init 异常时留下脏 cache；正常路径下
  cache 永远有效。
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

_VALID_RATE_LIMIT_STRATEGIES = frozenset({"ip", "user_id", "ip_user"})
_RATE_LIMIT_CACHE_TTL = 30.0
_rate_limit_strategy_cache: dict[str, Any] = {
    "strategy": "ip",
    "fetched_at": 0.0,
}


def rateLimitValue() -> str:
    """返回当前配置的限额字符串，如 "30/minute"（按请求实时求值）。"""
    settings = getSettings()
    return f"{settings.rateLimitRequests}/{settings.rateLimitWindow}"


def set_rate_limit_strategy(strategy: str) -> None:
    """lifespan init 或 admin PUT 钩子调用：直接写入策略缓存并刷新 fetched_at。

    非法 strategy → fallback "ip" 并 logger.warning（不抛错，避免误传值导致启停）。
    """
    sanitized = strategy if strategy in _VALID_RATE_LIMIT_STRATEGIES else "ip"
    if sanitized != strategy:
        logger.warning(
            "RATE_LIMIT_KEY_STRATEGY=%r 非法,fallback 'ip' (合法值: %s)",
            strategy,
            sorted(_VALID_RATE_LIMIT_STRATEGIES),
        )
    _rate_limit_strategy_cache["strategy"] = sanitized
    _rate_limit_strategy_cache["fetched_at"] = time.monotonic()


def invalidate_rate_limit_key_cache() -> None:
    """标记缓存过期；当前实现下请求路径不再主动 reload（admin PUT 已直接覆写）。

    保留入口便于未来切换为「自动 reload」语义时复用。
    """
    _rate_limit_strategy_cache["fetched_at"] = 0.0


def get_rate_limit_strategy() -> str:
    """返回当前限流策略：优先返回缓存值，TTL 过期视为 stale 但仍用旧值。

    lifespan init 失败 / 未调用 ``set_rate_limit_strategy`` 时返回默认 "ip"，
    永不抛错。
    """
    cached_at = _rate_limit_strategy_cache["fetched_at"]
    # fetched_at == 0.0 表示从未初始化过 → 默认 "ip"
    if cached_at == 0.0:
        return "ip"
    if time.monotonic() - cached_at >= _RATE_LIMIT_CACHE_TTL:
        # TTL 过期：退化到 stale 值，不在 request 路径同步读 DB（event loop 在跑，
        # asyncio.run 不可用）。lifespan init 失败可人工检查启动日志。
        logger.debug("rate_limit strategy cache stale, using last value")
    return _rate_limit_strategy_cache["strategy"]


def _extract_user_key(request: Request) -> str:
    """从 request.state.user 提取 userId；缺失时返回 'anonymous'。

    匿名用户走 user_id strategy 时共享一个 bucket —— 设计上等同于按
    'anonymous' 分桶，与 admin 通过 ip_user 策略组合使用可获得更细粒度。
    """
    user = getattr(request.state, "user", None)
    if user is None:
        return "anonymous"
    user_id = getattr(user, "userId", None)
    if user_id is None:
        return "anonymous"
    return str(user_id)


def _dynamic_key(request: Request) -> str:
    """slowapi key_func：根据 RATE_LIMIT_KEY_STRATEGY 选 key。

    - "ip"      → 客户端 IP（与原行为一致）
    - "user_id" → 当前用户 id（未登录降级 "anonymous"）
    - "ip_user" → f"{ip}:{userId}"（按 IP 与用户组合分桶）

    返回纯字符串，slowapi 直接拿去计 bucket。同步、无 IO。
    """
    strategy = get_rate_limit_strategy()
    ip = get_remote_address(request)
    if strategy == "ip":
        return ip
    user_key = _extract_user_key(request)
    if strategy == "user_id":
        return user_key
    return f"{ip}:{user_key}"


limiter = Limiter(
    key_func=_dynamic_key,
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
