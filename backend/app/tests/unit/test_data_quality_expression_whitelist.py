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


@pytest.mark.parametrize("expr", [
    "STATUS IN ('A'); DROP TABLE X",   # 含分号（不在白名单）
    "COL ~ 'x' UNION SELECT 1",        # UNION 黑名单
    "COL ~ 'x' -- comment",            # 注释
])
def test_injection_still_blocked(expr: str) -> None:
    with pytest.raises(ValidationError):
        validate_expression(expr)
