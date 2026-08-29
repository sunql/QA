"""API Key 加密工具。

使用 Fernet 对称加密。密钥来自 Settings.secretKey。
遵循不可变原则：encrypt/decrypt 返回新字符串，不修改入参。
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import getSettings
from app.domain.error_messages import (
    MSG_API_KEY_DECRYPT_FAILED,
    MSG_FERNET_KEY_INVALID,
    MSG_FERNET_KEY_INVALID_DETAIL,
)
from app.domain.exceptions import ConfigError

_fernet: Fernet | None = None


def _getFernet() -> Fernet:
    """返回 Fernet 单例。secretKey 必须是合法的 Fernet key。"""
    global _fernet
    if _fernet is None:
        secret = getSettings().secretKey
        try:
            _fernet = Fernet(secret.encode())
        except (ValueError, TypeError) as exc:
            raise ConfigError(
                MSG_FERNET_KEY_INVALID,
                detail=MSG_FERNET_KEY_INVALID_DETAIL.format(exc=exc),
            ) from exc
    return _fernet


def encryptApiKey(plaintext: str) -> str:
    """加密明文 API Key，返回密文字符串。空串原样返回。"""
    if not plaintext:
        return ""
    return _getFernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decryptApiKey(ciphertext: str) -> str:
    """解密 API Key。空串或 None 原样返回。密文非法抛 ConfigError。"""
    if not ciphertext:
        return ""
    try:
        return _getFernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ConfigError(MSG_API_KEY_DECRYPT_FAILED, detail=str(exc)) from exc


def resetFernet() -> None:
    """重置单例（测试用）。"""
    global _fernet
    _fernet = None
