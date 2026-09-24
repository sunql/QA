"""class_domain_mapping + coverage_cell - 机制 6 覆盖度自感知表（Phase 8 M7）。

两张表支撑「系统的知识覆盖到什么程度、缺口在哪」：

- ``class_domain_mapping``：本体类 → 业务域的映射。``ontology_class`` 没有
  domain 列（治理字段只有 object_type / object_owner），而覆盖度要按业务域出
  矩阵，故域轴独立成表，按需标注、可扩展。
- ``coverage_cell``：矩阵单元格 = (维度 × 本体类 × 业务域) 的覆盖快照。

**``domain`` 是 NOT NULL 且带 ``UNASSIGNED`` 哨兵值。** 这是本迁移里唯一一个
不显然的决定：唯一键含 domain，而 PostgreSQL 唯一索引默认 NULL 互不相等 ——
若允许 NULL，「未标注域的类」每次刷新都会插一行新的 NULL 行，唯一约束失效、
矩阵随刷新次数无限膨胀，且没有任何报错。哨兵值把「未分配」变成可去重的、
可在看板上直接看到的桶。

**两表都建 FK 到 ``ontology_class(id)`` 并级联删除**：类被硬删时其覆盖度与域映射
随之消失。类通常是软删（valid_to 墓碑），软删的类其映射保留 —— 覆盖度要能反映
「这条知识对应的业务对象还在不在」，映射是人工标注，不该被软删悄悄抹掉。

建表用 ``IF NOT EXISTS`` 幂等守卫，与 0053-0058 同模式。

Revision ID: 0059
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0059_wiki_coverage_tables"
down_revision: str | None = "0058_structure_artifact_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS class_domain_mapping (
            id                 BIGSERIAL PRIMARY KEY,
            ontology_class_id  BIGINT NOT NULL
                                 REFERENCES ontology_class(id) ON DELETE CASCADE,
            domain             VARCHAR(100) NOT NULL,
            created_by_user_id BIGINT,
            created_time       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_class_domain_mapping UNIQUE (ontology_class_id, domain)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_class_domain_mapping_domain "
        "ON class_domain_mapping (domain)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS coverage_cell (
            id                 BIGSERIAL PRIMARY KEY,
            dimension          VARCHAR(30)  NOT NULL,
            ontology_class_id  BIGINT NOT NULL
                                 REFERENCES ontology_class(id) ON DELETE CASCADE,
            domain             VARCHAR(100) NOT NULL DEFAULT 'UNASSIGNED',
            page_count         INTEGER NOT NULL DEFAULT 0,
            approved_count     INTEGER NOT NULL DEFAULT 0,
            coverage_status    VARCHAR(30)  NOT NULL,
            last_refreshed_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_coverage_cell
                UNIQUE (dimension, ontology_class_id, domain)
        )
        """
    )
    # 看板主查询：按域筛缺口格。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_coverage_cell_domain_status "
        "ON coverage_cell (domain, coverage_status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS coverage_cell")
    op.execute("DROP TABLE IF EXISTS class_domain_mapping")
