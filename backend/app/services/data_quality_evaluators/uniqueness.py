"""UNIQUENESS 评估器：检查目标列值的重复率。"""

from __future__ import annotations

from app.domain.models import DataQualityRule
from app.infrastructure.business_db_pool import BusinessDbAdapter
from app.services.data_quality_evaluators._common import validate_identifier
from app.services.messages_zh import MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED


async def evaluate(rule: DataQualityRule, adapter: BusinessDbAdapter) -> tuple[int, int]:
    """评估 UNIQUENESS 规则。

    返回 (total_count, passed_count)；passed_count = 唯一值个数。
    SQL: SELECT COUNT(*) AS total, COUNT(DISTINCT <column>) AS passed FROM <table>

    注：本评估仅考虑非空列的唯一性；NULL 不计入重复（NULL 与 NULL 不视作相等）。
    完整 unique（含空值约束）通常需要组合 COMPLETENESS + UNIQUENESS 两规则覆盖。
    """
    if not rule.target_column:
        raise ValueError(MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED.format(ruleType=rule.rule_type))
    table = validate_identifier(rule.target_table, role="target_table")
    column = validate_identifier(rule.target_column, role="target_column")
    sql = (
        f'SELECT COUNT(*) AS total, '
        f'COUNT(DISTINCT "{column}") AS passed '
        f'FROM "{table}"'
    )
    rows = await adapter.execute_read_only(sql)
    if not rows:
        return 0, 0
    row = rows[0]
    total = int(row.get("total") or 0)
    passed = int(row.get("passed") or 0)
    return total, passed