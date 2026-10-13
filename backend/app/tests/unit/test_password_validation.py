"""Task 2.3: bcrypt 72-byte UTF-8 password limit (audit Finding 6).

RED first: tests must fail before the validator is added.
GREEN: implement _checkPasswordBytes in password_policy.py and attach validators.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.auth import AuthLoginRequest
from app.schemas.auth import AuthChangePasswordRequest
from app.schemas.auth import AdminResetPasswordRequest
from app.schemas.rbac import UserCreate


# ---------------------------------------------------------------------------
# AuthLoginRequest — the 4 brief-prescribed tests (verbatim)
# ---------------------------------------------------------------------------

def test_72_byte_password_accepted_on_login():
    pwd = "a" * 72
    schema = AuthLoginRequest(username="x", password=pwd)
    assert schema.password == pwd


def test_73_byte_password_rejected_on_login():
    pwd = "a" * 73
    with pytest.raises(ValidationError) as exc:
        AuthLoginRequest(username="x", password=pwd)
    assert "字节" in str(exc.value)


def test_multibyte_password_under_72_bytes_accepted_on_login():
    # 24 Chinese chars × 3 bytes = 72 bytes
    pwd = "中" * 24
    schema = AuthLoginRequest(username="x", password=pwd)
    assert schema.password == pwd


def test_multibyte_password_over_72_bytes_rejected_on_login():
    # 25 Chinese chars × 3 bytes = 75 bytes
    pwd = "中" * 25
    with pytest.raises(ValidationError):
        AuthLoginRequest(username="x", password=pwd)


# ---------------------------------------------------------------------------
# AuthChangePasswordRequest — oldPassword and newPassword both validated
# ---------------------------------------------------------------------------

def test_change_password_rejects_73_byte_new():
    pwd = "a" * 73
    with pytest.raises(ValidationError):
        AuthChangePasswordRequest(oldPassword="validOld1", newPassword=pwd)


def test_change_password_rejects_73_byte_old():
    pwd = "a" * 73
    with pytest.raises(ValidationError):
        AuthChangePasswordRequest(oldPassword=pwd, newPassword="validNew1")


# ---------------------------------------------------------------------------
# AdminResetPasswordRequest — newPassword validated
# ---------------------------------------------------------------------------

def test_admin_reset_rejects_73_byte_new():
    pwd = "a" * 73
    with pytest.raises(ValidationError):
        AdminResetPasswordRequest(newPassword=pwd)


# ---------------------------------------------------------------------------
# UserCreate — password validated
# ---------------------------------------------------------------------------

def test_user_create_rejects_73_byte_password():
    pwd = "a" * 73
    with pytest.raises(ValidationError):
        UserCreate(username="x", display_name="Y", password=pwd)
