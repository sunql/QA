"""Think_Hide 系统参数：推理模型思维链隐藏开关（1=隐藏，0=显示）。

与 0052（ENABLE_L4_AGENT_LOOP）同模式：ON CONFLICT DO NOTHING——
只补种默认行，绝不覆盖 admin 已改的值（Think_Hide 会被运行期翻转，
进 seed_system_config.py 的强制覆盖语义会把 admin 的选择冲回默认）。

Revision ID: 0108
Revises: 0107
Create Date: 2026-10-03
"""

from alembic import op

revision: str = "0108"
down_revision: str | None = "0107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO system_config (key, value, description)
        VALUES (
            'Think_Hide',
            '0',
            '推理模型思维链(think)隐藏开关；1=隐藏，0=显示'
        )
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM system_config WHERE key = 'Think_Hide'")
