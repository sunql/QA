"""UserCreate password 字段测试（feat-admin-user-password Task 1）。

覆盖 schema 层约束：
- password 必填（min_length=8）
- password 过短被 ValidationError 拦截
- password 合法时通过校验
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.rbac import UserCreate


def test_user_create_requires_password():
    """新建用户必须带 password 字段（min_length=8）。"""
    with pytest.raises(ValidationError) as exc:
        UserCreate(username="alice", displayName="Alice")
    errors = exc.value.errors()
    assert any(e["loc"] == ("password",) for e in errors)


def test_user_create_password_too_short():
    with pytest.raises(ValidationError) as exc:
        UserCreate(username="alice", displayName="Alice", password="short")
    errors = exc.value.errors()
    assert any(e["loc"] == ("password",) for e in errors)


def test_user_create_password_ok():
    u = UserCreate(username="alice", displayName="Alice", password="ValidPass1")
    assert u.password == "ValidPass1"