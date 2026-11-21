"""M6-1：询价决策类型扩 'SEND'（发送事件留痕）。

0160 的 decision CHECK 只含 SUBMIT/APPROVE/REJECT/CANCEL；M6-1 的发送动作
需要 SEND 留痕（谁、何时、经哪个端口发出）。

Revision ID: 0161
"""

from __future__ import annotations

from alembic import op

revision = "0161"
down_revision = "0160"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE procurement_rfq_decision DROP CONSTRAINT ck_rfq_decision_kind"
    )
    op.execute(
        "ALTER TABLE procurement_rfq_decision ADD CONSTRAINT ck_rfq_decision_kind "
        "CHECK (decision IN ('SUBMIT','APPROVE','REJECT','CANCEL','SEND'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE procurement_rfq_decision DROP CONSTRAINT ck_rfq_decision_kind"
    )
    op.execute(
        "ALTER TABLE procurement_rfq_decision ADD CONSTRAINT ck_rfq_decision_kind "
        "CHECK (decision IN ('SUBMIT','APPROVE','REJECT','CANCEL'))"
    )
