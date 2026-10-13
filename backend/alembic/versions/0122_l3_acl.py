"""l3_acl 表（Task 4.1：audit-fx-4.1）。

L3 机密文档访问控制白名单表：
- id BIGSERIAL PK
- user_id FK → users.id，index=True
- resource_pattern String(255) — 模糊匹配（dataset:* 等）
- granted_by FK → users.id（记录授权人）
- granted_at DateTime server_default=func.now()

Revision ID: 0122
Revises: 0121
Create Date: 2026-10-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: skip if already applied (partial prior run left table but not rev)
    if not op.get_context().bind.dialect.has_table(
        op.get_context().bind, "l3_acl"
    ):
        op.create_table(
            "l3_acl",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("resource_pattern", sa.String(255), nullable=False),
            sa.Column("granted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column(
                "granted_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    # Idempotent: drop index only if it exists (was created by index=True)
    op.drop_index("ix_l3_acl_user_id", table_name="l3_acl", if_exists=True)
    op.drop_table("l3_acl", if_exists=True)
