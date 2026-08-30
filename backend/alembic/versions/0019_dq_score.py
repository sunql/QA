"""data_quality_score - 数据质量评分历史表（Phase 1.3）。

按 target_table 聚合 + GLOBAL 两个粒度保存 6 维评分与 overall。TIMELINESS 维
暂保留为 NULL（Phase 2 血缘模块补 ETL 时间字段后再算）。

命名采用 `0019_dq_score` 而非计划文档的 `0018_data_quality_score`：
1. 0018 已被 feat-data-quality-evaluator 占用来加 datasource_id；
2. alembic_version.version_num 是 VARCHAR(32)，长 revision 标识会触发
   StringDataRightTruncationError。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019_dq_score"
down_revision: str | None = "0018_dq_datasource"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_quality_score",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("target_table", sa.String(length=100), nullable=False),
        sa.Column(
            "score_type",
            sa.String(length=10),
            nullable=False,
            server_default="TABLE",
        ),
        # 6 维评分：5 维可计算，timeliness 暂为 NULL（Phase 2 补）
        sa.Column("completeness_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("validity_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("uniqueness_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("consistency_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("timeliness_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("referential_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column(
            "overall_score",
            sa.Numeric(precision=5, scale=2),
            nullable=False,
        ),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "evaluation_duration_ms",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "rules_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_time",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "score_type IN ('TABLE', 'GLOBAL')",
            name="ck_data_quality_score_type",
        ),
    )
    # 复合索引：列表查询（按表 / 类型，按时间倒序）+ latest 单条
    op.create_index(
        "ix_data_quality_score_lookup",
        "data_quality_score",
        ["target_table", "score_type", "evaluated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_data_quality_score_lookup", table_name="data_quality_score")
    op.drop_table("data_quality_score")