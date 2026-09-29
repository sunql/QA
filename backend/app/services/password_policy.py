"""密码策略（中策略：≥8 位、含字母 + 数字）。

SSOT：后端落库前校验 + 前端 ``GET /api/v1/auth/password-policy`` 拉到客户端即时校验。
feat-user-auth（2026-09-20）。
"""

from __future__ import annotations

import re
from typing import Literal

MIN_LENGTH = 8
"""最小长度。中策略：8 位。"""

_PASSWORD_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{" + str(MIN_LENGTH) + r",}$")

WEAK = "weak"
MEDIUM = "medium"
STRONG = "strong"


def validate_password(password: str) -> tuple[bool, str | None]:
    """校验密码是否符合策略。返回 ``(valid, error_msg_key_or_none)``。

    - 弱：返回 ``(False, "MSG_PASSWORD_TOO_WEAK")``
    - 符合：返回 ``(True, None)``
    """
    if not _PASSWORD_RE.match(password):
        return False, "MSG_PASSWORD_TOO_WEAK"
    return True, None


def strength_label(password: str) -> Literal["weak", "medium", "strong"]:
    """强度标签，供前端 Progress 渲染。

    - weak：不满足策略（短于 8 位 / 缺字母 / 缺数字）
    - medium：满足策略（≥8 位 + 字母 + 数字）
    - strong：满足策略 + 含特殊字符 + 长度 ≥ 12
    """
    if not _PASSWORD_RE.match(password):
        return WEAK
    has_special = bool(re.search(r"[^A-Za-z0-9]", password))
    if has_special and len(password) >= 12:
        return STRONG
    return MEDIUM