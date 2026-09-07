"""数据质量评估器共享工具（Phase 1.2）。

约束：
- 标识符白名单：SQL 标识符必须匹配 ^[A-Za-z_][A-Za-z0-9_]*$，失败抛 ValidationError。
- 表达式白名单：rule_expression 允许字母/数字/下划线 + 比较算术 + 括号 + 小数点 +
  单引号字面量（值域 IN 列表）+ POSIX 正则运算符（~ ^ $ { } [ ] ?）+ :: 转型。
  失败抛 ValidationError。
- 单条 SQL 走 BusinessDbAdapter.execute_read_only，自动享受只读护栏 + 行数 + 超时。

不引入 ORM bind params：identifier 是 SQL 标识符（不是字面量），必须静态拼接；因此
identifier 强校验是最后一道防线。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.domain.exceptions import ValidationError
from app.infrastructure.business_db_pool import BusinessDbAdapter

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 安全表达式：标识符 / 数字 / 比较算术 / 括号 / 小数点 / 空白 / AND OR NOT / IS NULL。
# 允许双引号以支持 PostgreSQL 大小写敏感标识符（如 "ORDERQTY" > 0）；双引号在 SQL
# 标识符上下文内无注入路径（与字符串字面量不同，标识符内无双引号转义语义）。
# 单引号字面量（值域 IN 列表）/ POSIX 正则运算符 ~ ^ $ { } [ ] ? / :: 转型。
# 引号字面量与正则由生成端 sanitize（值内禁 '），黑名单 + 只读 adapter 仍是防线。
_EXPR_RE = re.compile(r"""^[A-Za-z0-9_\s\.\(\)\<\>\=\!\,\*\+\-\/"\':~\^\$\{\}\[\]\?]+$""")
# 阻断危险关键字（即使在白名单内）
_FORBIDDEN_KEYWORDS = {
    "DROP",
    "DELETE",
    "UPDATE",
    "INSERT",
    "TRUNCATE",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "EXEC",
    "EXECUTE",
    "UNION",
    "--",
    "/*",
}


def validate_identifier(value: str, *, role: str) -> str:
    """校验 value 是合法 SQL 标识符（表名/列名），返回原值。

    role 用于错误信息（如 "table" / "column"），便于排查。
    """
    if not _IDENT_RE.match(value):
        raise ValidationError(
            f"非法 {role} 标识符（必须 ^[A-Za-z_][A-Za-z0-9_]*$）: {value!r}"
        )
    return value


def validate_expression(value: str) -> str:
    """校验 rule_expression 仅含安全 token（标识符 + 比较 + 算术 + 括号 + 值域字面量
    + POSIX 正则运算符 + :: 转型）。

    拒绝任何 DDL/DML/UNION/注释关键字。
    """
    if not value or not value.strip():
        raise ValidationError("rule_expression 不能为空")
    if not _EXPR_RE.match(value):
        raise ValidationError(
            f"表达式包含非法字符（仅允许标识符 + 比较 + 算术 + 括号 + "
            f"单引号字面量 / POSIX 正则 ~ ^ $ {{ }} [ ] ? / :: 转型）: {value!r}"
        )
    upper = value.upper()
    for kw in _FORBIDDEN_KEYWORDS:
        # 仅对「字母数字」关键字用空白边界匹配（避免误杀 SUBSTR / UPDATE_TIME）。
        # 符号类关键字（-- / /*）必须裸字面子串命中，因为它们没有 word boundary 概念。
        if kw.isalnum():
            if re.search(rf"\b{kw}\b", upper):
                raise ValidationError(f"表达式包含禁用关键字 {kw}: {value!r}")
        else:
            if kw in value:
                raise ValidationError(f"表达式包含禁用关键字 {kw}: {value!r}")
    return value


def _first_int(row: Mapping[str, Any], *keys: str) -> int:
    """从结果行取第一个非 None 整数；找不到返回 0。"""
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        if isinstance(v, bool):
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                continue
    return 0


async def run_read_only(
    adapter: BusinessDbAdapter,
    sql: str,
) -> int:
    """跑单行单整数结果（绝大多数评估 SQL 形态）。"""
    rows = await adapter.execute_read_only(sql)
    if not rows:
        return 0
    return _first_int(rows[0], "passed", "total", "cnt", "count")


__all__ = [
    "validate_identifier",
    "validate_expression",
    "run_read_only",
]