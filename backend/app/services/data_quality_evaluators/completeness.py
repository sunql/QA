"""COMPLETENESS 评估器：检查目标列非空比例。"""

from __future__ import annotations

from typing import Any

from app.domain.models import DataQualityRule
from app.infrastructure.business_db_pool import BusinessDbAdapter
from app.services.data_quality_evaluators._common import (
    inject_time_window_clause,
    quote_identifier,
    sample_limit_clause,
    validate_identifier,
)
from app.services.messages_zh import MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED


async def evaluate(
    rule: DataQualityRule,
    adapter: BusinessDbAdapter,
    time_window: tuple[Any, Any] | None = None,
) -> tuple[int, int]:
    """评估 COMPLETENESS 规则。

    返回 (total_count, passed_count)；passed_count = 非空行数。
    SQL: SELECT COUNT(*) AS total, COUNT(<column>) AS passed FROM <table>
         [WHERE created_at BETWEEN ...]    -- 仅当 time_window 非 None
    """
    if not rule.target_column:
        raise ValueError(MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED.format(ruleType=rule.rule_type))
    table = validate_identifier(rule.target_table, role="target_table")
    column = validate_identifier(rule.target_column, role="target_column")
    q_col = quote_identifier(adapter, column)
    q_tbl = quote_identifier(adapter, table)
    sql = f"SELECT COUNT(*) AS total, COUNT({q_col}) AS passed FROM {q_tbl}"
    sql += await inject_time_window_clause(adapter, table, time_window)
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
    """取 COMPLETENESS 规则的 top-N 违规样本（target_column IS NULL 的行）。

    返回 [ {"row_id": <value>, "target_column": "<col>", "target_table": "<tbl>"}, ... ]
    """
    if not rule.target_column:
        raise ValueError(MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED.format(ruleType=rule.rule_type))
    table = validate_identifier(rule.target_table, role="target_table")
    column = validate_identifier(rule.target_column, role="target_column")
    q_col = quote_identifier(adapter, column)
    q_tbl = quote_identifier(adapter, table)
    sql = f"SELECT {q_col} AS row_id FROM {q_tbl} WHERE {q_col} IS NULL"
    sql += await inject_time_window_clause(adapter, table, time_window)
    sql += sample_limit_clause(adapter, limit)
    rows = await adapter.execute_read_only(sql)
    return [
        {"row_id": r.get("row_id"), "target_column": column, "target_table": table}
        for r in rows
    ]