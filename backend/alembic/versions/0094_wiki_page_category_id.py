"""wiki_page.category_id（feat-wiki-category）。

新增 wiki_page.category_id 列，指向 wiki_category.id。ON DELETE SET NULL：
分类被删时 page 自动脱钩（不级联删 page，page 仍是知识资产）。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0094_wiki_page_category_id"
down_revision = "0093_wiki_category_tree"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wiki_page",
        sa.Column(
            "category_id",
            sa.BigInteger(),
            sa.ForeignKey("wiki_category.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_wiki_page_category_id",
        "wiki_page",
        ["category_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_wiki_page_category_id", table_name="wiki_page")
    op.drop_column("wiki_page", "category_id")