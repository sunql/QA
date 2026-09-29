"""analysis_hypothesis 表（v3.1 B6 / M7 Hypothesis Hook / 蓝图 §5.6）

数据查询完成后可选触发的假设后处理产物：每行一条「可能解释」假设，
含 schema driver（列名/指标名）与只读验证 SQL，供前端答案下方展示
「可能原因」区块 + GET /chat/sessions/{sessionId}/hypotheses 读取。

- 新表，不回填、不触碰任何既有对象；downgrade = drop table
- verification_sql 只存储不执行——执行走用户显式发起的既有 QUERY 链路
  （SQL Guard 自然生效），存储前 service 层做轻量只读静态校验
- 列与 ORM（app/domain/models.py AnalysisHypothesis）逐项对齐（drift 校验
  已到列+索引粒度）：id BIGSERIAL 主键、session_id 索引、created_time/
  updated_time 走 TimestampMixin 惯例（Python 侧默认，无 server_default）

生产部署注意（两库使用策略）：本迁移对 prod qa_metadata 执行由部署流程统一
执行；本任务只对测试库（qa-pg-a1 / qa_metadata_test）验证往返。

Revision ID: 0103
Revises: 0102
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_hypothesis",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("driver", sa.String(length=200), nullable=True),
        sa.Column("verification_sql", sa.Text(), nullable=False),
        sa.Column("turn_question", sa.Text(), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_analysis_hypothesis_session_id", "analysis_hypothesis", ["session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analysis_hypothesis_session_id", table_name="analysis_hypothesis",
    )
    op.drop_table("analysis_hypothesis")
