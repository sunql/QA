"""add btree indexes on audit_log.actor and audit_log.created_at

Revision ID: 0036_audit_actor_index
Revises: 0035_agent_tool_binding
Create Date: 2026-09-02

Why: 0024 模型上声明的是复合索引 ix_audit_log_actor(actor, created_at) —
该索引对「按 actor 单独过滤 / 按 created_at 全表扫描 / ORDER BY created_at」
三类查询并非最优：复合索引的 B 树只在最左前缀生效，单 actor 等值过滤能用上，
但纯 created_at 范围扫描会回退到 seq scan；全表倒序排序也只能用上
actor 单一前缀。

新增两个单列 btree 索引 (idx_audit_log_actor, idx_audit_log_created_at)：
- idx_audit_log_actor：admin 全局筛选 actor=X（listAll 频繁走）
- idx_audit_log_created_at：admin 按时间区间筛选 + 倒序排序热点

复合索引 ix_audit_log_actor 不删除：admin 跨字段 actor + 时间范围
查询时仍可复用。重复索引代价是写放大（每次 INSERT 多维护两个 B 树），
可接受；查询路径显著加快。
"""

from __future__ import annotations

from alembic import op

revision = "0036_audit_actor_index"
down_revision = "0035_agent_tool_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_audit_log_actor", "audit_log", ["actor"])
    op.create_index("idx_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_audit_log_created_at", table_name="audit_log")
    op.drop_index("idx_audit_log_actor", table_name="audit_log")
