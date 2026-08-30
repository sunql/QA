"""feature_definition_history — FeatureDefinition 历史快照（Phase 4.5 治理加固）。

对齐 kpi_catalog_history 模式：feature_definition 删除时 history 保留（FK ON DELETE SET NULL），
snapshot_json 存完整定义快照，changed_by/changed_at 记录变更人与时间。

与 audit_log 的区别：
- audit_log：通用审计，捕获变更前后差异（before/after JSON）
- feature_definition_history：实体专用历史，存完整快照，可按时间回放

回滚：drop table 即可，不影响 feature_definition 数据。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028_feature_definition_history"
down_revision: str | None = "0027_ai_feature"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feature_definition_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("feature_id", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_json", postgresql.JSONB(), nullable=False),
        sa.Column("changed_by", sa.String(length=50), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["feature_id"],
            ["feature_definition.id"],
            name="fk_feature_def_history_feature",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_feature_def_history_feature_changed",
        "feature_definition_history",
        ["feature_id", sa.text("changed_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_feature_def_history_feature_changed",
        table_name="feature_definition_history",
    )
    op.drop_table("feature_definition_history")
