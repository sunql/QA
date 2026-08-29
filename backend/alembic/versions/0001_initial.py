"""initial schema: llm_config and session_token_usage

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-11
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # llm_config
    op.create_table(
        "llm_config",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("model_name", sa.String(length=50), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("api_endpoint", sa.String(length=255), nullable=True),
        sa.Column("api_key_encrypted", sa.String(length=512), nullable=True),
        sa.Column("cost_per_1k_input", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("cost_per_1k_output", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("cost_threshold", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_name", name="uq_llm_config_model_name"),
    )

    # session_token_usage
    op.create_table(
        "session_token_usage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("model_config_id", sa.BigInteger(), nullable=True),
        sa.Column("model_name", sa.String(length=50), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("request_time", sa.DateTime(), nullable=False),
        sa.Column("purpose", sa.String(length=50), nullable=True),
        sa.ForeignKeyConstraint(["model_config_id"], ["llm_config.id"], name="fk_usage_model_config"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_session_time", "session_token_usage", ["session_id", "request_time"])
    op.create_index("idx_model_time", "session_token_usage", ["model_config_id", "request_time"])


def downgrade() -> None:
    op.drop_index("idx_model_time", table_name="session_token_usage")
    op.drop_index("idx_session_time", table_name="session_token_usage")
    op.drop_table("session_token_usage")
    op.drop_table("llm_config")
