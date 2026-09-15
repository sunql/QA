"""VALIDITY 评估器：检查目标列是否满足合法表达式。"""

from __future__ import annotations

from typing import Any

from app.domain.models import DataQualityRule
from app.infrastructure.business_db_pool import BusinessDbAdapter
from app.services.data_quality_evaluators._common import (
    inject_time_window_clause,
    quote_identifier,
    sample_limit_clause,
    validate_expression,
    validate_identifier,
)
from app.services.messages_zh import MSG_DQ_EVAL_RULE_EXPRESSION_REQUIRED


async def evaluate(
    rule: DataQualityRule,
    adapter: BusinessDbAdapter,
    time_window: tuple[Any, Any] | None = None,
) -> tuple[int, int]:
    """评估 VALIDITY 规则。

    期望 rule.rule_expression 是 SQL 谓词（如 `ORDER_QTY > 0`），统计满足谓词的比例。
    SQL: SELECT COUNT(*) AS total,
                SUM(CASE WHEN <expr> THEN 1 ELSE 0 END) AS passed
         FROM <table>
         [WHERE created_at BETWEEN ...]
    返回 (total_count, passed_count)。
    """
    if not rule.rule_expression:
        raise ValueError(MSG_DQ_EVAL_RULE_EXPRESSION_REQUIRED.format(ruleType=rule.rule_type))
    table = validate_identifier(rule.target_table, role="target_table")
    expr = validate_expression(rule.rule_expression)
    q_tbl = quote_identifier(adapter, table)
    sql = (
        f"SELECT COUNT(*) AS total, "
        f"SUM(CASE WHEN ({expr}) THEN 1 ELSE 0 END) AS passed "
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
    """取 VALIDITY 规则的 top-N 违规样本（谓词为假的行）。"""
    if not rule.rule_expression:
        raise ValueError(MSG_DQ_EVAL_RULE_EXPRESSION_REQUIRED.format(ruleType=rule.rule_type))
    table = validate_identifier(rule.target_table, role="target_table")
    column = validate_identifier(rule.target_column or "row_id", role="target_column")
    expr = validate_expression(rule.rule_expression)
    q_col = quote_identifier(adapter, column)
    q_tbl = quote_identifier(adapter, table)
    sql = (
        f"SELECT {q_col} AS row_id FROM {q_tbl} "
        f"WHERE NOT ({expr}) OR ({expr}) IS NULL"
    )
    # feat-sampler-3vl-fix (2026-09-15)：补 IS NULL 分支——SQL 三值逻辑下
    # NOT(NULL)=NULL，evaluator 用 CASE WHEN expr THEN 1 ELSE 0 END 把 NULL 行
    # 计为不通过，但旧 sampler 的 WHERE NOT(expr) 漏掉 NULL 行——
    # 「报告违规 568 条但采样 0 条」的根因。对齐 evaluator 语义。
    sql += await inject_time_window_clause(adapter, table, time_window)
    sql += sample_limit_clause(adapter, limit)
    rows = await adapter.execute_read_only(sql)
    return [
        {"row_id": r.get("row_id"), "target_column": column, "target_table": table}
        for r in rows
    ]