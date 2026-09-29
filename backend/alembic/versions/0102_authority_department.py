"""wiki_page + knowledge_claim 补 authority_department（v3.1 任务 M5 / 蓝图 §4.13）

Revision ID: 0102
Revises: 0101
Create Date: 2026-09-29

背景：v3.1 治理决策 D1=(b) / D2=双列 —— authority_level（L0-L5 数据精度）保留，
authority_department（组织归属）新增。Authority 是软约束：列存在但不入学习回路
（progressive_upgrader / claim_extractor 不动），仅供治理流程追溯。

新增字段：
- wiki_page.authority_department VARCHAR(30) NULL
- knowledge_claim.authority_department VARCHAR(30) NULL

11 个合法值（wiki_models.KNOWLEDGE_AUTHORITY_DEPARTMENTS）：前 9 个常规组织归属
（SALES_MGMT/FINANCE/SCM/QA/HR/IT/OPS/EXEC/LEGAL），后 2 个软约束占位
（INDUSTRY_STANDARD 外部权威 / CROSS_DOMAIN 跨部门共识），与蓝图 §4.13「软约束」
口径一致。

- **不回填存量行**：NULL 即「待治理流程推动」，旧数据原样保留
- **不删**现有 authority_level（L0-L5）列 —— 学习回路耦合，见
  progressive_upgrader.py:255 / claim_extractor.py:117
- downgrade 对称 drop 两个 column，无数据语义

生产部署注意（两库使用策略）：本迁移对 prod qa_metadata 执行前先备份
wiki_page_<YYYYMMDD> + knowledge_claim_<YYYYMMDD>；本任务只对测试库
（qa-pg-a1 / qa_metadata_test）验证往返，prod 迁移由部署流程统一执行。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wiki_page",
        sa.Column(
            "authority_department",
            sa.String(30),
            nullable=True,
            comment=(
                "权威归属部门，11 部门枚举之一或 NULL（v3.1 §4.13 软约束；"
                "INDUSTRIES/OPS/INDUSTRY_STANDARD/CROSS_DOMAIN 覆盖跨域与外部权威场景）"
            ),
        ),
    )
    op.add_column(
        "knowledge_claim",
        sa.Column(
            "authority_department",
            sa.String(30),
            nullable=True,
            comment=(
                "权威归属部门（同 wiki_page 字段；claim 级独立记录便于按部门筛冲突）"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("knowledge_claim", "authority_department")
    op.drop_column("wiki_page", "authority_department")