"""0165：文档「可用于 AI 上下文」的**人工开关**。

为什么用人工开关而不是自动判定：
本批先尝试过自动判定文本质量（中文占比 / 全大写词占比 / 替换字符…），
实测**反复误判**——中文文档里的编码（`TH-MDM-01`、`SUS304`）会让"大写词比例"
冲到 87%–95%，把正常文档全判成乱码；放宽阈值后又漏掉真乱码。
⇒ 判定规则不可靠时，**把决定权交给运营**：默认全部可用，人工把不可信的文档
关掉（附原因），检索与 LLM 注入都尊重这个开关。

自动体检脚本（`scripts/audit_document_text_quality.py`）保留，但只作**提示**，
不写库、不做判罚。

字段：
- ``ai_context_enabled``：BOOLEAN NOT NULL DEFAULT TRUE（关闭 = 不参与检索/注入）
- ``ai_context_note``：为什么关（可空，给下一个人看）

Revision ID: 0165
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0165"
down_revision = "0164"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_catalog",
        sa.Column(
            "ai_context_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "document_catalog",
        sa.Column("ai_context_note", sa.String(200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_catalog", "ai_context_note")
    op.drop_column("document_catalog", "ai_context_enabled")
