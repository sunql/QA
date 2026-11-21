"""M5-1：询价草稿表 + business_command.payload_hash（DRAFT-only）。

迁移号 0160（main 线 head=0152；**0153–0159 归采购线 b1–b4 与 A4-0 预留**
——迁移号串行占号纪律）。⚠️ 当前 down_revision=0152 供本分支本地开发/测试；
**合并时必须重指到彼时 head**（预计 0159 = A4-0），否则出现双 head。
本迁移不得先于 b1–b4 合并。

Revision ID: 0160
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0160"
down_revision = "0152"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "procurement_rfq_draft",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("rfq_draft_id", sa.dialects.postgresql.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "status",
            sa.String(24),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column(
            "idempotency_key", sa.String(128), nullable=False, unique=True
        ),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column(
            "created_time", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_time", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT','VALIDATING','NEEDS_CORRECTION','PENDING_APPROVAL',"
            "'READY_TO_SEND','REJECTED','CANCELLED','SENDING','SENT','CLOSED')",
            name="ck_rfq_draft_status",
        ),
    )
    op.create_index(
        "uq_rfq_draft_id", "procurement_rfq_draft",
        ["rfq_draft_id"], unique=True,
    )
    op.create_index(
        "ix_rfq_draft_request", "procurement_rfq_draft", ["request_id"]
    )
    # 幂等语义（DoD 3）：business_command 补 payload_hash
    op.add_column(
        "business_command",
        sa.Column("payload_hash", sa.String(64), nullable=True),
    )
    op.create_table(
        "procurement_rfq_decision",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "rfq_draft_id",
            sa.dialects.postgresql.UUID(),
            sa.ForeignKey("procurement_rfq_draft.rfq_draft_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "decision",
            sa.String(24),
            nullable=False,
        ),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "decided_time", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_time", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_time", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "decision IN ('SUBMIT','APPROVE','REJECT','CANCEL')",
            name="ck_rfq_decision_kind",
        ),
    )
    op.create_index(
        "ix_rfq_decision_draft", "procurement_rfq_decision", ["rfq_draft_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_rfq_decision_draft", table_name="procurement_rfq_decision")
    op.drop_table("procurement_rfq_decision")
    op.drop_column("business_command", "payload_hash")
    op.drop_index("ix_rfq_draft_request", table_name="procurement_rfq_draft")
    op.drop_index("uq_rfq_draft_id", table_name="procurement_rfq_draft")
    op.drop_table("procurement_rfq_draft")
