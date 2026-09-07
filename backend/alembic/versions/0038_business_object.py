"""新建 business_object 表（Phase 4 业务对象注册表 SSOT）."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_business_object"
down_revision = "0037_agent_tool_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_object",
        sa.Column("code", sa.String(20), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column(
            "header_class_id",
            sa.BigInteger(),
            sa.ForeignKey("ontology_class.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("graph_label", sa.String(100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(50), nullable=True),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "code = UPPER(code)", name="ck_business_object_code_upper"
        ),
    )
    op.create_index(
        "ix_business_object_header_class",
        "business_object",
        ["header_class_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_business_object_header_class", table_name="business_object")
    op.drop_table("business_object")
