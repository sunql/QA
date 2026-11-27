"""催货单 ORM（feat/delivery-chase-closed-loop，2026-11-27）。

**业务语义**：采购 Copilot · 到货跟踪里，采购员对**未到货**的订单行勾选需要催的行，
系统**按供应商**生成催货单（一个供应商一张），走
`DRAFT → VALIDATING → READY_TO_SEND → PENDING_APPROVAL → SENDING → SENT`
生命周期（与询价单 RFQ 同构但不共表 —— 两者 payload 契约与状态语义不同，
混表会让校验变成 if/else 分叉）。

**本期口径**（用户 2026-11-27 拍板）：
- 不需要四眼审批开关（保留 submit-approval→approve→send 全链路，但本期审批人
  即操作人也可通过，状态机不裁剪）；
- 供应商答复回流**不做**：SEND 成功即终态（`SENT`），后续提醒/升级是二期；
- payload_json（行快照）由**服务端回查**生成，不信前端带来的数量。

与 `procurement_rfq_*` 的刻意差异：
- 行落在**独立行表** `procurement_chase_line`（poNo/poLine 是业务身份，
  不塞 JSONB ⇒ 可按行查询/去重）；payload_json 仍存**完整快照**（外发报文）。
- `supplier_enterprise_key` 为列而非 JSON 内字段（分组主键、可索引）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntPk, TimestampMixin

#: 催货单状态集（与 RFQ 状态机同构：见 services/procurement_chase_state_machine.py）
CHASE_STATUSES = (
    "DRAFT",
    "VALIDATING",
    "NEEDS_CORRECTION",
    "PENDING_APPROVAL",
    "READY_TO_SEND",
    "REJECTED",
    "CANCELLED",
    "SENDING",
    "SENT",
    "CLOSED",
)

#: 决策种类（与 RFQ 相同）
CHASE_DECISIONS = ("SUBMIT", "APPROVE", "REJECT", "CANCEL", "SEND")


class ProcurementChaseDraft(Base, TimestampMixin):
    """催货单草稿（一个供应商一张）。"""

    __tablename__ = "procurement_chase_draft"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('DRAFT','VALIDATING','NEEDS_CORRECTION','PENDING_APPROVAL',"
            "'READY_TO_SEND','REJECTED','CANCELLED','SENDING','SENT','CLOSED')",
            name="ck_chase_draft_status",
        ),
        sa.Index("uq_chase_draft_id", "chase_draft_id", unique=True),
        sa.Index("ix_chase_draft_request", "request_id"),
        sa.Index("ix_chase_draft_supplier", "supplier_enterprise_key"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    chase_draft_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False, default="DRAFT")
    request_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: 统一供应商 key（entity_mapping.enterprise_key；一个催货单恒对应一个供应商）
    supplier_enterprise_key: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    #: ERP 供应商码（Q1630 等；冗余可读，不参与分组）
    supplier_code: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    #: 期望供应商答复期限（本期不回流，仅作为单据上的承诺字段）
    expected_reply_deadline: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: 完整外发报文快照（含行明细）；行明细同时落在 chase_line（可按行查询）
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        sa.String(128), nullable=False, unique=True
    )
    created_by: Mapped[str] = mapped_column(sa.String(64), nullable=False)


class ProcurementChaseLine(Base, TimestampMixin):
    """催货行（来自待到货明细的选定行；(chase_draft_id, po_no, po_line_no) 唯一）。"""

    __tablename__ = "procurement_chase_line"
    __table_args__ = (
        sa.UniqueConstraint(
            "chase_draft_id",
            "po_no",
            "po_line_no",
            name="uq_chase_line_draft_po_poLine",
        ),
        sa.Index("ix_chase_line_draft", "chase_draft_id"),
        sa.ForeignKeyConstraint(
            ["chase_draft_id"],
            ["procurement_chase_draft.chase_draft_id"],
            ondelete="CASCADE",
            name="procurement_chase_line_draft_fkey",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    chase_draft_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    po_no: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    po_line_no: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    material_code: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    material_desc: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    ordered_qty: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 4), nullable=True)
    received_qty: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 4), nullable=True)
    open_qty: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 4), nullable=True)
    expected_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    overdue_days: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)


class ProcurementChaseDecision(Base, TimestampMixin):
    """催货单决策留痕（谁/何时/动作/意见；不覆盖历史）。"""

    __tablename__ = "procurement_chase_decision"
    __table_args__ = (
        sa.CheckConstraint(
            "decision IN ('SUBMIT','APPROVE','REJECT','CANCEL','SEND')",
            name="ck_chase_decision_kind",
        ),
        sa.Index("ix_chase_decision_draft", "chase_draft_id"),
        sa.ForeignKeyConstraint(
            ["chase_draft_id"],
            ["procurement_chase_draft.chase_draft_id"],
            ondelete="CASCADE",
            name="procurement_chase_decision_draft_fkey",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    chase_draft_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    decision: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    actor: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    decided_time: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
