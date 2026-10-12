"""add research_run_state

Revision ID: b1ea6cb32588
Revises: 0117
Create Date: 2026-10-12 20:16:46.933826
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b1ea6cb32588'
down_revision: Union[str, None] = '0117'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'research_run_state',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('session_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('turn_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('parent_run_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('current_question', sa.Text(), server_default="", nullable=False),
        sa.Column('mode', sa.String(length=20), nullable=False),
        sa.Column('scope_version', sa.Integer(), server_default="1", nullable=False),
        sa.Column('datasource_id', sa.Integer(), nullable=True),
        sa.Column('schema_version', sa.Integer(), server_default="1", nullable=False),
        sa.Column('model_id', sa.Integer(), nullable=True),
        sa.Column('state_payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['parent_run_id'], ['research_run_state.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['session_id'], ['research_session.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['turn_id'], ['research_turn.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('research_run_state_pkey')),
    )
    op.create_index(op.f('ix_research_run_state_session_id'), 'research_run_state', ['session_id'], unique=False)
    op.create_index(op.f('ix_research_run_state_turn_id'), 'research_run_state', ['turn_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_research_run_state_turn_id'), table_name='research_run_state')
    op.drop_index(op.f('ix_research_run_state_session_id'), table_name='research_run_state')
    op.drop_table('research_run_state')
