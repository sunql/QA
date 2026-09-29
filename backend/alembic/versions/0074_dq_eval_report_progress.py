"""Phase 9: 评估报告异步化 + 进度状态（feat-dq-evaluation-report-progress）

变更：
1. evaluation_report.status CheckConstraint 扩为允许 PENDING/RUNNING/COMPLETED/FAILED。
   旧 DRAFT/PUBLISHED 仍保留（业务可见性）。
2. 新增 evaluation_report.progress JSONB 列：存 { stage, completed, total,
   current_rule_id, current_rule_code, message, started_at, finished_at }。
3. 新增 ix_evaluation_report_status 部分索引，让 worker 按 status 轮询更小。

文件名：0074_dq_eval_report_progress.py = 30 字符（≤32 字符 OK，参见
[[alembic-version-filename-32-char-limit]]）。

Revision ID: 0074
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "0074_dq_eval_report_progress"
down_revision: str | None = "0073_in_app_message"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) 替换 status CheckConstraint（旧约束只允许 DRAFT/PUBLISHED）
    op.drop_constraint(
        "ck_evaluation_report_status",
        "evaluation_report",
        type_="check",
    )
    op.create_check_constraint(
        "ck_evaluation_report_status",
        "evaluation_report",
        "status IN ('DRAFT','PUBLISHED','PENDING','RUNNING','COMPLETED','FAILED')",
    )

    # 2) 新增 progress JSONB 列；nullable，旧数据回填为 NULL。
    op.add_column(
        "evaluation_report",
        sa.Column(
            "progress",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # 3) 部分索引：worker 轮询 status='RUNNING' 的报告做进度更新；让扫描范围更小。
    op.create_index(
        "ix_evaluation_report_status_running",
        "evaluation_report",
        ["status"],
        postgresql_where=sa.text("status = 'RUNNING'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evaluation_report_status_running",
        table_name="evaluation_report",
    )
    op.drop_column("evaluation_report", "progress")
    op.drop_constraint(
        "ck_evaluation_report_status",
        "evaluation_report",
        type_="check",
    )
    op.create_check_constraint(
        "ck_evaluation_report_status",
        "evaluation_report",
        "status IN ('DRAFT','PUBLISHED')",
    )
