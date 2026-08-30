"""REFERENTIAL 评估器：检查目标列在引用表中是否存在。

rule_expression 必须形如 `REF <ref_table>.<ref_column>`：
- 例：`REF SUPPLIER.SUPPLIER_KEY` 表示目标列必须能在 SUPPLIER.SUPPLIER_KEY 中查到。
- 解析后的 ref_table / ref_column 同样走 identifier 白名单。
"""

from __future__ import annotations

import re

from app.domain.exceptions import ValidationError
from app.domain.models import DataQualityRule
from app.infrastructure.business_db_pool import BusinessDbAdapter
from app.services.data_quality_evaluators._common import validate_identifier
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


async def evaluate(rule: DataQualityRule, adapter: BusinessDbAdapter) -> tuple[int, int]:
    """评估 REFERENTIAL 规则。

    返回 (total_count, passed_count)；passed_count = 在引用表中能匹配到的非空行数。
    SQL:
      SELECT COUNT(*) AS total,
             SUM(CASE WHEN <col> IS NOT NULL AND EXISTS (
                 SELECT 1 FROM <ref_table> WHERE <ref_table>.<ref_col> = <table>.<col>
             ) THEN 1 ELSE 0 END) AS passed
      FROM <table>
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

    sql = (
        f'SELECT COUNT(*) AS total, '
        f'SUM(CASE WHEN "{column}" IS NOT NULL AND EXISTS ('
        f'SELECT 1 FROM "{ref_table}" WHERE "{ref_table}"."{ref_column}" = "{table}"."{column}"'
        f') THEN 1 ELSE 0 END) AS passed '
        f'FROM "{table}"'
    )
    rows = await adapter.execute_read_only(sql)
    if not rows:
        return 0, 0
    row = rows[0]
    total = int(row.get("total") or 0)
    passed = int(row.get("passed") or 0)
    return total, passed