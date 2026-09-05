"""表达式白名单扩展（feat-dq-rule-auto-generation）：值域字面量 + 正则 + 转型。"""
from __future__ import annotations

import pytest

from app.domain.exceptions import ValidationError
from app.services.data_quality_evaluators._common import validate_expression


@pytest.mark.parametrize("expr", [
    "STATUS IN ('NEW','CONFIRMED','CLOSED')",
    "STATUS IS NULL OR STATUS ~ '^-?[0-9]+$'",
    "AMT ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'",
    "COL::text IS NOT NULL",
    "FLAG IN ('true','false','t','f')",
])
def test_extended_expressions_pass(expr: str) -> None:
    assert validate_expression(expr) == expr


@pytest.mark.parametrize("expr,reason", [
    ("STATUS IN ('A'); DROP TABLE X", "非法字符"),   # 分号不在白名单
    ("COL ~ 'x' UNION SELECT 1", "禁用关键字"),     # UNION 黑名单
    ("COL ~ 'x' -- comment", "禁用关键字"),         # 注释
])
def test_injection_still_blocked(expr: str, reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        validate_expression(expr)
