"""organization 树形层级：新增 sort_order 字段 + parent+sort 复合索引

Revision ID: 0046_org_sort_order
Revises: 0045_rbac_identity
Why: 组织架构树形化管理——parent_id 自引用 FK 早已预留（0045），本迁移加
sort_order 字段让同一 parent 下的兄弟节点有稳定顺序；parent_id+sort_order
复合索引让「按 parent 列出按 sort 排序」O(log n) 查询（树形 API 主路径）。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0046_org_sort_order"
down_revision = "0045_rbac_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DEFAULT 0 让存量行（migration 0045 已有数据）安全回填，NOT NULL 强制
    # 未来所有行显式提供。
    op.add_column(
        "organizations",
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_organizations_parent_sort",
        "organizations",
        ["parent_id", "sort_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_organizations_parent_sort", table_name="organizations")
    op.drop_column("organizations", "sort_order")
