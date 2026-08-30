"""governance hardening - audit_log + kpi_catalog_history（Phase 4.5，遗留 #68）。

承载 Owner-based ACL / 审计 / 历史快照 三个遗留项的基础设施：

1. `audit_log`：通用审计日志，捕获任何 (entity_type, entity_id) 上的 CREATE/UPDATE/DELETE
   - before_json / after_json JSONB：保留改动前/后快照（UPDATE 两端都有；CREATE 只有 after；
     DELETE 只有 before）。完整状态而非 diff，省去 JSON diff 库依赖
   - actor VARCHAR：用户 ID
   - actor_departments VARCHAR：逗号分隔的部门列表（用于审计查询「哪个部门改了什么」）

2. `kpi_catalog_history`：KpiCatalog 专用历史快照
   - revision INT：与 kpi_catalog.revision_count 对齐（PUT 后 +1）
   - snapshot_json JSONB：当时完整 KPI 状态（含 revision_count）
   - changed_by / changed_at
   - FK 到 kpi_catalog.id 设为 ON DELETE SET NULL：删除 KPI 时历史保留（仅 kpi_id 置 NULL）

设计原则：
- 双表都不可变（仅 INSERT，不 UPDATE / DELETE）。审计/历史不可篡改是治理前提
- audit_log entity_type 用 VARCHAR(50) 而非 FK，保留对任意实体的扩展性
  （后续可给 ontology_class / data_quality_rule 加同样的 audit 写入，无需新建表）
- 索引聚焦查询模式：
  - audit_log: (entity_type, entity_id, created_at DESC) — 查某个实体的所有变更
  - audit_log: (actor, created_at DESC) — 查某个用户的所有变更
  - kpi_catalog_history: (kpi_id, revision DESC) — 查某个 KPI 的最新历史

回滚：drop table 即可，不影响 kpi_catalog 数据。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024_governance_hardening"
down_revision: str | None = "0023_kpi_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 通用审计日志：所有 (entity_type, entity_id) 上的变更
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),  # CREATE / UPDATE / DELETE
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("actor_departments", sa.String(length=500), nullable=True),
        sa.Column("before_json", postgresql.JSONB(), nullable=True),
        sa.Column("after_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "action IN ('CREATE','UPDATE','DELETE')",
            name="ck_audit_log_action",
        ),
    )
    op.create_index(
        "ix_audit_log_entity",
        "audit_log",
        ["entity_type", "entity_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_audit_log_actor",
        "audit_log",
        ["actor", sa.text("created_at DESC")],
    )

    # KpiCatalog 历史快照
    op.create_table(
        "kpi_catalog_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kpi_id", sa.BigInteger(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", postgresql.JSONB(), nullable=False),
        sa.Column("changed_by", sa.String(length=50), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["kpi_id"],
            ["kpi_catalog.id"],
            name="fk_kpi_catalog_history_kpi",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_kpi_history_kpi_revision",
        "kpi_catalog_history",
        ["kpi_id", sa.text("revision DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_kpi_history_kpi_revision", table_name="kpi_catalog_history")
    op.drop_table("kpi_catalog_history")
    op.drop_index("ix_audit_log_actor", table_name="audit_log")
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_table("audit_log")