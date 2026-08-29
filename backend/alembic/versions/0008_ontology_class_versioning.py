"""ontology_class_versioning - Phase 6 本体类版本管理。

为 ontology_class 增加 version / valid_from / valid_to 列，并：
- 移除旧的 class_name 唯一约束（同一类名可有多版本）。
- 新增 (class_name, version) 联合唯一约束。
- 新增 valid_to 索引（默认按有效时间窗过滤）。
- backfill 现有记录：version=1, valid_from=now(), valid_to=NULL。
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_ontology_class_versioning"
down_revision = "0007_add_oracle_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 新增三列。valid_from 先 nullable，backfill 后再 NOT NULL，避免大表重写失败。
    op.add_column(
        "ontology_class",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "ontology_class",
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ontology_class",
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
    )

    # 2. backfill 现有行：valid_from 填当前时刻（这些行原本没有时间窗，按现役类处理）
    op.execute("UPDATE ontology_class SET valid_from = NOW() WHERE valid_from IS NULL")

    # 3. 收紧 valid_from 为 NOT NULL
    op.alter_column("ontology_class", "valid_from", nullable=False)

    # 4. 移除旧的 class_name 唯一约束、加 (class_name, version) 联合唯一约束
    op.drop_constraint("uq_class_name", "ontology_class", type_="unique")
    op.create_unique_constraint(
        "uq_ontology_class_name_version", "ontology_class", ["class_name", "version"]
    )

    # 5. 索引：listClasses 默认按 valid_to IS NULL 过滤
    op.create_index("ix_ontology_class_valid_to", "ontology_class", ["valid_to"])


def downgrade() -> None:
    op.drop_index("ix_ontology_class_valid_to", table_name="ontology_class")
    op.drop_constraint("uq_ontology_class_name_version", "ontology_class", type_="unique")
    op.create_unique_constraint("uq_class_name", "ontology_class", ["class_name"])
    op.drop_column("ontology_class", "valid_to")
    op.alter_column("ontology_class", "valid_from", nullable=True)
    op.drop_column("ontology_class", "valid_from")
    op.drop_column("ontology_class", "version")
