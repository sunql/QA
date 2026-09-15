"""REFERENTIAL 评估器：检查目标列在引用表中是否存在。

rule_expression 必须形如 `REF <ref_table>.<ref_column>`：
- 例：`REF SUPPLIER.SUPPLIER_KEY` 表示目标列必须能在 SUPPLIER.SUPPLIER_KEY 中查到。
- 解析后的 ref_table / ref_column 同样走 identifier 白名单。
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.exceptions import ValidationError
from app.domain.models import DataQualityRule
from app.infrastructure.business_db_pool import BusinessDbAdapter
from app.services.data_quality_evaluators._common import (
    has_created_at_column,
    quote_qualified_name,
    quote_identifier,
    sample_limit_clause,
    validate_identifier,
)
from app.services.messages_zh import (
    MSG_DQ_EVAL_INVALID_REF_FORMAT,
    MSG_DQ_EVAL_RULE_EXPRESSION_REQUIRED,
    MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED,
)

_REF_RE = re.compile(
    r"^\s*REF\s+([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*$",
    re.IGNORECASE,
)


def parse_ref_expression(expression: str) -> tuple[str, str]:
    """解析 `REF <ref_table>.<ref_column>`，返回 (ref_table, ref_column)。"""
    m = _REF_RE.match(expression or "")
    if not m:
        raise ValidationError(MSG_DQ_EVAL_INVALID_REF_FORMAT.format(value=expression))
    return m.group(1), m.group(2)


async def evaluate(
    rule: DataQualityRule,
    adapter: BusinessDbAdapter,
    time_window: tuple[Any, Any] | None = None,
) -> tuple[int, int]:
    """评估 REFERENTIAL 规则。

    返回 (total_count, passed_count)；passed_count = 在引用表中能匹配到的非空行数。
    SQL（无 time_window）：
      SELECT COUNT(*) AS total,
             SUM(CASE WHEN <col> IS NOT NULL AND EXISTS (
                 SELECT 1 FROM <ref_table> WHERE <ref_table>.<ref_col> = <table>.<col>
             ) THEN 1 ELSE 0 END) AS passed
      FROM <table>

    SQL（有 time_window）：把主表扫过滤前置到 CTE，避免 EXISTS 子查询里再叠 WHERE：
      WITH filtered AS (
          SELECT "<col>" AS fk_col
          FROM "<table>"
          WHERE created_at BETWEEN '...' AND '...'
      )
      SELECT COUNT(*) AS total,
             SUM(CASE WHEN fk_col IS NOT NULL AND EXISTS (
                 SELECT 1 FROM "<ref_table>" WHERE "<ref_table>"."<ref_col>" = filtered.fk_col
             ) THEN 1 ELSE 0 END) AS passed
      FROM filtered
    """
    if not rule.target_column:
        raise ValueError(MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED.format(ruleType=rule.rule_type))
    if not rule.rule_expression:
        raise ValueError(MSG_DQ_EVAL_RULE_EXPRESSION_REQUIRED.format(ruleType=rule.rule_type))
    table = validate_identifier(rule.target_table, role="target_table")
    column = validate_identifier(rule.target_column, role="target_column")
    ref_table, ref_column = parse_ref_expression(rule.rule_expression)
    # 已经走 IDENT_RE 再过一遍以防未来正则变松
    validate_identifier(ref_table, role="ref_table")
    validate_identifier(ref_column, role="ref_column")

    # 老业务表（如 Oracle 上的 SUPPLIER）若缺 created_at 列，time_window 退化
    # 为全表扫描，避免 ORA-00904 整体 ERROR。
    use_window = time_window is not None and await has_created_at_column(adapter, table)
    q_col = quote_identifier(adapter, column)
    q_tbl = quote_identifier(adapter, table)
    q_ref_tbl = quote_identifier(adapter, ref_table)
    q_ref_qualified = quote_qualified_name(adapter, ref_table, ref_column)
    q_main_qualified = quote_qualified_name(adapter, table, column)
    if not use_window:
        sql = (
            f"SELECT COUNT(*) AS total, "
            f"SUM(CASE WHEN {q_col} IS NOT NULL AND EXISTS ("
            f"SELECT 1 FROM {q_ref_tbl} WHERE {q_ref_qualified} = {q_main_qualified}"
            f") THEN 1 ELSE 0 END) AS passed "
            f"FROM {q_tbl}"
        )
    else:
        start, end = time_window
        # CTE 模式：把时间过滤前置，避免在嵌套 EXISTS 子查询里再加 WHERE
        sql = (
            f"WITH filtered AS ("
            f"SELECT {q_col} AS fk_col "
            f"FROM {q_tbl} "
            f"WHERE created_at BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
            f") "
            f"SELECT COUNT(*) AS total, "
            f"SUM(CASE WHEN fk_col IS NOT NULL AND EXISTS ("
            f"SELECT 1 FROM {q_ref_tbl} WHERE {q_ref_qualified} = filtered.fk_col"
            f") THEN 1 ELSE 0 END) AS passed "
            f"FROM filtered"
        )
    rows = await adapter.execute_read_only(sql)
    if not rows:
        return 0, 0
    row = rows[0]
    total = int(row.get("total") or 0)
    passed = int(row.get("passed") or 0)
    return total, passed


async def collect_violation_samples(
    rule: DataQualityRule,
    adapter: BusinessDbAdapter,
    *,
    limit: int,
    time_window: tuple[Any, Any] | None = None,
) -> list[dict[str, Any]]:
    """取 REFERENTIAL 规则的 top-N 孤立 FK 样本。

    孤立 FK 定义：非空、且在 ref_table.ref_column 中查不到对应行。
    与 evaluate() 同样的 CTE 模式以保证 EXISTS 子查询不会因为时间窗口被错误地往内层加 WHERE。
    """
    if not rule.target_column:
        raise ValueError(MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED.format(ruleType=rule.rule_type))
    if not rule.rule_expression:
        raise ValueError(MSG_DQ_EVAL_RULE_EXPRESSION_REQUIRED.format(ruleType=rule.rule_type))
    table = validate_identifier(rule.target_table, role="target_table")
    column = validate_identifier(rule.target_column, role="target_column")
    ref_table, ref_column = parse_ref_expression(rule.rule_expression)
    validate_identifier(ref_table, role="ref_table")
    validate_identifier(ref_column, role="ref_column")

    q_col = quote_identifier(adapter, column)
    q_tbl = quote_identifier(adapter, table)
    q_ref_tbl = quote_identifier(adapter, ref_table)
    q_ref_qualified = quote_qualified_name(adapter, ref_table, ref_column)
    q_main_qualified = quote_qualified_name(adapter, table, column)
    base_select = (
        f"SELECT {q_col} AS row_id FROM {q_tbl} "
        f"WHERE {q_col} IS NOT NULL AND NOT EXISTS ("
        f"SELECT 1 FROM {q_ref_tbl} WHERE {q_ref_qualified} = {q_main_qualified}"
        f")"
    )
    # 老业务表无 created_at 列时跳过 time_window（与 evaluate() 同语义）。
    use_window = time_window is not None and await has_created_at_column(adapter, table)
    if use_window:
        start, end = time_window
        sql = (
            f"WITH filtered AS ("
            f"SELECT {q_col} AS row_id "
            f"FROM {q_tbl} "
            f"WHERE created_at BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
            f") "
            f"SELECT row_id FROM filtered WHERE row_id IS NOT NULL AND NOT EXISTS ("
            f"SELECT 1 FROM {q_ref_tbl} WHERE {q_ref_qualified} = filtered.row_id"
            f")"
        )
    else:
        sql = base_select
    sql += sample_limit_clause(adapter, limit)
    rows = await adapter.execute_read_only(sql)
    return [
        {"row_id": r.get("row_id"), "target_column": column, "target_table": table}
        for r in rows
    ]