"""P3a D19：语义注册表 RAG 过滤开关与鉴权门禁（``ENABLE_SEMANTIC_REGISTRY_RAG``）。

与 P2 的 ``ENABLE_SEMANTIC_REGISTRY_NL2SQL`` 同款门禁口径：

- 开关**默认关** ⇒ 关时读路径逐字节等于今天（不过滤、不碰 v2）；
- 开关开着而鉴权是 stub ⇒ **告警**（不阻塞启动，本地/测试需要 stub）。

为什么 stub 鉴权下必须告警：一旦过滤维度（可见域/组织/实体）是**按调用方身份**
解析出来的，过滤本身就成了一条**授权边界**。stub 鉴权下身份可被任意客户端用
``X-User-Roles`` 之类头伪造 ⇒ 收窄依据不可信 ⇒ 过滤形同虚设。
"""

from __future__ import annotations

import pytest

from app.config import Settings, semanticRegistryRagAuthMisconfiguration


def test_语义注册表rag默认关闭() -> None:
    """默认关：过滤能力未经影子比对，不该在任何环境自动生效。"""
    assert Settings(_env_file=None).enableSemanticRegistryRag is False


def test_语义注册表rag可由环境变量打开(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_SEMANTIC_REGISTRY_RAG", "true")

    assert Settings(_env_file=None).enableSemanticRegistryRag is True


def test_开关开且鉴权为stub时告警() -> None:
    settings = Settings(
        _env_file=None,
        ENABLE_SEMANTIC_REGISTRY_RAG="true",
        AUTH_MODE="stub",
        AUTH_STUB_ENABLED="true",
    )

    reason = semanticRegistryRagAuthMisconfiguration(settings)

    assert reason is not None
    assert "ENABLE_SEMANTIC_REGISTRY_RAG" in reason
    # 告警要说清「为什么危险」，否则部署方无从判断
    assert "stub" in reason


def test_开关开且鉴权为real但不放行stub头时无告警() -> None:
    """AUTH_MODE=real 且 ALLOW_STUB_WHEN_REAL=false ⇒ stub 头解析前即被拒。

    `AUTH_STUB_ENABLED=true` 不构成暴露面（只影响落到 stub 分支之后的行为）。
    """
    settings = Settings(
        _env_file=None,
        ENABLE_SEMANTIC_REGISTRY_RAG="true",
        AUTH_MODE="real",
        AUTH_STUB_ENABLED="true",
        ALLOW_STUB_WHEN_REAL="false",
        JWT_SECRET="test-secret-at-least-32-chars-long-000",
    )

    assert semanticRegistryRagAuthMisconfiguration(settings) is None


def test_开关开且鉴权为real但放行stub头时仍告警() -> None:
    """AUTH_MODE=real 但 ALLOW_STUB_WHEN_REAL=true ⇒ 身份仍可伪造 ⇒ 必须告警。"""
    settings = Settings(
        _env_file=None,
        ENABLE_SEMANTIC_REGISTRY_RAG="true",
        AUTH_MODE="real",
        AUTH_STUB_ENABLED="true",
        ALLOW_STUB_WHEN_REAL="true",
        JWT_SECRET="test-secret-at-least-32-chars-long-000",
    )

    assert semanticRegistryRagAuthMisconfiguration(settings) is not None


def test_开关开且真鉴权时无告警() -> None:
    settings = Settings(
        _env_file=None,
        ENABLE_SEMANTIC_REGISTRY_RAG="true",
        AUTH_MODE="real",
        AUTH_STUB_ENABLED="false",
        JWT_SECRET="test-secret-at-least-32-chars-long-000",
    )

    assert semanticRegistryRagAuthMisconfiguration(settings) is None


def test_开关关时永不告警() -> None:
    """开关关 = 本地/测试的默认组合，不该刷告警。"""
    settings = Settings(
        _env_file=None,
        ENABLE_SEMANTIC_REGISTRY_RAG="false",
        AUTH_MODE="stub",
    )

    assert semanticRegistryRagAuthMisconfiguration(settings) is None
