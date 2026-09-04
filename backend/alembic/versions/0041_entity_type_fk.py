"""三表 entity_type FK 化：entity_mapping / feature_definition / document_entity_relation.

预置校验：所有 entity_type 值必须 ∈ business_object.code；
非白名单值 → 中止（理论上不存在；防御性）。
"""
from __future__ import annotations

from alembic import op

revision = "0041_entity_type_fk"
down_revision = "0040_supplier_item_po_receipt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 校验：防御性检查（理论上所有存量值都已合法）
    for table in ("entity_mapping", "feature_definition", "document_entity_relation"):
        op.execute(
            f"""
            DO $$
            DECLARE
                bad_count INTEGER;
            BEGIN
                SELECT COUNT(*) INTO bad_count
                FROM {table} t
                WHERE t.entity_type IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM business_object b WHERE b.code = t.entity_type
                  );
                IF bad_count > 0 THEN
                    RAISE EXCEPTION 'Table % has % rows with invalid entity_type', '{table}', bad_count;
                END IF;
            END $$;
            """
        )

    # 加 FK
    for table in ("entity_mapping", "feature_definition", "document_entity_relation"):
        op.create_foreign_key(
            f"fk_{table}_entity_type",
            source_table=table,
            referent_table="business_object",
            local_cols=["entity_type"],
            remote_cols=["code"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    for table in ("entity_mapping", "feature_definition", "document_entity_relation"):
        op.drop_constraint(
            f"fk_{table}_entity_type", table, type_="foreignkey"
        )
