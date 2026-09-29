"""jwt_codec 单元测试（feat-user-auth）。

覆盖：
- 签发/解码 roundtrip
- jti 唯一性
- 验签失败：垃圾 token / alg=none / wrong audience
- 过期 token
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from jwt import InvalidTokenError

from app.config import getSettings
from app.services.jwt_codec import decode_jwt, sign_jwt


@pytest.fixture(autouse=True)
def _ensure_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试环境保证 JWT_SECRET ≥ 32 字节（避免 main.py 启动校验失败影响）。"""
    if not getSettings().jwtSecret:
        monkeypatch.setenv("JWT_SECRET", "x" * 32)


class TestSignAndDecode:
    def test_roundtrip(self) -> None:
        token, jti, exp = sign_jwt(user_id=42, username="alice")
        payload = decode_jwt(token)
        assert payload["sub"] == "42"
        assert payload["username"] == "alice"
        assert payload["jti"] == jti
        assert payload["iss"] == getSettings().jwtIssuer
        assert payload["aud"] == getSettings().jwtAudience
        assert payload["exp"] == int(exp.timestamp())

    def test_jti_is_unique(self) -> None:
        _, jti1, _ = sign_jwt(user_id=1, username="a")
        _, jti2, _ = sign_jwt(user_id=1, username="a")
        assert jti1 != jti2

    def test_iss_and_aud_are_configured(self) -> None:
        settings = getSettings()
        token, _, _ = sign_jwt(user_id=1, username="a")
        payload = decode_jwt(token)
        assert payload["iss"] == settings.jwtIssuer
        assert payload["aud"] == settings.jwtAudience


class TestDecodeFailures:
    def test_rejects_garbage(self) -> None:
        with pytest.raises(InvalidTokenError):
            decode_jwt("not-a-jwt")

    def test_rejects_expired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """临时缩短 TTL=1，签名后等 1.5s → 过期。"""
        settings = getSettings()
        original = settings.jwtTtlSeconds
        # 改 model_copy 避开 frozen 限制
        monkeypatch.setattr(settings, "jwtTtlSeconds", 1)
        token, _, _ = sign_jwt(user_id=1, username="a")
        time.sleep(1.5)
        with pytest.raises(InvalidTokenError):
            decode_jwt(token)
        # 不强制恢复（lru_cache 单例）

    def test_rejects_alg_none(self) -> None:
        """攻击：客户端手搓 alg=none token。"""
        header = (
            base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}')
            .rstrip(b"=")
            .decode()
        )
        payload = (
            base64.urlsafe_b64encode(b'{"sub":"1","exp":9999999999}')
            .rstrip(b"=")
            .decode()
        )
        fake = f"{header}.{payload}."
        with pytest.raises(InvalidTokenError):
            decode_jwt(fake)

    def test_rejects_wrong_secret(self) -> None:
        """用错 secret 签名 → 解码失败。"""
        import jwt as pyjwt

        bad_token = pyjwt.encode(
            {"sub": "1", "username": "a", "jti": "x"},
            "different-secret-xxxxxxxxxxxxxxxx",
            algorithm="HS256",
        )
        with pytest.raises(InvalidTokenError):
            decode_jwt(bad_token)

    def test_rejects_wrong_audience(self) -> None:
        """payload aud 与 settings 不一致 → 解码失败。"""
        import jwt as pyjwt

        token = pyjwt.encode(
            {
                "sub": "1",
                "username": "a",
                "jti": "x",
                "iss": getSettings().jwtIssuer,
                "aud": "different-audience",
                "exp": 9999999999,
            },
            getSettings().jwtSecret,
            algorithm="HS256",
        )
        with pytest.raises(InvalidTokenError):
            decode_jwt(token)