import asyncio

import httpx
import pytest

from app.domain.exceptions import LLMUnavailableError, Nl2SqlError
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


@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.ConnectError("refused"), ERROR_KIND_TRANSIENT),
        (httpx.ReadTimeout("slow"), ERROR_KIND_TRANSIENT),
        (asyncio.TimeoutError(), ERROR_KIND_TRANSIENT),
        (LLMUnavailableError("no client"), ERROR_KIND_TRANSIENT),
        (_StatusError(429), ERROR_KIND_TRANSIENT),
        (_StatusError(502), ERROR_KIND_TRANSIENT),
        (_StatusError(503), ERROR_KIND_TRANSIENT),
        (_StatusError(400), ERROR_KIND_PERMANENT),
        (Nl2SqlError("plan 校验失败"), ERROR_KIND_PERMANENT),
        (ValueError("bad input"), ERROR_KIND_PERMANENT),
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
