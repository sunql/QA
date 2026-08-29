"""Phase 2: ontology_class, ontology_property, ontology_metric

Revision ID: 0002_ontology_tables
Revises: 0001_initial
Create Date: 2026-08-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_ontology_tables"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===== ontology_class =====
    op.create_table(
        "ontology_class",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("class_name", sa.String(length=100), nullable=False),
        sa.Column("class_alias", sa.String(length=100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_table", sa.String(length=100), nullable=True),
        sa.Column("parent_class_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.String(length=50), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("class_name", name="uq_class_name"),
        sa.ForeignKeyConstraint(
            ["parent_class_id"],
            ["ontology_class.id"],
            name="fk_class_parent",
        ),
    )
    op.create_index("idx_class_name", "ontology_class", ["class_name"])

    # ===== ontology_property =====
    op.create_table(
        "ontology_property",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("class_id", sa.BigInteger(), nullable=False),
        sa.Column("property_name", sa.String(length=100), nullable=False),
        sa.Column("property_alias", sa.String(length=100), nullable=True),
        sa.Column("data_type", sa.String(length=20), nullable=False),
        sa.Column("is_primary_key", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_foreign_key", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("ref_class_id", sa.BigInteger(), nullable=True),
        sa.Column("source_column", sa.String(length=100), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("class_id", "property_name", name="uq_class_property"),
        sa.ForeignKeyConstraint(
            ["class_id"], ["ontology_class.id"], name="fk_property_class"
        ),
        sa.ForeignKeyConstraint(
            ["ref_class_id"], ["ontology_class.id"], name="fk_property_ref_class"
        ),
    )
    op.create_index("idx_property_class", "ontology_property", ["class_id"])

    # ===== ontology_metric =====
    op.create_table(
        "ontology_metric",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("metric_name", sa.String(length=100), nullable=False),
        sa.Column("metric_alias", sa.String(length=100), nullable=True),
        sa.Column("formula", sa.Text(), nullable=False),
        sa.Column("agg_function", sa.String(length=20), nullable=False, server_default="SUM"),
        sa.Column("target_class_id", sa.BigInteger(), nullable=True),
        sa.Column("dimension_defaults", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=50), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("metric_name", name="uq_metric_name"),
        sa.ForeignKeyConstraint(
            ["target_class_id"],
            ["ontology_class.id"],
            name="fk_metric_target_class",
        ),
    )
    op.create_index("idx_metric_class", "ontology_metric", ["target_class_id"])


def downgrade() -> None:
    op.drop_index("idx_metric_class", table_name="ontology_metric")
    op.drop_table("ontology_metric")
    op.drop_index("idx_property_class", table_name="ontology_property")
    op.drop_table("ontology_property")
    op.drop_table("ontology_class")
