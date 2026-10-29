"""ontology_metric_source：UNIQUE(metric_id, class_id, role) + INDEX(class_id)。

P1 指标技术形态治理（spec §5.2 / §5.5）。

- UNIQUE 三元组：同一指标挂同一类同一角色只能一行；同 (metric_id, class_id)
  不同 role 允许（一个类可同时是分子源与过滤源）。
- INDEX(class_id)：按类反查指标（本体管理页「这个类被哪些指标引用」）。
- class_id 的 FK 已是 RESTRICT（0136 建表时带的），本迁移不动。
"""

from __future__ import annotations

from alembic import op

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None

_TABLE = "ontology_metric_source"
_UNIQUE_NAME = "uq_metric_source_triple"
_INDEX_NAME = "ix_metric_source_class_id"


def upgrade() -> None:
    op.create_unique_constraint(
        _UNIQUE_NAME,
        _TABLE,
        ["metric_id", "class_id", "role"],
    )
    op.create_index(_INDEX_NAME, _TABLE, ["class_id"])


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name=_TABLE)
    op.drop_constraint(_UNIQUE_NAME, _TABLE, type_="unique")
