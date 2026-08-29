"""term_dictionary - NL2SQL 术语字典表。

用户在对答中发现 LLM 误解术语（如"实际到货""订的数量""占比"）时，可将术语的
真实含义、映射的本体类/属性、以及结构性提示（如占比公式）录入此表；计划阶段
将其渲染进 prompt，帮助 LLM 下次正确理解。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_term_dictionary"
down_revision: str | None = "0014_embedding_provider_active"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "term_dictionary",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("term", sa.String(length=100), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("mapped_class_name", sa.String(length=100), nullable=True),
        sa.Column("mapped_property_name", sa.String(length=100), nullable=True),
        sa.Column("formula_hint", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 与模型 __table_args__ 一致，避免 autogenerate 漂移
        sa.UniqueConstraint("term", name="uq_term_dictionary_term"),
    )


def downgrade() -> None:
    op.drop_table("term_dictionary")
