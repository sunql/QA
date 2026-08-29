"""LLM 共享 HTTP 客户端工厂单元测试。

验证 buildHttpClient() 传给 AsyncHTTPTransport 的 trust_env 值：
- 默认 False（绕过 macOS 系统代理，修复对 localhost/Ollama 的 502）；
- LLM_TRUST_ENV=true 时恢复 True（供经 HTTPS_PROXY 访问外部 LLM 的部署）。
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.config import getSettings
from app.infrastructure.llm.http_client import buildHttpClient


@pytest.fixture(autouse=True)
def _clearSettings() -> None:
    getSettings.cache_clear()
    yield
    getSettings.cache_clear()


def test_default_trust_env_is_false() -> None:
    with patch(
        "app.infrastructure.llm.http_client.httpx.AsyncHTTPTransport"
    ) as mockTransport:
        mockTransport.return_value._pool.return_value = None
        client = buildHttpClient()
        assert isinstance(client, httpx.AsyncClient)
        _transport, _kwargs = mockTransport.call_args
        # 默认绕系统/环境代理（修复 macOS 代理劫持 localhost 导致 502）
        assert _kwargs["trust_env"] is False


def test_trust_env_true_when_env_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TRUST_ENV", "true")
    getSettings.cache_clear()
    with patch(
        "app.infrastructure.llm.http_client.httpx.AsyncHTTPTransport"
    ) as mockTransport:
        buildHttpClient()
        _transport, _kwargs = mockTransport.call_args
        # 需经 HTTPS_PROXY / SSL_CERT_FILE 访问外部 LLM 的部署显式开启
        assert _kwargs["trust_env"] is True
