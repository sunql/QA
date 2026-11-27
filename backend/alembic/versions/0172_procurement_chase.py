"""催货单三张表（feat/delivery-chase-closed-loop）。

采购 Copilot · 到货跟踪的**业务闭环**：对未到货的订单行，选择需要催的行，
按供应商创建催货单（一个供应商一张），走与询价单同构的
DRAFT → VALIDATING → PENDING_APPROVAL → SENDING → SENT 生命周期。

表：
- procurement_chase_draft   —— 催货单（状态机 + 外发报文快照）
- procurement_chase_line    —— 催货行（来自待到货明细的选定行；按 (draft, po, line) 唯一）
- procurement_chase_decision —— 决策留痕（谁/何时/动作/意见）

为什么与 procurement_rfq_* 分表而不加 kind 列：两者 payload 契约与状态语义不同
（询价 = 邀请报价；催货 = 欠交催告），混表会让校验变成 if/else 分叉。

Revision ID: 0172；down_revision: 0171（迁移号串行占号，不留空号）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision = "0172"
down_revision = "0171"
branch_labels = None
depends_on = None

_DRAFT_STATUS_CHECK = (
    "status IN ('DRAFT','VALIDATING','NEEDS_CORRECTION','PENDING_APPROVAL',"
    "'READY_TO_SEND','REJECTED','CANCELLED','SENDING','SENT','CLOSED')"
)
_DECISION_CHECK = "decision IN ('SUBMIT','APPROVE','REJECT','CANCEL','SEND')"


def upgrade() -> None:
    op.create_table(
        "procurement_chase_draft",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("chase_draft_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="DRAFT"),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("supplier_enterprise_key", sa.BigInteger, nullable=False),
        sa.Column("supplier_code", sa.String(100), nullable=False),
        sa.Column("expected_reply_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("payload_json", JSONB, nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_time", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_time", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chase_draft_id", name="uq_chase_draft_id"),
        sa.UniqueConstraint("idempotency_key", name="procurement_chase_draft_idempotency_key_key"),
        sa.CheckConstraint(_DRAFT_STATUS_CHECK, name="ck_chase_draft_status"),
    )
    op.create_index("ix_chase_draft_request", "procurement_chase_draft", ["request_id"])
    op.create_index(
        "ix_chase_draft_supplier", "procurement_chase_draft", ["supplier_enterprise_key"]
    )

    op.create_table(
        "procurement_chase_line",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("chase_draft_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("po_no", sa.String(64), nullable=False),
        sa.Column("po_line_no", sa.String(64), nullable=False),
        sa.Column("material_code", sa.String(100), nullable=True),
        sa.Column("material_desc", sa.String(200), nullable=True),
        sa.Column("ordered_qty", sa.Numeric(18, 4), nullable=True),
        sa.Column("received_qty", sa.Numeric(18, 4), nullable=True),
        sa.Column("open_qty", sa.Numeric(18, 4), nullable=True),
        sa.Column("expected_date", sa.Date, nullable=True),
        sa.Column("overdue_days", sa.Integer, nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_time", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["chase_draft_id"],
            ["procurement_chase_draft.chase_draft_id"],
            ondelete="CASCADE",
            name="procurement_chase_line_draft_fkey",
        ),
        sa.UniqueConstraint(
            "chase_draft_id", "po_no", "po_line_no", name="uq_chase_line_draft_po_poLine"
        ),
    )
    op.create_index("ix_chase_line_draft", "procurement_chase_line", ["chase_draft_id"])

    op.create_table(
        "procurement_chase_decision",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("chase_draft_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "decided_time", sa.DateTime(timezone=True), server_default=sa.text("now()")
        ),
        sa.Column("created_time", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_time", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["chase_draft_id"],
            ["procurement_chase_draft.chase_draft_id"],
            ondelete="CASCADE",
            name="procurement_chase_decision_draft_fkey",
        ),
        sa.CheckConstraint(_DECISION_CHECK, name="ck_chase_decision_kind"),
    )
    op.create_index(
        "ix_chase_decision_draft", "procurement_chase_decision", ["chase_draft_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_chase_decision_draft", table_name="procurement_chase_decision")
    op.drop_table("procurement_chase_decision")
    op.drop_index("ix_chase_line_draft", table_name="procurement_chase_line")
    op.drop_table("procurement_chase_line")
    op.drop_index("ix_chase_draft_supplier", table_name="procurement_chase_draft")
    op.drop_index("ix_chase_draft_request", table_name="procurement_chase_draft")
    op.drop_table("procurement_chase_draft")
