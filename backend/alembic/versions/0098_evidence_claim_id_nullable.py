"""evidence.claim_id 放开 NOT NULL（v3.1 任务 B2）

Revision ID: 0098
Revises: 0097
Create Date: 2026-09-29

背景：v3.1 蓝图 §5.7 Evidence Agent 降级为「SQL 执行自动记录」——SQL_QUERY
型证据由 execute_read_only 钩子自动落库，创建时不挂 claim
（related_claim_ids 由上层填充，见 plan-person-b.md B2/B3）。原 0053 建表
`claim_id BIGINT NOT NULL` 阻塞无 claim 的自动记录，故放开为可空。

- Document 型写路径（claim_extractor）恒有 claim，不受影响（约束只放宽）
- 索引 ix_evidence_claim / 外键 fk_evidence_claim 保持不变

编号说明：0098 先到先得（本迁移）；A5 source_version 顺延 0099、
B4 confidence_level 顺延 0100（orchestrator 已同步 ledger）。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "evidence",
        "claim_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )


def downgrade() -> None:
    # 回滚占位：SQL_QUERY 自动证据行没有 claim，可空行先填占位值（自身 id）
    # 才能重建 NOT NULL 约束。占位值无业务语义，仅保证 downgrade 可执行；
    # 生产回滚后这些行应人工复核或清理。
    op.execute("UPDATE evidence SET claim_id = id WHERE claim_id IS NULL")
    op.alter_column(
        "evidence",
        "claim_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
