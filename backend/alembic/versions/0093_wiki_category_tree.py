"""wiki_category 树形分类（feat-wiki-category）。

新增 wiki_category 表：自引用 parent_id + sort_order + 可选 page_id 概览页。
ON DELETE SET NULL 让删除父分类时子分类自动升级为根，避免雪崩。
复合索引 (parent_id, sort_order) 是 tree 端点主路径。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0093_wiki_category_tree"
down_revision = "0092_fix_uq_wol_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wiki_category",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "parent_id",
            sa.BigInteger(),
            sa.ForeignKey("wiki_category.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "page_id",
            sa.String(length=64),
            sa.ForeignKey("wiki_page.page_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_wiki_category_parent_sort",
        "wiki_category",
        ["parent_id", "sort_order"],
    )
    op.create_index(
        "ix_wiki_category_page",
        "wiki_category",
        ["page_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_wiki_category_page", table_name="wiki_category")
    op.drop_index("ix_wiki_category_parent_sort", table_name="wiki_category")
    op.drop_table("wiki_category")