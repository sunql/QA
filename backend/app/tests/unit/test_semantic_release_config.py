"""语义发布 API 开关的默认值与别名（P1b / D9）。"""

from __future__ import annotations

import pytest

from app.config import Settings, semanticReleaseAuthMisconfiguration


def test_semantic_release_api_is_disabled_by_default() -> None:
    """默认关闭：未完成部署与前端接入前，不该有可探测的端点。"""
    assert Settings(_env_file=None).enableSemanticRelease is False


def test_semantic_release_api_can_be_enabled_by_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_SEMANTIC_RELEASE", "true")

    assert Settings(_env_file=None).enableSemanticRelease is True


def test_stub_auth_with_flag_on_is_reported() -> None:
    """stub 鉴权下 admin 可被伪造，开开关等于匿名可读治理快照 ⇒ 必须告警。"""
    settings = Settings(
        _env_file=None,
        ENABLE_SEMANTIC_RELEASE="true",
        AUTH_MODE="stub",
        AUTH_STUB_ENABLED="true",
    )

    reason = semanticReleaseAuthMisconfiguration(settings)

    assert reason is not None
    assert "ENABLE_SEMANTIC_RELEASE" in reason


def test_real_auth_without_stub_headers_is_clean() -> None:
    settings = Settings(
        _env_file=None,
        ENABLE_SEMANTIC_RELEASE="true",
        AUTH_MODE="real",
        AUTH_STUB_ENABLED="false",
        JWT_SECRET="test-secret-at-least-32-chars-long-000",
    )

    assert semanticReleaseAuthMisconfiguration(settings) is None


def test_flag_off_never_warns() -> None:
    """本地/测试默认 stub + 开关关闭是正常组合，不该刷告警。"""
    settings = Settings(_env_file=None, ENABLE_SEMANTIC_RELEASE="false", AUTH_MODE="stub")

    assert semanticReleaseAuthMisconfiguration(settings) is None


def test_real_auth_with_stub_headers_still_warns() -> None:
    """AUTH_MODE=real 但放行 stub 头，门禁同样可伪造。"""
    settings = Settings(
        _env_file=None,
        ENABLE_SEMANTIC_RELEASE="true",
        AUTH_MODE="real",
        AUTH_STUB_ENABLED="true",
        JWT_SECRET="test-secret-at-least-32-chars-long-000",
    )

    assert semanticReleaseAuthMisconfiguration(settings) is not None
