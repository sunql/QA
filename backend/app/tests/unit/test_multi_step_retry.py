import asyncio

import httpx
import pytest

from app.domain.exceptions import LLMUnavailableError, LlmClientError, Nl2SqlError
from app.services.multi_step_retry import (
    ERROR_KIND_PERMANENT,
    ERROR_KIND_TRANSIENT,
    MAX_ATTEMPTS,
    classifyStepError,
    runWithTransientRetry,
)


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


def _wrap(inner: Exception, outer: Exception) -> Exception:
    """返回以 ``inner`` 为 __cause__ 的 ``outer``（等价于 `raise outer from inner`）。"""
    try:
        raise inner
    except Exception as exc:  # noqa: BLE001 - 仅用于构造异常链
        try:
            raise outer from exc
        except Exception as wrapped:  # noqa: BLE001 - 同上
            return wrapped


@pytest.mark.parametrize(
    "exc, expected",
    [
        # 裸类型（本模块自身可能直接看到）
        (httpx.ConnectError("refused"), ERROR_KIND_TRANSIENT),
        (httpx.ReadTimeout("slow"), ERROR_KIND_TRANSIENT),
        (asyncio.TimeoutError(), ERROR_KIND_TRANSIENT),
        (_StatusError(429), ERROR_KIND_TRANSIENT),
        (_StatusError(502), ERROR_KIND_TRANSIENT),
        (_StatusError(503), ERROR_KIND_TRANSIENT),
        (_StatusError(400), ERROR_KIND_PERMANENT),
        (Nl2SqlError("plan 校验失败"), ERROR_KIND_PERMANENT),
        (ValueError("bad input"), ERROR_KIND_PERMANENT),
        # 配置错误（未配置 LLM / 无可用 key）：重试不会自愈 → 永久
        (LLMUnavailableError("no client"), ERROR_KIND_PERMANENT),
        # 真实链路形态：provider 失败被 LlmClientError 包住（openai_client 的 `from exc`）
        (
            _wrap(httpx.ConnectError("refused"), LlmClientError("call failed", provider="openai")),
            ERROR_KIND_TRANSIENT,
        ),
        (
            _wrap(ConnectionResetError("reset by peer"), LlmClientError("call failed", provider="openai")),
            ERROR_KIND_TRANSIENT,
        ),
        (
            _wrap(_StatusError(503), LlmClientError("call failed", provider="openai")),
            ERROR_KIND_TRANSIENT,
        ),
        (
            _wrap(_StatusError(401), LlmClientError("call failed", provider="openai")),
            ERROR_KIND_PERMANENT,
        ),
        # 状态码埋在第二层 __cause__ 之下：只走一层会漏判
        (
            _wrap(_wrap(_StatusError(503), Exception("middle")), LlmClientError("call failed")),
            ERROR_KIND_TRANSIENT,
        ),
        # 无 cause 的 LlmClientError（缺 endpoint / key 等配置错）→ 永久
        (LlmClientError("missing endpoint", provider="azure"), ERROR_KIND_PERMANENT),
    ],
)
def testClassifyStepError(exc, expected):
    assert classifyStepError(exc) == expected


@pytest.mark.asyncio
async def testRetrySucceedsOnSecondAttempt():
    # Arrange
    waits: list[float] = []

    async def fakeSleep(seconds: float) -> None:
        waits.append(seconds)

    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("refused")
        return "ok"

    # Act
    result, attempts = await runWithTransientRetry(call, sleep=fakeSleep)

    # Assert
    assert result == "ok"
    assert attempts == 2
    assert waits == [1]


@pytest.mark.asyncio
async def testPermanentErrorDoesNotRetry():
    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        raise Nl2SqlError("permanent")

    with pytest.raises(Nl2SqlError):
        await runWithTransientRetry(call, sleep=lambda s: asyncio.sleep(0))
    assert calls["n"] == 1


@pytest.mark.asyncio
async def testTransientErrorExhaustsAttemptsThenRaises():
    waits: list[float] = []

    async def fakeSleep(seconds: float) -> None:
        waits.append(seconds)

    async def call():
        raise httpx.ConnectError("always down")

    with pytest.raises(httpx.ConnectError):
        await runWithTransientRetry(call, sleep=fakeSleep)
    assert waits == [1, 2]  # 3 次尝试之间只等 2 次


@pytest.mark.asyncio
async def testOnErrorHookSeesEachTransientFailure():
    seen: list[str] = []

    async def call():
        raise httpx.ConnectError("down")

    async def onError(exc: Exception, attempt: int) -> None:
        seen.append(f"{type(exc).__name__}:{attempt}")

    with pytest.raises(httpx.ConnectError):
        await runWithTransientRetry(call, sleep=lambda s: asyncio.sleep(0), onError=onError)
    assert seen == ["ConnectError:1", "ConnectError:2", "ConnectError:3"]


def testMaxAttemptsMatchesWaitTable():
    assert MAX_ATTEMPTS == 3
