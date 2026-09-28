"""password_policy 单元测试（feat-user-auth）。"""

from __future__ import annotations

from app.services.password_policy import (
    MEDIUM,
    MIN_LENGTH,
    STRONG,
    WEAK,
    strength_label,
    validate_password,
)


class TestValidatePassword:
    """validate_password 返回 (valid, error_msg_key_or_none)。"""

    def test_returns_valid_for_min_policy_compliant(self) -> None:
        ok, err = validate_password("abc12345")
        assert ok is True
        assert err is None

    def test_rejects_too_short(self) -> None:
        ok, err = validate_password("Ab1")
        assert ok is False
        assert err == "MSG_PASSWORD_TOO_WEAK"

    def test_rejects_missing_letter(self) -> None:
        ok, err = validate_password("12345678")
        assert ok is False
        assert err == "MSG_PASSWORD_TOO_WEAK"

    def test_rejects_missing_digit(self) -> None:
        ok, err = validate_password("abcdefgh")
        assert ok is False
        assert err == "MSG_PASSWORD_TOO_WEAK"

    def test_accepts_exactly_min_length(self) -> None:
        """8 位且含字母 + 数字 → 合法（边界内）。"""
        ok, _ = validate_password("a" * 7 + "1")
        assert ok is True

    def test_rejects_one_below_min(self) -> None:
        ok, _ = validate_password("a" * 6 + "1")
        assert ok is False

    def test_rejects_empty(self) -> None:
        ok, _ = validate_password("")
        assert ok is False


class TestStrengthLabel:
    """strength_label 返回 weak / medium / strong。"""

    def test_weak_when_too_short(self) -> None:
        assert strength_label("a1") == WEAK

    def test_weak_when_missing_digit(self) -> None:
        assert strength_label("abcdefgh") == WEAK

    def test_medium_when_meets_policy(self) -> None:
        assert strength_label("abc12345") == MEDIUM

    def test_strong_when_long_with_special(self) -> None:
        assert strength_label("Abc12345!@#$") == STRONG

    def test_strong_requires_length_12_and_special(self) -> None:
        """仅 10 位 + 特殊字符不够 strong。"""
        assert strength_label("Ab12!@#xyz") == MEDIUM


def test_min_length_constant() -> None:
    """契约：SSOT=8，便于前后端共享。"""
    assert MIN_LENGTH == 8