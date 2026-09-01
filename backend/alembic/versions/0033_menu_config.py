"""menu_config - 菜单配置表（Phase X）。

存 6 个一级类 + 20 个叶子项，自引用父子关系。
permissionCode / roles 字段预留，本期不消费。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_menu_config"
down_revision: str | None = "0032_agent_schedule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "menu_config",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("label_key", sa.String(length=128), nullable=False),
        sa.Column("path", sa.String(length=256), nullable=True),
        sa.Column("icon_code", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("permission_code", sa.String(length=64), nullable=True),
        sa.Column("roles", sa.String(length=512), nullable=True),
        sa.Column(
            "visible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_menu_config_code"),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["menu_config.id"],
            name="fk_menu_config_parent",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_menu_config_parent", "menu_config", ["parent_id"])
    op.create_index(
        "ix_menu_config_parent_sort",
        "menu_config",
        ["parent_id", "sort_order", "id"],
    )
    op.create_index(
        "ix_menu_config_visible_sort",
        "menu_config",
        ["visible", "sort_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_menu_config_visible_sort", table_name="menu_config")
    op.drop_index("ix_menu_config_parent_sort", table_name="menu_config")
    op.drop_index("ix_menu_config_parent", table_name="menu_config")
    op.drop_table("menu_config")