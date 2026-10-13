"""Task 1.2: Production startup config gate — RED phase.

Tests that APP_ENV=production enforces AUTH_MODE=real, no stub,
non-default JWT_SECRET, and FERNET_KEY presence.
"""

import pytest
from app.config import ProductionConfigError, validateProduction


class TestValidateProduction:
    def test_stub_mode_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AUTH_MODE", "stub")
        with pytest.raises(ProductionConfigError, match="AUTH_MODE"):
            validateProduction()

    def test_stub_enabled_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AUTH_MODE", "real")
        monkeypatch.setenv("AUTH_STUB_ENABLED", "true")
        with pytest.raises(ProductionConfigError, match="STUB"):
            validateProduction()

    def test_default_jwt_secret_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AUTH_MODE", "real")
        monkeypatch.setenv("JWT_SECRET", "dev-secret-change-me")
        with pytest.raises(ProductionConfigError, match="JWT"):
            validateProduction()

    def test_missing_fernet_key_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AUTH_MODE", "real")
        monkeypatch.setenv("JWT_SECRET", "x" * 32)
        monkeypatch.delenv("FERNET_KEY", raising=False)
        with pytest.raises(ProductionConfigError, match="FERNET"):
            validateProduction()

    def test_valid_production_passes(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AUTH_MODE", "real")
        monkeypatch.setenv("AUTH_STUB_ENABLED", "false")
        monkeypatch.setenv("JWT_SECRET", "x" * 32)
        monkeypatch.setenv("FERNET_KEY", "x" * 44)
        assert validateProduction() is None
