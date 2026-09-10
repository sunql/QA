"""Add routing metrics fields to session_message - Task 5.2.

Adds three nullable columns to session_message for 4-layer routing monitoring:
- routing_layer: L1/L2/L3/L4 routing layer identifier
- latency_ms: response latency in milliseconds
- token_cost_usd: token cost in USD

Revision ID: 0051
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0051_routing_metrics"
down_revision: str | None = "0050_kpi_semantic_kw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session_message",
        sa.Column("routing_layer", sa.String(10), nullable=True),
    )
    op.add_column(
        "session_message",
        sa.Column("latency_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "session_message",
        sa.Column("token_cost_usd", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_message", "token_cost_usd")
    op.drop_column("session_message", "latency_ms")
    op.drop_column("session_message", "routing_layer")
