"""knowledge_claim 补 confidence_level（v3.1 任务 B4 / 蓝图 §12.2）

Revision ID: 0100
Revises: 0099
Create Date: 2026-09-29

背景：M3 离散 4 级置信度（HIGH/MEDIUM/LOW/REFUSE）淘汰 0-1 伪精确展示。
knowledge_claim 加 ``confidence_level`` VARCHAR(10) nullable：

- **不回填存量行**：读路径在 DTO 序列化时现算（confidence_service），
  列仅作缓存/导出用途，留作未来缓存
- **不删**原始 ``confidence`` Numeric(5,4) 列（原始分保留供未来校准）
- downgrade = drop column（无数据语义，直接对称反转）

生产部署注意（两库使用策略）：本迁移对 prod qa_metadata 执行前，先备份
knowledge_claim_<YYYYMMDD>；本任务只对测试库（qa-pg-a1 / qa_metadata_test）
验证往返，prod 迁移由部署流程统一执行。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_claim",
        sa.Column(
            "confidence_level",
            sa.String(10),
            nullable=True,
            comment="离散置信度等级 HIGH/MEDIUM/LOW/REFUSE（v3.1 §12.2，读路径现算，此列留作缓存）",
        ),
    )


def downgrade() -> None:
    op.drop_column("knowledge_claim", "confidence_level")
