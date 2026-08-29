"""ontology_property_aliases - 2-2 本体属性业务别名与描述。

为 ontology_property 增加：
- business_aliases：业务别名/同义词列表（JSON，生产 PG 落 JSONB），供 schema 文本消歧缩写列名。
- description：列含义描述。

两列均可空，历史数据无需 backfill；版本提升克隆属性时由业务层携带。
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_ontology_property_aliases"
down_revision = "0008_ontology_class_versioning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ontology_property",
        sa.Column(
            "business_aliases",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.add_column(
        "ontology_property",
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ontology_property", "description")
    op.drop_column("ontology_property", "business_aliases")
