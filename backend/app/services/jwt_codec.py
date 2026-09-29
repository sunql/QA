"""JWT HS256 签发/校验（feat-user-auth，2026-09-20）。

dev/单服务：单 HS256 secret。iss/aud/algorithm 锁定为环境级配置，避免 alg=none 等
常见攻击；secret 启动期校验长度（≥32 字节），与 PyJWT 签名推荐对齐。

payload 字段：
- iss / aud / iat / exp 标准字段
- sub：用户 DB 主键（str）
- username：冗余存用户名（审计 / 调试方便）
- jti：UUID4，每次签发唯一；user_sessions 表写入此值
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from jwt import InvalidTokenError

from app.config import getSettings


def sign_jwt(*, user_id: int, username: str) -> tuple[str, str, datetime]:
    """签发 JWT。返回 ``(token, jti, expires_at_utc)``。"""
    settings = getSettings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.jwtTtlSeconds)
    jti = str(uuid.uuid4())
    payload = {
        "iss": settings.jwtIssuer,
        "aud": settings.jwtAudience,
        "sub": str(user_id),
        "username": username,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = pyjwt.encode(payload, settings.jwtSecret, algorithm=settings.jwtAlgorithm)
    return token, jti, expires_at


def decode_jwt(token: str) -> dict:
    """验签 + 验 exp + iss + aud。失败抛 ``jwt.InvalidTokenError``。

    ``algorithms`` 显式白名单（防御 alg=none / RS256 混用等攻击）。
    """
    settings = getSettings()
    return pyjwt.decode(
        token,
        settings.jwtSecret,
        algorithms=[settings.jwtAlgorithm],
        audience=settings.jwtAudience,
        issuer=settings.jwtIssuer,
    )