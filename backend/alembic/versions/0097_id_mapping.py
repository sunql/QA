"""id_mapping table — M0-P0.1

Revision ID: 0095
Revises: 0094_wiki_page_category_id
Create Date: 2026-09-29

id_mapping 表：统一 ID 映射中枢。
unified_id 格式 "obj:{business_object}:{external_id}"，唯一索引 (business_object, external_id)。
三库 ID 列（pg/neo4j/milvus）初期可为空，后续 M0-P0.2/P0.3 逐步填充。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0097"
down_revision = "0096"  # 接 B1 合入的 0096（d547355）
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "id_mapping",
        sa.Column("unified_id", sa.String(length=128), primary_key=True),
        sa.Column(
            "business_object",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column(
            "external_id",
            sa.String(length=128),
            nullable=False,
        ),
        # PG 侧
        sa.Column("pg_table", sa.String(length=64), nullable=True),
        sa.Column("pg_id", sa.String(length=128), nullable=True),
        # Neo4j 侧
        sa.Column("neo4j_node_id", sa.String(length=128), nullable=True),
        # Milvus 侧
        sa.Column("milvus_collection", sa.String(length=64), nullable=True),
        sa.Column("milvus_id", sa.String(length=128), nullable=True),
        # 时间戳（与现有 TimestampMixin 保持一致）
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
            onupdate=sa.func.now(),
        ),
    )
    # 唯一约束：(business_object, external_id)
    op.create_index(
        "uq_id_mapping_bo_ext",
        "id_mapping",
        ["business_object", "external_id"],
        unique=True,
    )
    # 查询友好索引
    op.create_index("ix_id_mapping_bo", "id_mapping", ["business_object"])
    op.create_index("ix_id_mapping_pg_table", "id_mapping", ["pg_table"])


def downgrade() -> None:
    op.drop_index("ix_id_mapping_pg_table", table_name="id_mapping")
    op.drop_index("ix_id_mapping_bo", table_name="id_mapping")
    op.drop_index("uq_id_mapping_bo_ext", table_name="id_mapping")
    op.drop_table("id_mapping")
