"""Token 计数器单元测试。

TDD RED 阶段：定义期望行为，实现尚未就绪时部分用例应失败。
覆盖：tiktoken 计数、启发式近似计数、工厂按 provider 选型。
"""

from __future__ import annotations

from app.domain.enums import ProviderType
from app.infrastructure.token_counter.factory import getCounter
from app.infrastructure.token_counter.heuristic_counter import HeuristicCounter
from app.infrastructure.token_counter.tiktoken_counter import TiktokenCounter


class TestTiktokenCounter:
    """tiktoken 计数器（OpenAI / 兼容代理）。"""

    def test_counts_tokens_for_known_openai_model(self) -> None:
        # Arrange
        counter = TiktokenCounter()
        text = "Hello world, this is a token counting test."
        # Act
        count = counter.countTokens("gpt-4o", text)
        # Assert - 非空文本应返回正数，且小于字符数
        assert count > 0
        assert count < len(text)

    def test_returns_zero_for_empty_text(self) -> None:
        counter = TiktokenCounter()
        assert counter.countTokens("gpt-4o", "") == 0

    def test_returns_zero_for_whitespace_only(self) -> None:
        counter = TiktokenCounter()
        assert counter.countTokens("gpt-4o", "   \n\t  ") == 0

    def test_counts_more_tokens_for_longer_text(self) -> None:
        counter = TiktokenCounter()
        short = "one two three"
        longText = "one two three " * 100
        shortCount = counter.countTokens("gpt-4o", short)
        longCount = counter.countTokens("gpt-4o", longText)
        assert longCount > shortCount

    def test_is_approximate_is_false(self) -> None:
        counter = TiktokenCounter()
        assert counter.isApproximate() is False

    def test_falls_back_when_model_unknown(self) -> None:
        """未知模型名应回退到默认编码器，而非抛异常。"""
        counter = TiktokenCounter()
        count = counter.countTokens("totally-unknown-model-xyz", "Hello world")
        assert count > 0


class TestHeuristicCounter:
    """启发式近似计数器（Ollama / 未知模型）。"""

    def test_returns_positive_count_for_text(self) -> None:
        counter = HeuristicCounter()
        count = counter.countTokens("llama3.1", "Hello world, this is a test.")
        assert count > 0

    def test_returns_zero_for_empty_text(self) -> None:
        counter = HeuristicCounter()
        assert counter.countTokens("llama3.1", "") == 0

    def test_is_approximate_is_true(self) -> None:
        counter = HeuristicCounter()
        assert counter.isApproximate() is True

    def test_count_is_roughly_proportional_to_length(self) -> None:
        """中文文本应比英文多计 token（按字符近似）。"""
        counter = HeuristicCounter()
        en = "hello"
        zh = "你好世界这是一个中文测试句子"
        assert counter.countTokens("llama3.1", zh) > counter.countTokens("llama3.1", en)

    def test_handles_chinese_text(self) -> None:
        counter = HeuristicCounter()
        count = counter.countTokens("llama3.1", "查询上个月华北地区的订单总额")
        assert count > 5


class TestTokenCounterFactory:
    """工厂按 provider 选择计数器。"""

    def test_returns_tiktoken_for_openai(self) -> None:
        counter = getCounter(ProviderType.OPENAI)
        assert isinstance(counter, TiktokenCounter)

    def test_returns_tiktoken_for_azure_openai(self) -> None:
        counter = getCounter(ProviderType.AZURE_OPENAI)
        assert isinstance(counter, TiktokenCounter)

    def test_returns_tiktoken_for_compatible_proxy(self) -> None:
        """国内兼容代理也用 tiktoken 近似（cl100k_base）。"""
        counter = getCounter(ProviderType.OPENAI_COMPATIBLE_PROXY)
        assert isinstance(counter, TiktokenCounter)

    def test_returns_heuristic_for_ollama(self) -> None:
        counter = getCounter(ProviderType.OLLAMA)
        assert isinstance(counter, HeuristicCounter)

    def test_get_counter_is_cached_per_provider(self) -> None:
        """同一 provider 应复用同一实例（缓存）。"""
        a = getCounter(ProviderType.OPENAI)
        b = getCounter(ProviderType.OPENAI)
        assert a is b
