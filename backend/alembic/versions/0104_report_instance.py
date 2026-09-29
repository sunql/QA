"""report_instance 表（v3.1 A8 / M4 Report 模板 MVP / 蓝图 §5.8 §21）

模板化报告的渲染产物落库：模板存代码不存库（report_templates/ 包），
本表只存每次生成的实例——sections JSONB（绑定数据）、summary（≤1 次
LLM 总结，逐行 [事实]/[推断]/[假设] 前缀）、强制人审状态机
PENDING_REVIEW → APPROVED / REJECTED。

- 新表，不回填、不触碰任何既有对象；downgrade = drop table
- 列与 ORM（app/domain/models.py ReportInstance）逐项对齐（drift 校验
  已到列+索引粒度）：id BIGSERIAL 主键、status/created_by 两个索引、
  created_time/updated_time 走 TimestampMixin 惯例（Python 侧默认，
  无 server_default；status 的 server_default='PENDING_REVIEW' 与 ORM 一致）

生产部署注意（两库使用策略）：本迁移对 prod qa_metadata 执行由部署流程统一
执行；本任务只对测试库（qa-pg-a1 / qa_metadata_test）验证往返。

Revision ID: 0104
Revises: 0103
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_instance",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("template_code", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("params", JSONB(), nullable=True),
        sa.Column("sections", JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="PENDING_REVIEW",
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_instance_status", "report_instance", ["status"])
    op.create_index(
        "ix_report_instance_created_by", "report_instance", ["created_by"],
    )


def downgrade() -> None:
    op.drop_index("ix_report_instance_created_by", table_name="report_instance")
    op.drop_index("ix_report_instance_status", table_name="report_instance")
    op.drop_table("report_instance")
