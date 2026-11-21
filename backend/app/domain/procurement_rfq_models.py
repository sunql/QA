"""M5-1 询价草稿 ORM（DRAFT-only，设计稿 §6.4 的 RFQ 半边）。

payload_json 结构（§8.6）：
``{"supplierKeys": [123, 456], "lineItems": [{"materialKey": 9988, "quantity": 100,
"unit": "TON", "deliveryDate": "2026-12-05"}]}``
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntPk, TimestampMixin


class ProcurementRfqDraft(Base, TimestampMixin):
    """询价草稿（DRAFT-only：SENDING/SENT 在 M5 不可达，见状态机模块）。"""

    __tablename__ = "procurement_rfq_draft"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('DRAFT','VALIDATING','NEEDS_CORRECTION','PENDING_APPROVAL',"
            "'READY_TO_SEND','REJECTED','CANCELLED','SENDING','SENT','CLOSED')",
            name="ck_rfq_draft_status",
        ),
        sa.Index("uq_rfq_draft_id", "rfq_draft_id", unique=True),
        sa.Index("ix_rfq_draft_request", "request_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    rfq_draft_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False, default="DRAFT")
    request_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    deadline: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    created_by: Mapped[str] = mapped_column(sa.String(64), nullable=False)


class ProcurementRfqDecision(Base, TimestampMixin):
    """RFQ 决策留痕（M5-2，DoD 4：谁/何时/动作/意见；不覆盖历史）。"""

    __tablename__ = "procurement_rfq_decision"
    __table_args__ = (
        sa.CheckConstraint(
            "decision IN ('SUBMIT','APPROVE','REJECT','CANCEL','SEND')",
            name="ck_rfq_decision_kind",
        ),
        sa.Index("ix_rfq_decision_draft", "rfq_draft_id"),
        sa.ForeignKeyConstraint(
            ["rfq_draft_id"],
            ["procurement_rfq_draft.rfq_draft_id"],
            ondelete="CASCADE",
            name="procurement_rfq_decision_rfq_draft_id_fkey",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    rfq_draft_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    decision: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    actor: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    decided_time: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False,
        server_default=sa.text("now()"),
    )
