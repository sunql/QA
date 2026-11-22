"""发现评审：候选/问题补 `decision_note`（WP-ONT-AI A3 / G5 前台签收，0168）。

前台「发现评审」页需要业务在判定时留一句话（驳回理由 / 待证要补什么）。
`discovery_candidate` / `discovery_finding` 已有 `status/decided_*`，缺的只是备注列。

纯加法 DDL；列注释与 ORM 逐字一致（单测钉住）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0168"
down_revision = "0167"
branch_labels = None
depends_on = None

_CANDIDATE_COMMENT = "0168，WP-ONT-AI G5：业务判定备注（驳回理由 / 待证需补证据）"
_FINDING_COMMENT = "0168，WP-ONT-AI G5：问题处理备注（负责人 / 处理建议）"


def upgrade() -> None:
    op.add_column(
        "discovery_candidate",
        sa.Column("decision_note", sa.Text(), nullable=True, comment=_CANDIDATE_COMMENT),
    )
    op.add_column(
        "discovery_finding",
        sa.Column("decision_note", sa.Text(), nullable=True, comment=_FINDING_COMMENT),
    )


def downgrade() -> None:
    op.drop_column("discovery_finding", "decision_note")
    op.drop_column("discovery_candidate", "decision_note")
