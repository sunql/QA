"""LLM 客户端工厂单测。

主要守住「无 API key 时显式返回 None 而非 raise OpenAIError」的契约，
让调用方能把这种情况映射成 503 LLMUnavailableError，而不是裸 500。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.infrastructure.llm.factory import createClient, resetFactory


class _FakeSettings:
    """最小化 settings 替身：所有 *ApiKey 都为空。"""
    openaiApiKey = ""
    azureOpenaiApiKey = ""
    azureOpenaiEndpoint = ""
    azureOpenaiApiVersion = ""
    deepseekApiKey = ""
    qwenApiKey = ""
    ollamaBaseUrl = "http://localhost:11434"
    openaiModel = "gpt-4o-mini"


class _Cfg(SimpleNamespace):
    """最小化 LlmConfig 替身（鸭子类型）。"""


@pytest.fixture(autouse=True)
def _resetFactory():
    resetFactory()
    yield
    resetFactory()


def test_create_client_returns_none_when_api_key_missing_non_ollama():
    """关键回归测试：provider=openai_compatible_proxy、api_key_encrypted=NULL、
    settings.*ApiKey 也空时，必须返回 None（让路由层 raise LLMUnavailableError → 503），
    而不是让 OpenAI SDK 在构造时 raise OpenAIError（→ 裸 500）。
    """
    cfg = _Cfg(
        id=1,
        provider="openai_compatible_proxy",
        model_name="deepseek-chat",
        api_endpoint="https://api.deepseek.com/v1",
        api_key_encrypted=None,
    )
    settings = _FakeSettings()
    client = createClient(cfg, settings=settings)
    assert client is None, (
        "无 API key 时必须返回 None（让上层映射 503），"
        f"实际返回 {type(client).__name__ if client else 'None'}"
    )


def test_create_client_with_explicit_api_key_works():
    """注入 apiKey 时正常构造，不返回 None。"""
    cfg = _Cfg(
        id=2,
        provider="openai_compatible_proxy",
        model_name="deepseek-chat",
        api_endpoint="https://api.deepseek.com/v1",
        api_key_encrypted=None,
    )
    settings = _FakeSettings()
    client = createClient(cfg, settings=settings, apiKey="sk-test-injected")
    assert client is not None
    assert hasattr(client, "_client")  # OpenAiClient 构造成功


def test_create_client_ollama_does_not_need_api_key():
    """Ollama 不需要 API key，直接构造成功。"""
    cfg = _Cfg(
        id=3,
        provider="ollama",
        model_name="llama3.1",
        api_endpoint="http://localhost:11434",
        api_key_encrypted=None,
    )
    settings = _FakeSettings()
    client = createClient(cfg, settings=settings)
    assert client is not None


def test_create_client_caches_by_config_id():
    """同一 config id 重复调用应返回同一实例（缓存）。"""
    cfg = _Cfg(
        id=4,
        provider="ollama",
        model_name="llama3.1",
        api_endpoint="http://localhost:11434",
        api_key_encrypted=None,
    )
    settings = _FakeSettings()
    c1 = createClient(cfg, settings=settings)
    c2 = createClient(cfg, settings=settings)
    assert c1 is c2
