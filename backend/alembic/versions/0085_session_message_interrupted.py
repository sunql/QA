"""session_message 增加 interrupted 列（H4 流式断连兜底，2026-09-26）。

**触发**：SSE 流式回答中途客户端断连时，已经下发给用户的回答片段此前整轮回滚丢失
（断连时生成器停在 ``yield`` 上，``except CancelledError``/``finally`` 都不触发，
既有落库点根本走不到）。H4 改为 ``StreamingResponse(background=...)`` 兜底落库，
需要一个字段把「半截回答」与正常回答区分开：历史面板渲染「（已中断）」，
下游（多轮上下文/导出）不得把它当完整回答。

**变更**：``session_message`` 增加 ``interrupted BOOLEAN NOT NULL DEFAULT false``。
存量历史行天然为 false（当时不存在中断行）；加列 + 常量默认值，向后兼容。

**幂等性**：降级反向 DROP COLUMN（丢失该列数据，可接受 —— 它只是标记）。

**两库同步**：prod + test。

Revision ID: 0085
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0085_session_message_interrupted"
down_revision: str | None = "0084_mcp_call_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_message",
        sa.Column(
            "interrupted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("session_message", "interrupted")
