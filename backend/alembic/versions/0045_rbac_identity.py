"""add RBAC identity & permission tables (feat-rbac-identity)

Revision ID: 0045_rbac_identity
Revises: 0044_dq_rule_auto_generation
Create Date: 2026-09-07

Why: 系统需要真正的身份与权限底座（此前认证是 HTTP 头桩、无持久化 user/role/
org/permission）。6 张表：users / roles / organizations + user_roles /
user_organizations 多对多关联 + permission_grant（多态主体 USER/ROLE/
ORGANIZATION 对 menu_config.code 的菜单授权，FK 级联删菜单清授权）。

数据权限扩展预留：permission_grant 的多态主体模型与合集语义可在后续复用于
data_permission_grant（resource 以本体类/属性/指标为基准）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0045_rbac_identity"
down_revision = "0044_dq_rule_auto_generation"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    ]


def upgrade() -> None:
    # 1) 三类主体
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index("ix_users_enabled", "users", ["enabled"])

    op.create_table(
        "roles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_roles_code"),
    )

    op.create_table(
        "organizations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_organizations_code"),
    )
    op.create_index("ix_organizations_parent", "organizations", ["parent_id"])
    op.create_foreign_key(
        "fk_organizations_parent_self",
        "organizations",
        "organizations",
        ["parent_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 2) 多对多关联
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "user_organizations",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "organization_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
    )

    # 3) 权限授予（多态主体 → menu_config.code，删菜单级联清授权）
    op.create_table(
        "permission_grant",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("menu_code", sa.String(length=64), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "menu_code", "subject_type", "subject_id",
            name="uq_permission_grant_scope",
        ),
        sa.CheckConstraint(
            "subject_type IN ('USER', 'ROLE', 'ORGANIZATION')",
            name="ck_permission_grant_subject_type",
        ),
    )
    op.create_index(
        "ix_permission_grant_subject", "permission_grant", ["subject_type", "subject_id"]
    )
    op.create_index("ix_permission_grant_menu", "permission_grant", ["menu_code"])
    op.create_foreign_key(
        "fk_permission_grant_menu",
        "permission_grant",
        "menu_config",
        ["menu_code"],
        ["code"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_table("permission_grant")
    op.drop_table("user_organizations")
    op.drop_table("user_roles")
    op.drop_table("organizations")
    op.drop_table("roles")
    op.drop_table("users")
