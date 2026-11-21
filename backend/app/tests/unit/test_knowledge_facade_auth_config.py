"""P4 D37：知识服务 Facade 开关与鉴权门禁（``ENABLE_KNOWLEDGE_FACADE``）。

与 P2 的 ``ENABLE_SEMANTIC_RELEASE`` / P3a 的 ``ENABLE_SEMANTIC_REGISTRY_RAG``
同款门禁口径：

- 开关**默认关** ⇒ 关时整组路由不注册 ⇒ 对外 404；
- 开关开着而鉴权是 stub ⇒ **告警**（不阻塞启动，本地/测试需要 stub）。

为什么 stub 鉴权下必须告警：知识 Facade 的**域可见性**是按调用方身份
（`user.departments` / `user.roles`）推导出来的 ⇒ 它就是一条**授权边界**。
stub 鉴权下身份可被任意客户端用 ``X-User-Roles`` / ``X-User-Departments``
之类头伪造 ⇒ 可见域推导的依据不可信 ⇒ 域级隔离形同虚设。
"""

from __future__ import annotations

import pytest

from app.config import Settings, knowledgeFacadeAuthMisconfiguration


def test_知识facade默认关闭() -> None:
    """默认关：未部署时对外 404（不改变现状行为）。"""
    assert Settings(_env_file=None).enableKnowledgeFacade is False


def test_知识facade可由环境变量打开(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_FACADE", "true")

    assert Settings(_env_file=None).enableKnowledgeFacade is True


def test_开关开且鉴权为stub时告警() -> None:
    settings = Settings(
        _env_file=None,
        ENABLE_KNOWLEDGE_FACADE="true",
        AUTH_MODE="stub",
        AUTH_STUB_ENABLED="true",
    )

    reason = knowledgeFacadeAuthMisconfiguration(settings)

    assert reason is not None
    assert "ENABLE_KNOWLEDGE_FACADE" in reason
    # 告警要说清「为什么危险」，否则部署方无从判断
    assert "stub" in reason


def test_开关开且鉴权为real但不放行stub头时无告警() -> None:
    """AUTH_MODE=real 且 ALLOW_STUB_WHEN_REAL=false ⇒ stub 头解析前即被拒。

    `AUTH_STUB_ENABLED=true` 不构成暴露面（只影响落到 stub 分支之后的行为）。
    """
    settings = Settings(
        _env_file=None,
        ENABLE_KNOWLEDGE_FACADE="true",
        AUTH_MODE="real",
        AUTH_STUB_ENABLED="true",
        ALLOW_STUB_WHEN_REAL="false",
        JWT_SECRET="test-secret-at-least-32-chars-long-000",
    )

    assert knowledgeFacadeAuthMisconfiguration(settings) is None


def test_开关开且鉴权为real但放行stub头时仍告警() -> None:
    """AUTH_MODE=real 但 ALLOW_STUB_WHEN_REAL=true ⇒ 身份仍可伪造 ⇒ 必须告警。"""
    settings = Settings(
        _env_file=None,
        ENABLE_KNOWLEDGE_FACADE="true",
        AUTH_MODE="real",
        AUTH_STUB_ENABLED="true",
        ALLOW_STUB_WHEN_REAL="true",
        JWT_SECRET="test-secret-at-least-32-chars-long-000",
    )

    assert knowledgeFacadeAuthMisconfiguration(settings) is not None


def test_开关开且真鉴权时无告警() -> None:
    settings = Settings(
        _env_file=None,
        ENABLE_KNOWLEDGE_FACADE="true",
        AUTH_MODE="real",
        AUTH_STUB_ENABLED="false",
        JWT_SECRET="test-secret-at-least-32-chars-long-000",
    )

    assert knowledgeFacadeAuthMisconfiguration(settings) is None


def test_开关关时永不告警() -> None:
    """开关关 = 本地/测试的默认组合，不该刷告警。"""
    settings = Settings(
        _env_file=None,
        ENABLE_KNOWLEDGE_FACADE="false",
        AUTH_MODE="stub",
    )

    assert knowledgeFacadeAuthMisconfiguration(settings) is None
