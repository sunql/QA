"""url_validator 单元测试：生产环境阻断 http + 私有 IP/loopback。"""
from __future__ import annotations

import pytest
from app.infrastructure.url_validator import validateEndpointUrl


class TestValidateEndpointUrl:
    def test_https_production_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENV", "production")
        assert validateEndpointUrl("https://api.openai.com/v1") is None

    def test_http_production_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(ValueError, match="必须为 https"):
            validateEndpointUrl("http://api.openai.com/v1")

    def test_private_ip_production_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(ValueError, match="私有地址"):
            validateEndpointUrl("https://10.0.0.1/v1")

    def test_loopback_production_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(ValueError, match="私有地址"):
            validateEndpointUrl("https://127.0.0.1/v1")

    def test_localhost_dev_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENV", "development")
        assert validateEndpointUrl("http://localhost:1234/v1") is None
