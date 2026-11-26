"""copilot 场景配置化（P4）：场景定义表 + 会话置顶。

- `copilot_scenario`：场景从硬编码（ProcurementCopilotPage.tsx）变为 DB 业务资产。
  卡片墙/工作区/提问模板/结果渲染器全部由此表驱动；加场景 = 插一行配置。
- `procurement_copilot_conversation.pinned_at`：会话置顶（跨设备同步）。
  前端 localStorage 版已移除，改由 pin 端点维护。

revision: 0170；down_revision: 0169。零数据回填，downgrade 直接 drop。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from typing import Sequence

# revision identifiers, used by Alembic.
revision: str = "0170"
down_revision: str | None = "0169"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Sequence import（与 0166 等既有迁移同款写法）
from typing import Sequence  # noqa: E402


def upgrade() -> None:
    op.create_table(
        "copilot_scenario",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "scenario_code", sa.String(64), nullable=False
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("icon_code", sa.String(30), nullable=False, server_default="robot"),
        # 钉死的 Agent（保存时校验存活；运行时 fail loud）
        sa.Column("agent_code", sa.String(64), nullable=False),
        # 槽位定义数组（type: number/dateRange/entityMulti/dictMulti/text）
        sa.Column("slot_schema", JSONB(none_as_null=True), nullable=False),
        # 提问模板：{slot} 占位 + 冲突仲裁尾注（后端渲染）
        sa.Column("question_template", sa.Text(), nullable=False),
        # 预设槽位组合（推荐问题 chips）
        sa.Column("chips", JSONB(none_as_null=True), nullable=False),
        # 结果渲染器：pending_delivery / generic_table / none
        sa.Column("result_renderer", sa.String(30), nullable=False, server_default="none"),
        # 可见角色（NULL=全员；admin=仅管理员）——数据源门控等治理口径由此表达
        sa.Column("required_role", sa.String(64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("scenario_code", name="uq_copilot_scenario_code"),
    )
    op.create_index(
        "ix_copilot_scenario_enabled_sort",
        "copilot_scenario",
        ["enabled", "sort_order"],
    )

    # 会话置顶（跨设备同步）：NULL = 未置顶；非 NULL = 置顶时间（排序用）
    op.add_column(
        "procurement_copilot_conversation",
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_copilot_conversation_pinned",
        "procurement_copilot_conversation",
        ["pinned_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_copilot_conversation_pinned", table_name="procurement_copilot_conversation"
    )
    op.drop_column("procurement_copilot_conversation", "pinned_at")
    op.drop_index("ix_copilot_scenario_enabled_sort", table_name="copilot_scenario")
    op.drop_table("copilot_scenario")
