"""INSERT ontology_class IncomingInspection（结构性建模，0 行）.

NCR 不建本体类（user 拍板）；本迁移仅追加 IncomingInspection 一行。
重复运行幂等：WHERE NOT EXISTS 守卫。
"""
from __future__ import annotations

from alembic import op

revision = "0039_incoming_inspection_class"
down_revision = "0038_business_object"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO ontology_class (
                class_name, source_table, description, object_type,
                version, valid_from, created_by,
                created_time, updated_time
            )
        SELECT 'IncomingInspection',
               'DWD_INCOMING_INSPECTION',
               '来料检验（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）',
               'Transaction',
               1,
               now(),
               'seed',
               now(),
               now()
        WHERE NOT EXISTS (
            SELECT 1 FROM ontology_class WHERE class_name = 'IncomingInspection'
        )
    """)


def downgrade() -> None:
    op.execute(
        "DELETE FROM ontology_class WHERE class_name = 'IncomingInspection'"
    )