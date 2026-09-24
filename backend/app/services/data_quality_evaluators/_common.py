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

import logging
import re
from collections.abc import Mapping
from typing import Any

from app.domain.exceptions import ValidationError
from app.infrastructure.business_db_pool import BusinessDbAdapter, _OracleAdapter, _SqlaAdapter

logger = logging.getLogger(__name__)

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


def time_window_clause(window: tuple[Any, Any] | None) -> str:
    """生成「WHERE created_at BETWEEN '...' AND '...'」子句，window=None 时返回空串。

    用法：evaluator 把生成的 SQL 末尾 append 这个串。例：
        sql = f'SELECT ... FROM "{table}"'
        sql += time_window_clause(time_window)

    约定：业务表必须有 created_at 列；缺列的表会让 SQL 报错，由 evaluator 异常路径
    转成 EvaluationResult.status=ERROR 带 message（不静默吞错）。
    """
    if window is None:
        return ""
    start, end = window
    # isoformat() 对 aware datetime 输出 ISO8601（含 tz offset）；PG/SQLite 直接
    # 解析。datetimes 必须带 tz（service 层保证），不带 tz 抛 ValidationError。
    return (
        f" WHERE created_at BETWEEN '{start.isoformat()}'"
        f" AND '{end.isoformat()}'"
    )


async def inject_time_window_clause(
    adapter: BusinessDbAdapter,
    table: str,
    window: tuple[Any, Any] | None,
) -> str:
    """异步版 time_window_clause：先探测业务表是否有 created_at 列。

    老业务库（Oracle/SQL Server 等）很多表没有 created_at 列；硬加
    `WHERE created_at BETWEEN ...` 会让 evaluator 整体 ERROR（ORA-00904 等）。
    这里探测到缺列时跳过 WHERE 子句，退化为全表扫描，与 time_window=None 同语义。

    探测结果走进程内缓存（has_created_at_column），每个 table 只探测一次。

    用法（替代 time_window_clause）：
        sql += await inject_time_window_clause(adapter, table, time_window)
    """
    if window is None:
        return ""
    if not await has_created_at_column(adapter, table):
        logger.warning(
            "业务表 %s 缺 created_at 列，time_window=%s 时跳过 WHERE 子句（全表扫描）",
            table, window,
        )
        return ""
    return time_window_clause(window)


# 业务表是否包含 created_at 列的进程内缓存（feat-dq-evaluation-report）。
# 第一次探测失败后写 False 缓存，永久跳过；用于老业务表没 created_at 列的兼容。
_TABLE_HAS_CREATED_AT: dict[str, bool] = {}


def reset_table_capability_cache() -> None:
    """清空业务表能力缓存（仅测试用）。"""
    _TABLE_HAS_CREATED_AT.clear()


async def has_created_at_column(adapter: BusinessDbAdapter, table: str) -> bool:
    """探测业务表是否有 created_at 列；缓存结果。

    探测 SQL：`SELECT created_at FROM "table" WHERE 1=0 LIMIT 1`（PG 风格）——PG 在
    列不存在时抛 UndefinedColumnError；SQLite 同理。成功 = 列存在。

    注：探测 SQL 的表名按 adapter dialect 自适应引号（MySQL 反引号），见
    [[feat-dialect-quoting]] / quote_identifier。
    """
    if table in _TABLE_HAS_CREATED_AT:
        return _TABLE_HAS_CREATED_AT[table]
    safe_table = validate_identifier(table, role="target_table")
    try:
        await adapter.execute_read_only(
            f"SELECT created_at FROM {quote_identifier(adapter, safe_table)} WHERE 1=0 LIMIT 1"
        )
        _TABLE_HAS_CREATED_AT[table] = True
        return True
    except Exception:  # noqa: BLE001 - 探测失败 = 列不存在
        _TABLE_HAS_CREATED_AT[table] = False
        return False


def quote_identifier(adapter: BusinessDbAdapter, name: str) -> str:
    """按 adapter dialect 给 SQL 标识符（表/列名）加正确的引号。

    - Oracle / PostgreSQL：双引号 `"name"`（保留大小写敏感语义）
    - MySQL：反引号 `` `name` ``（ANSI_QUOTES 未开时的默认）

    适配器既非 _OracleAdapter 也非 _SqlaAdapter（如测试 stub）时按 PG 风格默认双引号，
    保持既有单测契约不破。

    衍生 bug 修复：data_quality_evaluators 早期硬编码双引号，MySQL 数据源执行报
    `(pymysql 1064) syntax error`，13 条 COMPLETENESS 规则全 ERROR 静默 3 个月。
    参见 [[silent-failure-hunter]] 复盘。
    """
    if isinstance(adapter, _OracleAdapter):
        return f'"{name}"'
    if isinstance(adapter, _SqlaAdapter):
        if getattr(adapter, "dialect", None) == "mysql":
            return f"`{name}`"
        return f'"{name}"'
    return f'"{name}"'


def quote_qualified_name(adapter: BusinessDbAdapter, *parts: str) -> str:
    """给限定名（`db.tbl.col` 之类）每段加正确引号，再用 `.` 连接。

    等价于 `quote_identifier(adapter, parts[0]) + "." + quote_identifier(adapter, parts[1]) + ...`
    """
    return ".".join(quote_identifier(adapter, p) for p in parts)


def sample_limit_clause(adapter: BusinessDbAdapter, limit: int) -> str:
    """采样 LIMIT 子句（按 adapter dialect 自适应）。

    - Oracle：返回 `` AND ROWNUM <= N``（必须 AND 进去——5 个 sampler 的 collect_violation_samples
      主体 SQL 都已带 WHERE 谓词，所以直接拼到末尾安全）。
    - 其它（PostgreSQL / SQL Server / MySQL / openGauss）：返回 `` LIMIT N``。

    limit <= 0 时返回空串（=不限）。

    sample 上限默认值见 ``app.services.data_quality_violation_sample_service._DEFAULT_PER_RULE_LIMIT``。

    变更背景：早期版本固定写 `` LIMIT N``，Oracle 报 ``ORA-00933: SQL 命令未正确结束``，
    dispatcher 又有 blanket ``except Exception: return []`` 把整批采样吞掉，报告里
    「规则全 FAIL 但无一条违规样本」—— 静默 3 个月没暴露（参见 [[silent-failure-hunter]]）。
    """
    if limit <= 0:
        return ""
    if isinstance(adapter, _OracleAdapter):
        return f" AND ROWNUM <= {int(limit)}"
    return f" LIMIT {int(limit)}"


__all__ = [
    "validate_identifier",
    "validate_expression",
    "run_read_only",
    "time_window_clause",
    "inject_time_window_clause",
    "has_created_at_column",
    "reset_table_capability_cache",
    "sample_limit_clause",
    "quote_identifier",
    "quote_qualified_name",
]