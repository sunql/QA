"""feat-dq-rule-params (2026-09-15)

data_quality_rule 新增 rule_params JSONB 可空列：给隔离的新特性存结构化规则参数。

语义约定（本迁移不强制，由上层 service 约定）：
- NULL = legacy 自定义 SQL 模式（rule_expression 生效）
- 非 NULL = 结构化模式（rule_params 生效）

存量规则零迁移：列可空，旧行不写即为 NULL。
评估路径（evaluator/dispatcher）本迁移不变更。

文件名：0076_data_quality_rule_params.py = 29 字符（≤32 字符 OK，参见
[[alembic-version-filename-32-char-limit]]）。

Revision ID: 0076
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "0076_data_quality_rule_params"
down_revision: str | None = "0075_dq_sample_error"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 添加列；可空，老行不强制填。JSONB 与 ontology_property.allowed_values /
    # evaluation_report.progress 风格一致（postgresql.JSONB）。
    op.add_column(
        "data_quality_rule",
        sa.Column(
            "rule_params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.execute(
        "COMMENT ON COLUMN data_quality_rule.rule_params IS "
        "'结构化规则参数；NULL=自定义SQL模式(legacy)，非NULL=结构化模式'"
    )


def downgrade() -> None:
    op.drop_column("data_quality_rule", "rule_params")
