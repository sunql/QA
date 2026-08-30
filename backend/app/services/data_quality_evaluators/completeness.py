"""COMPLETENESS 评估器：检查目标列非空比例。"""

from __future__ import annotations

from app.domain.models import DataQualityRule
from app.infrastructure.business_db_pool import BusinessDbAdapter
from app.services.data_quality_evaluators._common import (
    run_read_only,
    validate_identifier,
)
from app.services.messages_zh import MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED


async def evaluate(rule: DataQualityRule, adapter: BusinessDbAdapter) -> tuple[int, int]:
    """评估 COMPLETENESS 规则。

    返回 (total_count, passed_count)；passed_count = 非空行数。
    SQL: SELECT COUNT(*) AS total, COUNT(<column>) AS passed FROM <table>
    """
    if not rule.target_column:
        raise ValueError(MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED.format(ruleType=rule.rule_type))
    table = validate_identifier(rule.target_table, role="target_table")
    column = validate_identifier(rule.target_column, role="target_column")
    sql = f'SELECT COUNT(*) AS total, COUNT("{column}") AS passed FROM "{table}"'
    rows = await adapter.execute_read_only(sql)
    if not rows:
        return 0, 0
    row = rows[0]
    total = int(row.get("total") or 0)
    passed = int(row.get("passed") or 0)
    return total, passed