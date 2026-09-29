"""knowledge_claim 补 source_version（v3.1 任务 A5）

Revision ID: 0099
Revises: 0098
Create Date: 2026-09-29

背景：v3.1 蓝图 §4.14 运维约束 2「编译产物可追溯」——每个编译产物都能反向
追溯到源定义（source_object_id + source_version）。knowledge_claim 作为
Wiki 知识的弱结构编译产物，补 source_version（VARCHAR(32)，nullable）：

- 存量行不动（NULL，不回填）
- 新写入路径由 claim 抽取侧按需携带（本迁移只加列，不改写路径）
- downgrade = drop column（无数据语义，直接对称反转）

编号说明：0097 id_mapping（A1）、0098 evidence.claim_id nullable（B2）
先到先得，本迁移顺延 0099（2026-09-29 orchestrator 拍板）。

生产部署注意（两库使用策略）：本迁移对 prod qa_metadata 执行前，先备份
knowledge_claim_<YYYYMMDD>。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_claim",
        sa.Column(
            "source_version",
            sa.String(32),
            nullable=True,
            comment="源 Page 版本号（编译产物可追溯，v3.1 §4.14）",
        ),
    )


def downgrade() -> None:
    op.drop_column("knowledge_claim", "source_version")
