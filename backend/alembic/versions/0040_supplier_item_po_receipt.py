"""INSERT ontology_class Supplier/ItemMaster/PurchaseOrder/Receipt（结构性建模，0 行）.

补齐 5 业务对象 (Phase 4.4) 所需的剩余 4 个本体类。
重复运行幂等：每个 class_name 都用 WHERE NOT EXISTS 守卫。
"""
from __future__ import annotations

from alembic import op

revision = "0040_supplier_item_po_receipt"
down_revision = "0039_incoming_inspection_class"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO ontology_class (
                class_name, source_table, description, object_type,
                version, valid_from, created_by,
                created_time, updated_time
            )
        SELECT 'Supplier',
               'DWD_SUPPLIER',
               '供应商主数据（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）',
               'Master',
               1,
               now(),
               'seed',
               now(),
               now()
        WHERE NOT EXISTS (
            SELECT 1 FROM ontology_class WHERE class_name = 'Supplier'
        )
    """)
    op.execute("""
        INSERT INTO ontology_class (
                class_name, source_table, description, object_type,
                version, valid_from, created_by,
                created_time, updated_time
            )
        SELECT 'ItemMaster',
               'DWD_MATERIAL',
               '物料主数据（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）',
               'Master',
               1,
               now(),
               'seed',
               now(),
               now()
        WHERE NOT EXISTS (
            SELECT 1 FROM ontology_class WHERE class_name = 'ItemMaster'
        )
    """)
    op.execute("""
        INSERT INTO ontology_class (
                class_name, source_table, description, object_type,
                version, valid_from, created_by,
                created_time, updated_time
            )
        SELECT 'PurchaseOrder',
               'DWD_PURCHASE_ORDER',
               '采购订单（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）',
               'Transaction',
               1,
               now(),
               'seed',
               now(),
               now()
        WHERE NOT EXISTS (
            SELECT 1 FROM ontology_class WHERE class_name = 'PurchaseOrder'
        )
    """)
    op.execute("""
        INSERT INTO ontology_class (
                class_name, source_table, description, object_type,
                version, valid_from, created_by,
                created_time, updated_time
            )
        SELECT 'Receipt',
               'DWD_GOODS_RECEIPT',
               '收货（结构性建模，0 行；Task 13 将 Neo4j label GoodsReceipt 改名为 Receipt）',
               'Transaction',
               1,
               now(),
               'seed',
               now(),
               now()
        WHERE NOT EXISTS (
            SELECT 1 FROM ontology_class WHERE class_name = 'Receipt'
        )
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM ontology_class
        WHERE class_name IN ('Supplier', 'ItemMaster', 'PurchaseOrder', 'Receipt')
    """)
