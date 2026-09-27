"""entity_mapping supplier name trigram 索引（§2.4 L1，2026-09-27）。

**触发**：`supplier_name_resolver.py:151` 的 `ilike %name%` 无索引，3500 行
顺序扫，supplier_360/risk 路径每条「供应商名解析」都走全表。

**方案**：GIN trigram 索引（pg_trgm extension）+ partial WHERE entity_type='SUPPLIER'
（只对供应商子集建索引，体积最小）。

**为何不用 CONCURRENTLY**：alembic 默认事务性 migration（见 env.py ``begin_transaction``），
事务内 PG 拒绝 ``CREATE INDEX CONCURRENTLY``；强行切 autocommit 会动 env.py 全局行为，
对其它迁移造成回归风险。**3500 行表锁 < 1 秒**，在线 DDL 风险可接受；0084 migration
已建立「不引 CONCURRENTLY」的约定（见其 comment），本批沿用。

**幂等性**：IF NOT EXISTS 全程幂等。pg_trgm extension 不在降级里删 —— 后续可能有
别的迁移复用。

Revision ID: 0086
"""

from __future__ import annotations

from alembic import op

revision: str = "0086_supplier_name_trigram"
down_revision: str | None = "0085_session_message_interrupted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_mapping_supplier_name_trgm "
        "ON entity_mapping USING gin (name gin_trgm_ops) "
        "WHERE entity_type = 'SUPPLIER'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_entity_mapping_supplier_name_trgm")