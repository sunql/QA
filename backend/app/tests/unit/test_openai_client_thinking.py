"""OpenAiClient 的 disable_thinking → extra_body 透传单测。

背景（2026-10-03 真机）：推理模型（MiniMax-M3）把 91.3% 的 token 花在
<think> 思维链上（实测 think 5829 / JSON 558 tokens），挤爆计划阶段
2048 的预算 ⇒ 回复被截断在 JSON 之前 ⇒ chat 报 HTTP 400。

关闭 thinking 后实测同 prompt 只需 509~907 tokens（省 86-92%）且解析全部成功。

本模块逐条锁住四个易错点：
1. **必须走 extra_body**：openai SDK 2.53.0 不认顶层 `thinking` kwarg
   （直传报 unexpected keyword argument），SDK 指定的透传方式是 extra_body。
2. **不得覆盖调用方的 extra_body**：调用方可能自带 reasoning_split
   （官方文档：M2.7 不传 reasoning_split=true 可能返回空响应），
   payload.update(kwargs) 在其后执行，会把我们的注入整个冲掉。
3. **三个方法都要注入**：complete（默认路径）/ completeStream（流式）/
   complete_with_tools（L4 agent loop）——漏一个就是半修。
4. **旧配置对象无该属性时不得抛 AttributeError**。
"""

from __future__ import annotations

from typing import Any

from app.infrastructure.llm.base_client import LlmMessage
from app.infrastructure.llm.openai_client import OpenAiClient


class _FakeCompletions:
    """记录 create() 的 kwargs；stream=True 时返回可异步迭代对象。"""

    def __init__(self) -> None:
        self.lastKwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.lastKwargs = kwargs
        if kwargs.get("stream"):
            return self._stream()

        class _Message:
            content = '{"ok": true}'
            tool_calls = None

        class _Choice:
            message = _Message()

        class _Usage:
            prompt_tokens = 10
            completion_tokens = 5
            total_tokens = 15
            prompt_tokens_details = None

        class _Resp:
            choices = [_Choice()]
            usage = _Usage()
            model = "MiniMax-M3"

        return _Resp()

    async def _stream(self):
        class _Delta:
            content = "片段"

        class _Chunk:
            choices = [type("C", (), {"delta": _Delta()})()]
            usage = None
            model = "MiniMax-M3"

        yield _Chunk()
        yield type(
            "UsageChunk",
            (),
            {
                "choices": [],
                "usage": type(
                    "U",
                    (),
                    {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "prompt_tokens_details": None,
                    },
                )(),
                "model": "MiniMax-M3",
            },
        )()


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeOpenAi:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()
        self.chat = _FakeChat(self.completions)

    async def close(self) -> None:
        pass


class _Config:
    """最小 LlmConfig 替身（避免依赖 DB）。"""

    def __init__(self, *, disableThinking: bool = False) -> None:
        self.model_name = "MiniMax-M3"
        self.provider = "openai_compatible_proxy"
        self.api_endpoint = "https://api.minimax.cn/v1"
        self.api_key_encrypted = "x"
        self.disable_thinking = disableThinking


def _client(*, disableThinking: bool) -> tuple[OpenAiClient, _FakeOpenAi]:
    fake = _FakeOpenAi()
    cli = OpenAiClient(
        _Config(disableThinking=disableThinking), apiKey="sk-test", client=fake
    )
    return cli, fake


class TestCompletePassthrough:
    async def test_disabled_injects_extra_body_thinking(self) -> None:
        cli, fake = _client(disableThinking=True)

        await cli.complete([LlmMessage(role="user", content="hi")])

        extra = fake.completions.lastKwargs["extra_body"]
        assert extra["thinking"] == {"type": "disabled"}

    async def test_enabled_does_not_inject_extra_body(self) -> None:
        """默认 false：payload 必须与修复前完全一致（守约既有 provider）。"""
        cli, fake = _client(disableThinking=False)

        await cli.complete([LlmMessage(role="user", content="hi")])

        assert "extra_body" not in fake.completions.lastKwargs

    async def test_caller_extra_body_is_merged_not_overwritten(self) -> None:
        """调用方自带 extra_body（reasoning_split）时，两者必须共存。

        payload.update(kwargs) 在注入之后执行——若直接赋值 extra_body，
        调用方的会赢，我们的关闭 thinking 静默失效。
        """
        cli, fake = _client(disableThinking=True)

        await cli.complete(
            [LlmMessage(role="user", content="hi")],
            extra_body={"reasoning_split": True},
        )

        extra = fake.completions.lastKwargs["extra_body"]
        assert extra["reasoning_split"] is True
        assert extra["thinking"] == {"type": "disabled"}

    async def test_caller_extra_body_thinking_wins(self) -> None:
        """调用方显式给了 thinking 时以调用方为准（本层不覆盖别人的显式意图）。"""
        cli, fake = _client(disableThinking=True)

        await cli.complete(
            [LlmMessage(role="user", content="hi")],
            extra_body={"thinking": {"type": "adaptive"}},
        )

        extra = fake.completions.lastKwargs["extra_body"]
        assert extra["thinking"] == {"type": "adaptive"}

    async def test_config_without_attribute_defaults_to_no_injection(self) -> None:
        """旧配置对象（无该属性，如测试替身/历史缓存）不得抛 AttributeError。"""

        class _OldConfig:
            model_name = "gpt-4o"
            provider = "openai"
            api_endpoint = None
            api_key_encrypted = "x"

        fake = _FakeOpenAi()
        cli = OpenAiClient(_OldConfig(), apiKey="sk-test", client=fake)

        await cli.complete([LlmMessage(role="user", content="hi")])

        assert "extra_body" not in fake.completions.lastKwargs


class TestCompleteStreamPassthrough:
    async def test_stream_injects_extra_body_thinking(self) -> None:
        """流式是 chat 的默认路径——漏了它等于半修。"""
        cli, fake = _client(disableThinking=True)

        chunks = [
            c async for c in cli.completeStream(
                [LlmMessage(role="user", content="hi")]
            )
        ]

        extra = fake.completions.lastKwargs["extra_body"]
        assert extra["thinking"] == {"type": "disabled"}
        assert any(c.isDone for c in chunks)

    async def test_stream_enabled_does_not_inject(self) -> None:
        cli, fake = _client(disableThinking=False)

        _ = [c async for c in cli.completeStream([LlmMessage(role="user", content="hi")])]

        assert "extra_body" not in fake.completions.lastKwargs


class TestCompleteWithToolsPassthrough:
    async def test_tools_injects_extra_body_thinking(self) -> None:
        """L4 agent loop 走 complete_with_tools——思维链同样挤占预算。"""
        cli, fake = _client(disableThinking=True)

        await cli.complete_with_tools(
            [LlmMessage(role="user", content="hi")],
            tools=[{"type": "function", "function": {"name": "t"}}],
        )

        extra = fake.completions.lastKwargs["extra_body"]
        assert extra["thinking"] == {"type": "disabled"}

    async def test_tools_enabled_does_not_inject(self) -> None:
        cli, fake = _client(disableThinking=False)

        await cli.complete_with_tools([LlmMessage(role="user", content="hi")])

        assert "extra_body" not in fake.completions.lastKwargs