"""research entry: 5 tables (feat-research-entry)

**触发**：研究型 Agent 入口（feat-research-entry）。研究域与 chat 域完全隔离
——不复用 chat_session / session_message / analysis_hypothesis / evidence，
避免对既有聊天表结构做任何耦合性改动。

**变更**：新建 5 张表（列定义与 ``app/domain/research_models.py`` 一一对应）：

- ``research_session``：研究 会话 主表（UUID PK；``created_by`` 为普通
  BigInteger、**不加 FK**——用户表名跨域耦合无收益，FK 条款按此放宽）。
- ``research_turn``：会话内单轮交互（``session_id`` FK CASCADE；``content``
  JSONB）。
- ``research_checkpoint``：分阶段决策检查点（``turn_id`` FK CASCADE；
  ``options`` / ``user_choice`` JSONB；``decided_at`` 可空）。
- ``research_finding``：过程结论（``turn_id`` FK CASCADE；
  ``confidence`` Numeric(5,4)）。
- ``research_report``：研究报告（``session_id`` FK CASCADE；``version`` 递增）。
  带部分唯一索引 ``uq_research_report_session_published``：同一 session 至多
  一个 ``status='published'`` 版本（其余版本不受限）。

**幂等性**：降级为倒序 ``op.drop_index`` + ``op.drop_table``（研究域数据整体
丢失，可接受 —— 尚无存量数据）。

**两库同步**：prod + test（容器启动 / 部署脚本自动迁移）。

Revision ID: 0111
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0111"
down_revision: str | None = "0110"
branch_labels = None
depends_on = None

_JSONB_DEFAULT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "research_session",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("mode", sa.String(20), nullable=False, server_default="research"),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("input_seed", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False
        ),
    )
    op.create_table(
        "research_turn",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column(
            "content", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=_JSONB_DEFAULT,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_research_turn_session_id", "research_turn", ["session_id"])
    op.create_table(
        "research_checkpoint",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_turn.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "options", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=_JSONB_DEFAULT,
        ),
        sa.Column("user_choice", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_research_checkpoint_turn_id", "research_checkpoint", ["turn_id"])
    op.create_table(
        "research_finding",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_turn.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("supporting_sql", sa.Text(), nullable=True),
        sa.Column("supporting_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
    )
    op.create_index("ix_research_finding_turn_id", "research_finding", ["turn_id"])
    op.create_table(
        "research_report",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=_JSONB_DEFAULT,
        ),
        sa.Column("rendered_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_research_report_session_id", "research_report", ["session_id"])
    op.create_index(
        "uq_research_report_session_published",
        "research_report",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )


def downgrade() -> None:
    op.drop_index("uq_research_report_session_published", table_name="research_report")
    op.drop_index("ix_research_report_session_id", table_name="research_report")
    op.drop_table("research_report")
    op.drop_index("ix_research_finding_turn_id", table_name="research_finding")
    op.drop_table("research_finding")
    op.drop_index("ix_research_checkpoint_turn_id", table_name="research_checkpoint")
    op.drop_table("research_checkpoint")
    op.drop_index("ix_research_turn_session_id", table_name="research_turn")
    op.drop_table("research_turn")
    op.drop_table("research_session")
