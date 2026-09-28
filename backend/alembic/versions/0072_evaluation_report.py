"""Phase 1.5: 数据质量评估报告 (feat-dq-evaluation-report)

新增 4 张表：
- evaluation_report        ：报告主表（配置 + 快照合一）
- data_quality_violation_sample ：违规样本（FK ON DELETE CASCADE from report）
- evaluation_report_schedule     ：定时生成配置（cron 表达式 + 收件人）
- evaluation_report_share        ：分享链接 token 表（FK CASCADE from report）

文件名 ≤32 字符（alembic_version.version_num varchar(32) 限制）：
0072_evaluation_report.py = 26 字符。

Revision ID: 0072
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "0072_evaluation_report"
down_revision: str | None = "0071_ontology_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. evaluation_report —— 配置 + 快照合一
    op.create_table(
        "evaluation_report",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # JSONB 数组：与 ontology_property.allowed_values 风格一致
        sa.Column(
            "class_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "rule_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("time_window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="PUBLISHED",
        ),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "snapshot_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("owner", sa.String(length=100), nullable=True),
        sa.Column("created_by", sa.String(length=50), nullable=False),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('DRAFT','PUBLISHED')",
            name="ck_evaluation_report_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # GIN 索引 + 按 time_window_end 时间倒序 + 按 created_by 过滤
    op.create_index(
        "ix_evaluation_report_created_by",
        "evaluation_report",
        ["created_by"],
    )
    op.create_index(
        "ix_evaluation_report_window",
        "evaluation_report",
        ["time_window_end"],
    )
    op.create_index(
        "ix_evaluation_report_class_gin",
        "evaluation_report",
        ["class_ids"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_evaluation_report_rule_gin",
        "evaluation_report",
        ["rule_ids"],
        postgresql_using="gin",
    )

    # 2. data_quality_violation_sample —— 违规样本
    op.create_table(
        "data_quality_violation_sample",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_id", sa.BigInteger(), nullable=False),
        sa.Column("datasource_id", sa.BigInteger(), nullable=False),
        sa.Column("target_table", sa.String(length=100), nullable=False),
        sa.Column("target_column", sa.String(length=100), nullable=True),
        sa.Column("total_violations", sa.Integer(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column(
            "sample_pk_values",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["evaluation_report.id"],
            name="fk_dq_violation_sample_report",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dq_violation_sample_report_rule",
        "data_quality_violation_sample",
        ["report_id", "rule_id"],
    )
    op.create_index(
        "ix_dq_violation_sample_table",
        "data_quality_violation_sample",
        ["target_table"],
    )

    # 3. evaluation_report_schedule —— 定时配置
    op.create_table(
        "evaluation_report_schedule",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("cron_expression", sa.String(length=100), nullable=False),
        sa.Column(
            "class_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "rule_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("time_window_type", sa.String(length=32), nullable=False),
        sa.Column(
            "recipients",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_report_id", sa.BigInteger(), nullable=True),
        sa.Column("owner", sa.String(length=100), nullable=True),
        sa.Column("created_by", sa.String(length=50), nullable=False),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "time_window_type IN ('LAST_7D','LAST_30D','LAST_RUN')",
            name="ck_evaluation_report_schedule_window",
        ),
        sa.ForeignKeyConstraint(
            ["last_report_id"],
            ["evaluation_report.id"],
            name="fk_eval_report_schedule_last_report",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # 部分索引：只索引 enabled=TRUE 行，让 worker 轮询扫描更小
    op.create_index(
        "ix_evaluation_report_schedule_due",
        "evaluation_report_schedule",
        ["next_run_at"],
        postgresql_where=sa.text("enabled = TRUE"),
    )

    # 4. evaluation_report_share —— 分享链接
    op.create_table(
        "evaluation_report_share",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        # share_token：PG 用 UUID，SQLite 测试用 String(36) 兼容
        sa.Column(
            "share_token",
            postgresql.UUID().with_variant(sa.String(36), "sqlite"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "access_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("created_by", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["evaluation_report.id"],
            name="fk_evaluation_report_share_report",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("share_token", name="uq_evaluation_report_share_token"),
    )
    op.create_index(
        "ix_evaluation_report_share_expires",
        "evaluation_report_share",
        ["expires_at"],
    )


def downgrade() -> None:
    # 必须与 upgrade 严格对称：先删索引再删表（外键依赖顺序）
    op.drop_index("ix_evaluation_report_share_expires", table_name="evaluation_report_share")
    op.drop_table("evaluation_report_share")

    op.drop_index(
        "ix_evaluation_report_schedule_due",
        table_name="evaluation_report_schedule",
    )
    op.drop_table("evaluation_report_schedule")

    op.drop_index("ix_dq_violation_sample_table", table_name="data_quality_violation_sample")
    op.drop_index(
        "ix_dq_violation_sample_report_rule", table_name="data_quality_violation_sample"
    )
    op.drop_table("data_quality_violation_sample")

    op.drop_index("ix_evaluation_report_rule_gin", table_name="evaluation_report")
    op.drop_index("ix_evaluation_report_class_gin", table_name="evaluation_report")
    op.drop_index("ix_evaluation_report_window", table_name="evaluation_report")
    op.drop_index("ix_evaluation_report_created_by", table_name="evaluation_report")
    op.drop_table("evaluation_report")