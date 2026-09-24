"""CONSISTENCY 评估器：检查跨列/表的一致性约束。

与 VALIDITY 共用 `rule_expression`（SQL 谓词），但语义上是跨列一致性，
如 `RECEIVED_QTY <= ORDER_QTY * 1.05`。
"""

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
    """评估 CONSISTENCY 规则。"""
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
    """取 CONSISTENCY 规则的 top-N 违规样本。

    CONSISTENCY 没有固定 target_column（谓词可能跨多列），按 target_table 投影一个
    通用 row_id：选 `<table>_KEY` 常见命名；如果失败退到任意 `id` / `row_number()`。
    """
    if not rule.rule_expression:
        raise ValueError(MSG_DQ_EVAL_RULE_EXPRESSION_REQUIRED.format(ruleType=rule.rule_type))
    table = validate_identifier(rule.target_table, role="target_table")
    expr = validate_expression(rule.rule_expression)
    # 用目标表的常规主键列名投影；若用户配置了 target_column 就用之
    pk_col = rule.target_column or f"{table}_KEY"
    try:
        validate_identifier(pk_col, role="target_column")
    except Exception:  # noqa: BLE001
        pk_col = "row_id"
    q_col = quote_identifier(adapter, pk_col)
    q_tbl = quote_identifier(adapter, table)
    sql = (
        f"SELECT {q_col} AS row_id FROM {q_tbl} "
        f"WHERE NOT ({expr}) OR ({expr}) IS NULL"
    )
    # feat-sampler-3vl-fix (2026-09-15)：同 validity.py——SQL 三值逻辑下
    # NOT(NULL)=NULL，evaluator 的 CASE WHEN expr THEN 1 ELSE 0 END 把 NULL 行
    # 计为不通过；旧 sampler 漏掉 NULL 行。对齐 evaluator 语义。
    sql += await inject_time_window_clause(adapter, table, time_window)
    sql += sample_limit_clause(adapter, limit)
    rows = await adapter.execute_read_only(sql)
    return [
        {"row_id": r.get("row_id"), "target_column": pk_col, "target_table": table}
        for r in rows
    ]