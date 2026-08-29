"""OpenAI SDK 共享 HTTP 客户端工厂。

背景：httpx 默认信任环境/系统代理（macOS 系统代理 127.0.0.1:12450），
会把 openai SDK 的请求劫持进本地代理，导致对 localhost（如 Ollama）的
embedding/chat 调用间歇性 502。curl 直连正常、aiohttp 不读环境代理，
故 OllamaClient 一直没踩坑（见其注释）。

修复：构造 AsyncOpenAI/AsyncAzureOpenAI 时注入 trust_env=False 的 httpx
transport，绕过环境/系统代理，直接连接目标 base_url。
"""

from __future__ import annotations

import httpx

from app.config import getSettings


def buildHttpClient() -> httpx.AsyncClient:
    """返回 openai SDK 可用的 httpx AsyncClient。

    默认 trust_env=False 绕过系统/环境代理（macOS 系统代理曾劫持 localhost 请求
    导致 502）。需经 HTTPS_PROXY 或 SSL_CERT_FILE 访问外部 LLM 的部署须设
    LLM_TRUST_ENV=true 以恢复信任环境代理/证书。
    """
    transport = httpx.AsyncHTTPTransport(trust_env=getSettings().llmTrustEnv)
    return httpx.AsyncClient(transport=transport)
