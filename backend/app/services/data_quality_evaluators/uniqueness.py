"""UNIQUENESS 评估器：检查目标列值的重复率。"""

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
    """评估 UNIQUENESS 规则。

    返回 (total_count, passed_count)；passed_count = 唯一值个数。
    SQL: SELECT COUNT(*) AS total, COUNT(DISTINCT <column>) AS passed FROM <table>
         [WHERE created_at BETWEEN ...]
    """
    if not rule.target_column:
        raise ValueError(MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED.format(ruleType=rule.rule_type))
    table = validate_identifier(rule.target_table, role="target_table")
    column = validate_identifier(rule.target_column, role="target_column")
    q_col = quote_identifier(adapter, column)
    q_tbl = quote_identifier(adapter, table)
    sql = (
        f"SELECT COUNT(*) AS total, "
        f"COUNT(DISTINCT {q_col}) AS passed "
        f"FROM {q_tbl}"
    )
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
    """取 UNIQUENESS 规则的 top-N 重复值样本。

    先取 COUNT > 1 的重复值集合，再随机抽 N 行（用 target_column 在结果集里的偏移；
    真正的随机抽 LIMIT 在内层子查询外层很难实现，简化为按出现顺序取前 N 行）。
    """
    if not rule.target_column:
        raise ValueError(MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED.format(ruleType=rule.rule_type))
    table = validate_identifier(rule.target_table, role="target_table")
    column = validate_identifier(rule.target_column, role="target_column")
    q_col = quote_identifier(adapter, column)
    q_tbl = quote_identifier(adapter, table)
    # 取前 N 个重复值（按出现频次降序），再投影原行
    sql = (
        f"SELECT {q_col} AS row_id FROM {q_tbl} "
        f"WHERE {q_col} IN ("
        f"SELECT {q_col} FROM {q_tbl} "
        f"GROUP BY {q_col} HAVING COUNT(*) > 1"
        f")"
    )
    sql += await inject_time_window_clause(adapter, table, time_window)
    sql += sample_limit_clause(adapter, limit)
    rows = await adapter.execute_read_only(sql)
    return [
        {"row_id": r.get("row_id"), "target_column": column, "target_table": table}
        for r in rows
    ]