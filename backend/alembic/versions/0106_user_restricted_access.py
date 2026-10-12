"""user.restricted_access 字段（Task 4.2：audit-fx-4.2）。

添加 restricted_access BOOLEAN NOT NULL DEFAULT false 到 users 表。
L2 文档访问权限标记：admin PATCH /api/v1/users/{id} 管控。

Revision ID: 0106
Revises: 0105
Create Date: 2026-10-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: skip if column already exists (partial prior run)
    bind = op.get_context().bind
    if not bind.dialect.has_table(bind, "users"):
        return
    col_names = [col["name"] for col in bind.dialect.get_columns(bind, "users")]
    if "restricted_access" in col_names:
        return
    op.add_column(
        "users",
        sa.Column(
            "restricted_access",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "restricted_access", if_exists=True)
