"""LLM 重试策略叶子模块单测（M4）。

`app/services/llm_retry_policy.py` 是**叶子模块**：纯判定 + tenacity 退避 + 用量携带，
不 import 任何 service（`chat_service` → `nl2sql_service`，反向 import 即成环）。

覆盖三件事：
1. 用量携带通道（`attachRetryGenTokens` / `retryGenTokens` / `...IfAbsent`）；
2. `consumedTokens` 的取值优先级 —— **Nl2SqlError.tokens 优先，其次读携带通道**，
   后者的意义是「多轮调用中途抛出的异常不再让已累加用量凭空消失」；
3. `completeWithTransientRetry` 的重试预算与过滤：仅首轮允许额外一次、
   仅可重试错误重试、任何逃逸异常都带上已累加用量。

无 IO：假 LLM 客户端 + `wait_none`（真实退避 1s~4s 会拖长单测）。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from tenacity import wait_none

from app.domain.exceptions import LlmClientError, Nl2SqlError
from app.services import llm_retry_policy as policy
from app.services.llm_retry_policy import (
    attachRetryGenTokens,
    attachRetryGenTokensIfAbsent,
    completeWithTransientRetry,
    consumedTokens,
    isRetryableLlmError,
    retryGenTokens,
)


class _Resp:
    def __init__(self, content: str, promptTokens: int = 10, completionTokens: int = 5) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = promptTokens
        self.completionTokens = completionTokens


class _ScriptedLlm:
    """按脚本逐次响应：元素是 _Resp 则返回，是异常则抛出。"""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    async def complete(self, messages: list, **kwargs) -> _Resp:
        self.calls.append({"messages": messages, **kwargs})
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, _Resp)
        return item


def _retryable() -> LlmClientError:
    """可重试错误：无 __cause__.status_code ⇒ 默认可重试（503/超时同族）。"""
    return LlmClientError("LLM 调用失败", provider="qwen", detail="503 Service Unavailable")


def _permanent() -> LlmClientError:
    """永久错误：__cause__ 携带 401 ⇒ 不可重试。"""
    cause = Exception("unauthorized")
    cause.status_code = 401  # type: ignore[attr-defined]
    exc = LlmClientError("LLM 调用失败", provider="qwen", detail="401 Unauthorized")
    exc.__cause__ = cause
    return exc


@pytest.fixture(autouse=True)
def noBackoffWait(monkeypatch: pytest.MonkeyPatch) -> None:
    """退避等待置零（`_retry` 在**调用时**读模块全局，故 monkeypatch 生效）。"""
    monkeypatch.setattr(policy, "RETRY_WAIT_EXPONENTIAL", wait_none())


async def _complete(fake: _ScriptedLlm, **overrides):
    kwargs = dict(
        messages=[SimpleNamespace(role="user", content="问题")],
        model="test-model",
        temperature=0.0,
        maxTokens=1024,
        allowRetry=True,
        consumedTokenCounts=(0, 0),
    )
    kwargs.update(overrides)
    return await completeWithTransientRetry(fake, **kwargs)


class TestTokenCarryingChannel:
    """用量携带通道：挂在异常上的私有属性。"""

    def test_unattached_returns_zero(self) -> None:
        assert retryGenTokens(RuntimeError("boom")) == (0, 0)

    def test_attach_then_read_round_trip(self) -> None:
        exc = RuntimeError("boom")
        attachRetryGenTokens(exc, (12, 34))
        assert retryGenTokens(exc) == (12, 34)

    def test_attach_if_absent_does_not_clobber(self) -> None:
        """已携带用量时不被覆盖（下层先落的值更贴近真实消耗）。"""
        exc = RuntimeError("boom")
        attachRetryGenTokens(exc, (3000, 500))
        attachRetryGenTokensIfAbsent(exc, (0, 0))
        assert retryGenTokens(exc) == (3000, 500)


class TestConsumedTokens:
    """consumedTokens 的取值优先级。"""

    def test_nl2sql_error_tokens_win(self) -> None:
        """Nl2SqlError.tokens 是终态累计值，优先于携带通道。"""
        exc = Nl2SqlError("计划无效", tokens=(777, 88))
        attachRetryGenTokens(exc, (1, 2))
        assert consumedTokens(exc) == (777, 88)

    def test_nl2sql_error_without_tokens_falls_back_to_channel(self) -> None:
        exc = Nl2SqlError("计划无效")
        attachRetryGenTokens(exc, (3000, 500))
        assert consumedTokens(exc) == (3000, 500)

    def test_plain_llm_error_reads_carried_tokens(self) -> None:
        """M4 核心：多轮调用中途抛出的 LlmClientError 也带着已累加用量。"""
        exc = _retryable()
        attachRetryGenTokens(exc, (3000, 500))
        assert consumedTokens(exc) == (3000, 500)

    def test_unattached_llm_error_is_zero(self) -> None:
        assert consumedTokens(_retryable()) == (0, 0)

    def test_unattached_generic_exception_is_zero(self) -> None:
        assert consumedTokens(RuntimeError("boom")) == (0, 0)

    def test_llm_error_with_builtin_tokens_wins_over_channel(self) -> None:
        """失败路径 LLM 用量（§15 末尾第 3 项）：`openai_client` 流式 / post-response
        失败时把已测得的部分用量挂到 `LlmClientError.tokens`，优先级**高于**携带通道
        —— 真实消耗的数字不会因为通道晚挂或重写而被覆盖。
        """
        exc = LlmClientError("流式中断", provider="qwen", detail="连接重置", tokens=(1500, 200))
        # 即使通道也挂了一个值，builtin tokens 优先
        attachRetryGenTokens(exc, (9999, 9999))
        assert consumedTokens(exc) == (1500, 200)

    def test_llm_error_with_builtin_tokens_no_channel_fallback(self) -> None:
        """builtin tokens 存在时，不回退到通道（即使通道有值）。"""
        exc = LlmClientError("LLM 调用失败", provider="qwen", detail="503", tokens=(42, 7))
        assert consumedTokens(exc) == (42, 7)

    def test_llm_error_without_builtin_tokens_falls_back_to_channel(self) -> None:
        """builtin tokens 为 None 时，按既有契约回退到携带通道（M4 路径）。"""
        exc = LlmClientError("LLM 调用失败", provider="qwen", detail="超时")
        # tokens 缺省为 None
        assert exc.tokens is None
        attachRetryGenTokens(exc, (300, 50))
        assert consumedTokens(exc) == (300, 50)


class TestCompleteWithTransientRetry:
    """同模型瞬态重试：预算、过滤、用量携带。"""

    async def test_success_does_not_retry(self) -> None:
        fake = _ScriptedLlm([_Resp("ok")])
        result = await _complete(fake, allowRetry=True, consumedTokenCounts=(0, 0))
        assert result.content == "ok"
        assert len(fake.calls) == 1

    async def test_retryable_failure_retries_once_on_first_attempt(self) -> None:
        """首轮瞬态故障 → 同一模型上再试一次，第 2 次成功。"""
        fake = _ScriptedLlm([_retryable(), _Resp("recovered")])
        result = await _complete(fake, allowRetry=True, consumedTokenCounts=(0, 0))
        assert result.content == "recovered"
        assert len(fake.calls) == 2
        # 同一模型、同一 messages（不换模型、不改 prompt）
        assert fake.calls[0]["model"] == fake.calls[1]["model"]
        assert fake.calls[0]["messages"] == fake.calls[1]["messages"]

    async def test_permanent_failure_is_not_retried(self) -> None:
        """401 永久错误：即使允许重试也立即上抛（不浪费退避窗口）。"""
        fake = _ScriptedLlm([_permanent()])
        with pytest.raises(LlmClientError):
            await _complete(fake, allowRetry=True, consumedTokenCounts=(0, 0))
        assert len(fake.calls) == 1

    async def test_retry_disallowed_when_allowRetry_is_false(self) -> None:
        """非首轮（allowRetry=False）：可重试错误也不重试 —— 预算闸门。"""
        fake = _ScriptedLlm([_retryable()])
        with pytest.raises(LlmClientError):
            await _complete(fake, allowRetry=False, consumedTokenCounts=(0, 0))
        assert len(fake.calls) == 1

    async def test_escaping_exception_carries_accumulated_tokens(self) -> None:
        """逃逸异常必须带上已累加用量：第 1 轮成功 3000/500 + 第 2 轮抛出 ⇒ 不是 (0,0)。"""
        fake = _ScriptedLlm([_retryable(), _retryable()])
        with pytest.raises(LlmClientError) as err:
            await _complete(fake, allowRetry=True, consumedTokenCounts=(3000, 500))
        assert consumedTokens(err.value) == (3000, 500)

    async def test_escaping_exception_keeps_lower_layer_tokens(self) -> None:
        """下层已携带用量时不被本层的累计值覆盖（重试后仍是同一异常）。"""
        exc = _retryable()
        attachRetryGenTokens(exc, (11, 22))
        fake = _ScriptedLlm([exc, exc])
        with pytest.raises(LlmClientError) as err:
            await _complete(fake, allowRetry=True, consumedTokenCounts=(3000, 500))
        assert consumedTokens(err.value) == (11, 22)

    async def test_retry_is_logged(self, caplog) -> None:
        """重试必须留痕（否则「为什么多花了一次钱」在日志里无从查证）。"""
        fake = _ScriptedLlm([_retryable(), _Resp("ok")])
        with caplog.at_level(logging.WARNING, logger="app.services.llm_retry_policy"):
            await _complete(fake, allowRetry=True, consumedTokenCounts=(0, 0))
        assert any("重试" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]

    async def test_passes_through_model_and_generation_params(self) -> None:
        """透传契约：model/temperature/maxTokens 原样交给 LLM 客户端。"""
        fake = _ScriptedLlm([_Resp("ok")])
        await _complete(fake, temperature=0.3, maxTokens=2048)
        assert fake.calls[0]["model"] == "test-model"
        assert fake.calls[0]["temperature"] == 0.3
        assert fake.calls[0]["maxTokens"] == 2048


class TestRetryPolicyContract:
    """共享退避策略的常量契约。"""

    def test_max_attempts_is_two(self) -> None:
        """一次额外尝试（总 2 次）——多一轮就是第二条重试策略的开始。"""
        assert policy.RETRY_MAX_ATTEMPTS == 2

    def test_retryability_matches_chat_service_semantics(self) -> None:
        """判定函数与 chat_service 既有口径同源（默认可重试 + 4xx 精确拦截）。"""
        assert isRetryableLlmError(_retryable()) is True
        assert isRetryableLlmError(_permanent()) is False
        assert isRetryableLlmError(Nl2SqlError("计划无效")) is True
        assert isRetryableLlmError(ValueError("x")) is False
