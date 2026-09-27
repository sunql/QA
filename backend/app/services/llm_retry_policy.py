"""LLM 调用的重试策略（叶子模块：判定 + 退避 + 用量携带，无 IO、无 service 依赖）。

抽出的理由（M4）：同一套「什么错误值得重试 / 退避多久」此前只活在 `chat_service`
里，`nl2sql_service` 想在同模型上加一次瞬态重试就只能另写一条策略 —— 一个库两条
重试策略正是重试逻辑腐化的开始。这里做成唯一实现，两个消费方共用：

- `chat_service._callWithFallback`：换模型前的**降级**重试（`callWithRetryBackoff`）；
- `nl2sql_service.generateQueryPlan` / `generateSql`：同模型上的**一次**瞬态重试
  （`completeWithTransientRetry`）。

⚠️ 本模块**不得** import `app.services.chat_service`（或 `nl2sql_service`）：
`chat_service` → `nl2sql_service`，反向 import 即成环。只允许 import domain 层。

用量携带通道（`_RETRY_GEN_TOKENS_ATTR`）也在这里 —— 它是「异常上带用量」的 SSOT，
`chat_service` 反向 import 后按私有名重新导出，既有消费点（`_accountRetryGenUsage`、
`test_chat_step_error_text.py`）无需改动。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.domain.exceptions import LlmClientError, Nl2SqlError

logger = logging.getLogger(__name__)

# 重试预算 SSOT：一次额外尝试（总 2 次）。退避同为 SSOT，两个入口共用同一对象。
RETRY_MAX_ATTEMPTS = 2
RETRY_WAIT_MIN_SECONDS = 1
RETRY_WAIT_MAX_SECONDS = 4
RETRY_WAIT_EXPONENTIAL = wait_exponential(
    multiplier=1, min=RETRY_WAIT_MIN_SECONDS, max=RETRY_WAIT_MAX_SECONDS
)

# 挂在异常上的私有属性名：携带「已消耗但尚未落账」的 token（见 attachRetryGenTokens）
_RETRY_GEN_TOKENS_ATTR = "_retryGenTokens"


def attachRetryGenTokens(exc: Exception, tokens: tuple[int, int]) -> None:
    """把「重试生成已花掉」的 token 挂到上抛的执行异常上。

    两个分支都要挂：重试生成成功但重试执行又失败（用 `SqlResult` 带的用量），以及
    重试生成**自己**失败（用 `consumedTokens(genErr)` 取 `Nl2SqlError.tokens`）——
    两种情形都真的调了 LLM，都必须在失败路径上留账。

    只挂私有属性，**不改异常类型与消息**（API 层按类型映射 HTTP 状态，改类型会连带
    改变对外错误契约），调用方用 `retryGenTokens` 取回记账。与 `consumedTokens`
    对 `Nl2SqlError.tokens` 的处理同一思路。
    """
    setattr(exc, _RETRY_GEN_TOKENS_ATTR, tokens)


def attachRetryGenTokensIfAbsent(exc: Exception, tokens: tuple[int, int]) -> None:
    """仅在异常尚未携带用量时挂上（M4）。

    「先挂的层更贴近真实消耗」：`nl2sql_service` 的生成循环只补挂自己累计的部分，
    不覆盖下层（如某个已自带用量的客户端异常）已经交代过的数字。
    """
    if retryGenTokens(exc) == (0, 0):
        attachRetryGenTokens(exc, tokens)


def retryGenTokens(exc: Exception) -> tuple[int, int]:
    """提取异常携带的「重试生成」token；无法计量时返回 (0, 0)。"""
    return getattr(exc, _RETRY_GEN_TOKENS_ATTR, (0, 0)) or (0, 0)


def consumedTokens(exc: Exception) -> tuple[int, int]:
    """提取异常携带的已消耗 token；无法计量时返回 (0, 0)。

    取值优先级：`Nl2SqlError.tokens`（终态累计值）> 携带通道（`retryGenTokens`）。

    第二档是 M4 补的：多轮调用**中途**抛出的异常（如第 2 轮 `complete()` 抛
    `LlmClientError`）此前不带任何用量，前几轮已累加的 token 就此静默消失。
    现在 `nl2sql_service` 会把累计值挂上去，本函数读得到 ⇒ 降级审计行与总消耗
    都能如实计量（核心约束 #3 —— 失败路径也是计量路径）。
    """
    if isinstance(exc, Nl2SqlError) and exc.tokens is not None:
        return exc.tokens
    return retryGenTokens(exc)


# 默认 retryable 以兼容旧测试（注入的合成 LlmClientError 无 status_code），
# 仅当能**确定性判定**为 4xx 永久错误（401/403/400）时才不走 fallback。
# 这样不会改变既有 fallback 行为，只在「明确不该重试」时拦截。
_RETRYABLE_LLM_ERROR_HINTS = (
    "429",
    "rate limit",
    "rate_limit",
    "503",
    "service unavailable",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
)


def isRetryableLlmError(exc: Exception) -> bool:
    """判断 LLM 异常是否值得 fallback + 重试。

    默认 True（兼容旧 fallback 行为：任何 LlmClientError 都触发降级）。
    仅当 ``__cause__`` 携带**确定性** 4xx status_code（401/403/400 等永久
    错误）时才返回 False，跳过 fallback 与重试。Nl2SqlError 一律视为可重试。

    「默认 True」是保守选择：宁可让 fallback 在某些边缘情况下多跑一次（fallback
    模型自身仍会被自己的 _callWithFallback 拦截），也不要因为误判把真正可恢复
    的请求直接抛掉。生产中真实 LLM 异常一定有 __cause__ 的 status_code 字段
    （OpenAI SDK / aiohttp 都带），所以 4xx 仍会被精确拦截。
    """
    if isinstance(exc, Nl2SqlError):
        return True
    if not isinstance(exc, LlmClientError):
        return False
    # 唯一确定的「不可重试」信号：__cause__ 携带 4xx status_code（排除 429）
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        status = getattr(cause, "status_code", None) or getattr(cause, "status", None)
        if status is not None:
            try:
                code = int(status)
            except (TypeError, ValueError):
                code = 0
            # 429 是 rate limit（4xx 但属临时故障），应走 fallback
            if code == 429:
                return True
            # 其他 4xx（400/401/403）→ 永久错误，不重试
            if 400 <= code < 500:
                return False
            # 5xx（含 503）→ 临时故障，可重试
            if 500 <= code < 600:
                return True
    # 默认 retryable（兼容既有行为 + 测试场景）
    return True


async def _retryWithBackoff(
    caller: Callable[[Any], Awaitable[Any]],
    fallback: Any,
    *,
    shouldRetry: Callable[[BaseException], bool],
) -> Any:
    """tenacity 包装：最多 `RETRY_MAX_ATTEMPTS` 次尝试，指数退避 1s~4s。

    两个入口共用（策略与退避只此一处）：
    - `callWithRetryBackoff`：`shouldRetry` = 任何 LlmClientError（既有契约）；
    - `completeWithTransientRetry`：`shouldRetry` = `isRetryableLlmError`。

    ``reraise=True`` 让最终异常保持原类型，方便上层 catch。判定为「不重试」时
    首个异常即原样上抛 —— 不会白等一个退避窗口。
    """
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=RETRY_WAIT_EXPONENTIAL,
        retry=retry_if_exception(shouldRetry),
        reraise=True,
    ):
        with attempt:
            return await caller(fallback)
    # 不可达：AsyncRetrying 总会 raise 或 yield 一次。
    # noqa: B008 —— 不是函数调用，是给 mypy 的 NoReturn marker，
    # 此分支实际不会执行（tenacity 契约：始终 raise 或 yield）。
    raise RuntimeError("unreachable")  # noqa: B008


async def callWithRetryBackoff(
    caller: Callable[[Any], Awaitable[Any]], fallback: Any
) -> Any:
    """tenacity 包装：最多 2 次尝试（1+1 重试），指数退避 1s~4s。

    仅对 LlmClientError 重试——其它异常（编程错误、配置错误）立即抛出。
    **不做可重试性判定**：永久错误（4xx）由调用方在进入本函数前用
    `isRetryableLlmError` 过滤（见 `chat_service._callWithFallback`）——
    这条契约有测试钉死（`test_fallback_backoff.py::TestCallWithRetryBackoff`）。
    """
    return await _retryWithBackoff(
        caller,
        fallback,
        shouldRetry=lambda exc: isinstance(exc, LlmClientError),
    )


async def completeWithTransientRetry(
    llmClient: Any,
    *,
    messages: list[Any],
    model: str,
    temperature: float,
    maxTokens: int,
    allowRetry: bool,
    consumedTokenCounts: tuple[int, int],
) -> Any:
    """调一次 LLM；可重试的瞬态故障时在**同一模型**上多试一次（M4）。

    `allowRetry` 由调用方按预算给出（`nl2sql_service` 是 `attempt == 0`）：
    只有最便宜的首轮允许额外一次，最坏调用数 = `(maxRetries + 1) + 1`，
    不会每轮翻倍 —— 429 风暴下正撞节流窗口的写法不是重试而是自伤。

    `consumedTokenCounts` 是调用方**此前各轮已累加**的用量：任何逃逸的异常
    （含重试后仍失败的那次）都会带上它，交给上层降级审计行与总消耗计量。
    失败尝试自身的 token 在源头就不存在（`LlmClientError` 不带用量、流式只在
    终块给），故不做假账，只保证「已测得的」不丢。
    """
    async def caller(_model: Any) -> Any:
        return await llmClient.complete(
            messages=messages,
            model=model,
            temperature=temperature,
            maxTokens=maxTokens,
        )

    try:
        if not allowRetry:
            return await caller(model)
        return await _retryWithBackoff(
            caller, model, shouldRetry=_retryableAndLog(model)
        )
    except Exception as exc:
        attachRetryGenTokensIfAbsent(exc, consumedTokenCounts)
        raise


def _retryableAndLog(model: str) -> Callable[[BaseException], bool]:
    """可重试性判定 + 留痕：**真要重试时**记一条 warning。

    放在判定里而不是重试后：重试成功与否都必须留痕，否则「为什么多花了一次钱、
    多花了一次延迟」在日志里无从查证。
    """
    def shouldRetry(exc: BaseException) -> bool:
        retryable = isinstance(exc, Exception) and isRetryableLlmError(exc)
        if retryable:
            logger.warning("LLM 调用瞬态失败，同模型重试一次（model=%s）: %s", model, exc)
        return retryable

    return shouldRetry
