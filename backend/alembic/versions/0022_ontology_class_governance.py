"""ontology_class 治理字段 - object_type / object_owner（Phase 3.4）。

满足采购域 Sheet 03 业务对象目录治理要求：
- object_type：Master（主数据）/ Transaction（交易单据）/ Reference（参考/配置）/ Event（事件）
- object_owner：责任部门/人

两列均可空：历史行（迁移前已有）object_type 为 NULL，由 seed_ontology
增量回填（只增不删）。仅加列、不改既有数据，可安全回滚（drop column）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022_ontology_class_governance"
down_revision: str | None = "0021_entity_mapping"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ontology_class",
        sa.Column(
            "object_type",
            sa.String(length=20),
            nullable=True,
            comment="Master/Transaction/Reference/Event",
        ),
    )
    op.add_column(
        "ontology_class",
        sa.Column(
            "object_owner",
            sa.String(length=100),
            nullable=True,
            comment="责任部门/人",
        ),
    )


def downgrade() -> None:
    op.drop_column("ontology_class", "object_owner")
    op.drop_column("ontology_class", "object_type")
