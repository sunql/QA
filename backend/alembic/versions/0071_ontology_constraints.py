"""ontology_property - 约束字段（feat-ontology-property-constraints）。

为 ontology_property 增加：
- is_not_null     ：BOOL    ，NULL = 未约束；True = 推导 COMPLETENESS 规则
- min_value       ：VARCHAR(50)  ，范围下界；与 max_value 配套，单独存无意义
- max_value       ：VARCHAR(50)  ，范围上界
- regex_pattern   ：VARCHAR(255) ，正则；推导 PATTERN 规则

四列均可空；与既有 allowed_values 并存（多约束合法）。无 default，历史
数据无需 backfill。LLM prompt 同步支持四种 kind 输出（allowed_values /
not_null / range / pattern），apply-suggestion service 按 kind 派发写入。

Revision ID: 0071
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0071_ontology_constraints"
down_revision: str | None = "0070_community_topic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ontology_property",
        sa.Column("is_not_null", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "ontology_property",
        sa.Column("min_value", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "ontology_property",
        sa.Column("max_value", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "ontology_property",
        sa.Column("regex_pattern", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ontology_property", "regex_pattern")
    op.drop_column("ontology_property", "max_value")
    op.drop_column("ontology_property", "min_value")
    op.drop_column("ontology_property", "is_not_null")
