"""多步执行的服务端错误分类与瞬态重试（spec §6）。

与 app.services.llm_retry_policy 的差异：那边把 Nl2SqlError 一律视为可重试，
多步场景下 plan/SQL 校验失败重试无意义且白烧 token，故这里独立分类。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
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

#: 判定「瞬态」的 http 状态码：429 限流 + 5xx 服务端错误。
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

#: 直接判瞬态的异常类型。`httpx.TransportError` 已覆盖 ConnectError /
#: TimeoutException / ReadError / RemoteProtocolError / PoolTimeout 等全部传输层
#: 失败（它们都没有 http 状态码，重试是正确处置）；内建 `ConnectionError`
#: 覆盖 socket 层的连接重置/拒绝。
_TRANSIENT_TYPES: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    ConnectionError,
    asyncio.TimeoutError,
)

T = TypeVar("T")


def classifyStepError(exc: BaseException) -> str:
    """把异常分成 transient（可自动重试）或 permanent（转人工）。

    **沿整条 `__cause__`/`__context__` 链判定**，不只看最外层：本仓所有 provider
    失败都被 `openai_client` 以 `LlmClientError(...) from exc` 包住，故 spec §6.1
    列的 httpx 类型在分类点永远不会裸着到达；只看一层会把「provider 不可达 /
    超时」误判为永久，而那正是本功能要救的故障类别。HTTP 状态码同理，也可能
    埋在多层之下。

    与 `llm_retry_policy.isRetryableLlmError` 的差异：那一条对 `Nl2SqlError` 一律
    返回可重试，而多步场景下 plan 校验失败属永久错误（重试白烧 token），故不复用。
    """
    # 按类型优先判永久：语义固定，不受包装层数影响。
    if isinstance(exc, (Nl2SqlError, LLMUnavailableError)):
        # Nl2SqlError：NL2SQL 自带重试/降级，plan 校验失败重试无意义。
        # LLMUnavailableError：「未配置 LLM / 无可用 key」是配置错，重试不自愈。
        return ERROR_KIND_PERMANENT

    for link in _causeChain(exc):
        if isinstance(link, _TRANSIENT_TYPES):
            return ERROR_KIND_TRANSIENT
        status = _statusCodeOf(link)
        if status is not None:
            return ERROR_KIND_TRANSIENT if status in _TRANSIENT_STATUS else ERROR_KIND_PERMANENT
    return ERROR_KIND_PERMANENT


def _causeChain(exc: BaseException) -> Iterator[BaseException]:
    """异常自身 + 整条 `__cause__`／`__context__` 链（按 id 去环）。"""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _statusCodeOf(exc: BaseException) -> int | None:
    """取链接上的 http 状态码（`status` 是 aiohttp 一类客户端的字段名）。"""
    for attr in ("status_code", "status"):
        code = getattr(exc, attr, None)
        if isinstance(code, int) and not isinstance(code, bool):
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
