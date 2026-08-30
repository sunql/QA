"""VALIDITY 评估器：检查目标列是否满足合法表达式。"""

from __future__ import annotations

from app.domain.models import DataQualityRule
from app.infrastructure.business_db_pool import BusinessDbAdapter
from app.services.data_quality_evaluators._common import (
    validate_expression,
    validate_identifier,
)
from app.services.messages_zh import MSG_DQ_EVAL_RULE_EXPRESSION_REQUIRED


async def evaluate(rule: DataQualityRule, adapter: BusinessDbAdapter) -> tuple[int, int]:
    """评估 VALIDITY 规则。

    期望 rule.rule_expression 是 SQL 谓词（如 `ORDER_QTY > 0`），统计满足谓词的比例。
    SQL: SELECT COUNT(*) AS total,
                SUM(CASE WHEN <expr> THEN 1 ELSE 0 END) AS passed
         FROM <table>
    返回 (total_count, passed_count)。
    """
    if not rule.rule_expression:
        raise ValueError(MSG_DQ_EVAL_RULE_EXPRESSION_REQUIRED.format(ruleType=rule.rule_type))
    table = validate_identifier(rule.target_table, role="target_table")
    expr = validate_expression(rule.rule_expression)
    sql = (
        f'SELECT COUNT(*) AS total, '
        f'SUM(CASE WHEN ({expr}) THEN 1 ELSE 0 END) AS passed '
        f'FROM "{table}"'
    )
    rows = await adapter.execute_read_only(sql)
    if not rows:
        return 0, 0
    row = rows[0]
    total = int(row.get("total") or 0)
    passed = int(row.get("passed") or 0)
    return total, passed