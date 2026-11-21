"""0163：文档目录补原文阅览/向量化所需元数据。

新增 4 列（纯加法，不改既有列语义）：
- ``mime_type``    上传时的真实 MIME（wiki 侧也记；缺失按扩展名推断）
- ``file_size``    字节数（前端显示体积）
- ``preview_kind`` 预览类别（TEXT/MARKDOWN/HTML/PDF/OFFICE/IMAGE）——
  **落库而非每次推断**：推断规则会变，落库后已入库文档的呈现不变
- ``vectorized_at`` 最近一次向量化完成时间（NULL = 未向量化；目录页据此显示
  状态，避免 N 行文档 = N 次 Milvus 查询）

Revision ID: 0163
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0163"
down_revision = "0162"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_catalog",
        sa.Column("mime_type", sa.String(120), nullable=True),
    )
    op.add_column(
        "document_catalog",
        sa.Column("file_size", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "document_catalog",
        sa.Column("preview_kind", sa.String(20), nullable=True),
    )
    op.add_column(
        "document_catalog",
        sa.Column("vectorized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_document_preview_kind",
        "document_catalog",
        "preview_kind IS NULL OR preview_kind IN "
        "('TEXT','MARKDOWN','HTML','PDF','OFFICE','IMAGE')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_document_preview_kind", "document_catalog", type_="check"
    )
    op.drop_column("document_catalog", "vectorized_at")
    op.drop_column("document_catalog", "preview_kind")
    op.drop_column("document_catalog", "file_size")
    op.drop_column("document_catalog", "mime_type")
