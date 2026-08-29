"""embedding_provider 单活唯一索引：is_active 至多一行 True（DB 层兜底）

Revision ID: 0014_embedding_provider_active
Revises: 0013_seed_embedding_providers
Create Date: 2026-08-13

应用层已用 _clearOtherActives 维护单活互斥；此部分唯一索引兜底并发写（READ COMMITTED
下两个并发 activate 可能各自看不到对方未提交行）。冲突由 service 捕获 IntegrityError 转 422。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_embedding_provider_active"
down_revision: Union[str, None] = "0013_seed_embedding_providers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_embedding_provider_active",
        "embedding_provider",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("uq_embedding_provider_active", table_name="embedding_provider")
