"""多步执行的服务端错误分类与瞬态重试（spec §6）。

与 app.services.llm_retry_policy 的差异：那边把 Nl2SqlError 一律视为可重试，
多步场景下 plan/SQL 校验失败重试无意义且白烧 token，故这里独立分类。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

from app.domain.exceptions import LLMUnavailableError, Nl2SqlError

logger = logging.getLogger(__name__)

ERROR_KIND_TRANSIENT = "transient"
ERROR_KIND_PERMANENT = "permanent"

#: 第 N 次尝试失败后等 TRANSIENT_WAITS[N - 1] 秒。3 次尝试之间只等 2 次
#: （spec §6.2：1s → 2s → 第 3 次失败即转 manual），故只有 2 个元素。
TRANSIENT_WAITS: tuple[int, ...] = (1, 2)
#: 尝试次数上限。**独立于 TRANSIENT_WAITS 的长度**——写成 len(TRANSIENT_WAITS)
#: 会让「等待次数」与「尝试次数」互相绑死（长度 2 会被误读成最多试 2 次），
#: 正是本模块要避免的坑。
MAX_ATTEMPTS: int = 3

_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

T = TypeVar("T")


def classifyStepError(exc: BaseException) -> str:
    """把异常分成 transient（可自动重试）或 permanent（转人工）。"""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException)):
        return ERROR_KIND_TRANSIENT
    if isinstance(exc, asyncio.TimeoutError):
        return ERROR_KIND_TRANSIENT
    if isinstance(exc, LLMUnavailableError):
        return ERROR_KIND_TRANSIENT
    if isinstance(exc, Nl2SqlError):
        return ERROR_KIND_PERMANENT

    status = _statusCode(exc)
    if status is not None:
        return ERROR_KIND_TRANSIENT if status in _TRANSIENT_STATUS else ERROR_KIND_PERMANENT
    return ERROR_KIND_PERMANENT


def _statusCode(exc: BaseException) -> int | None:
    for candidate in (exc, getattr(exc, "__cause__", None)):
        code = getattr(candidate, "status_code", None)
        if isinstance(code, int):
            return code
    return None


async def runWithTransientRetry(
    call: Callable[[], Awaitable[T]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    onError: Callable[[Exception, int], Awaitable[None]] | None = None,
) -> tuple[T, int]:
    """执行 call，瞬态错误按 TRANSIENT_WAITS 退避重试。

    返回 (结果, 实际尝试次数)。永久错误立即抛出；瞬态错误耗尽后抛出最后一次异常。

    只捕获 Exception：CancelledError 继承自 BaseException，必须让它透传，
    否则客户端断连时重试会把取消信号吞掉。
    """
    lastError: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await call(), attempt
        except Exception as exc:  # noqa: BLE001 - 需按分类决定是否重试
            kind = classifyStepError(exc)
            if onError is not None:
                await onError(exc, attempt)
            if kind != ERROR_KIND_TRANSIENT:
                raise
            lastError = exc
            # 最后一次尝试失败后不再等待，直接转人工（spec §6.2）
            if attempt < MAX_ATTEMPTS:
                logger.warning("multi-step 第 %d 次尝试瞬态失败，%.0fs 后重试：%s",
                               attempt, TRANSIENT_WAITS[attempt - 1], exc)
                await sleep(TRANSIENT_WAITS[attempt - 1])
    assert lastError is not None
    raise lastError
