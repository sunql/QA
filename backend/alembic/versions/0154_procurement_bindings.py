"""采购域源绑定：`procurement_source_binding` / `procurement_property_binding`（0154）。

设计来源 `docs/update/1118/M0/A8-采购源绑定候选与表结构设计.md` §4；
决策 D2（2026-11-18）：两张表，复合键 / 有效期 / 过滤白名单走**独立列**，不进 JSON。

- `procurement_source_binding`：本体类 → 数据源 / schema / 物理表。
  唯一键**必须含 `class_id`** —— 实测 `DWD_SUPPLIER_PRICE_LIST` 与
  `DWD_SUPPLIER_PRICE_LIST_DEL` 指向同一张表（同表双类）。
- `procurement_property_binding`：属性 → 列 + 复合键位（`key_ordinal`）。
  没有它就表达不了「同属性多源 / 列级变换 / 4 列复合主键」。

约束：

- `uq_procurement_source_binding_active` / `uq_procurement_property_binding_active`：
  **部分唯一索引**（`valid_to IS NULL`）⇒ 同一键位只允许一条生效绑定。
- `uq_procurement_property_binding_key_ordinal`：同一绑定内键序号不得重复。
- `ck_*_candidate` / `ck_*_confirmed`：候选 ⇔ 无确认人（D-3 库层隔离）。
- FK 指向本体 / 数据源一律 `RESTRICT`；属性绑定 → 源绑定 `CASCADE`（生命周期从属）。

**手写**（禁 autogenerate）：纯加法 DDL，只建两张新表，不触碰任何既有表。
列 `comment` 与 CHECK 表达式与 `app/domain/procurement_binding_models.py` 的 ORM 声明
**逐字一致**（单测 `app/tests/unit/test_procurement_binding_models.py` 双向钉住）。

迁移号说明：head 0153 → 本批 0154。原规划的 0154 留给 WP-ONT-AI A4-0，但 A4 征集暂缓
且无任何分支占号 ⇒ 按「串行占号、不许插队」规则由本批先占；A4-0 落地时取当时的下一个空号。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0154"
down_revision = "0153"
branch_labels = None
depends_on = None

_TABLE_SOURCE = "procurement_source_binding"
_TABLE_PROPERTY = "procurement_property_binding"

# 与 `app/domain/procurement_binding_models.py` 的 ORM 列注释逐字一致（单测双向钉住）。
COLUMN_COMMENTS = {
    "source_class": "0154，M1：本体类（ontology_class.id；同表双类靠 class_id 区分）",
    "source_datasource": "0154，M1：数据源（data_source.id）",
    "source_schema": "0154，M1：schema / owner（Oracle owner、PG schema、MySQL database）",
    "source_table": "0154，M1：物理表名（可 ≠ class_name）",
    "source_role": "0154，M1：绑定角色（复用 MetricSourceRole：BASE / NUMERATOR / DENOMINATOR / FILTER）",
    "source_filter": "0154，M1：源表行级谓词（指标级 filter_expression 之外的行过滤）",
    "source_allowlist": "0154，M1：过滤表达式允许出现的标识符/函数白名单（D2：独立列，不进 JSON）",
    "source_candidate": "0154，M1：候选标记（TRUE = 未经确认，不得进入任何读路径 / manifest）",
    "source_confirmed_by": "0154，M1：确认人（问卷核对后填写；候选行为空）",
    "source_confirmed_time": "0154，M1：确认时刻",
    "source_valid_from": "0154，M1：生效起始时刻",
    "source_valid_to": "0154，M1：失效时刻（NULL = 当前生效）",
    "property_source": "0154，M1：所属源绑定（procurement_source_binding.id；生命周期从属 ⇒ CASCADE）",
    "property_property": "0154，M1：本体属性（ontology_property.id）",
    "property_column": "0154，M1：物理列名（允许 ≠ ontology_property.source_column）",
    "property_key_ordinal": "0154，M1：复合键位（1..n；NULL = 非键列）",
    "property_transform": "0154，M1：列级表达式（TRUNC / UPPER / 单位换算；为空 = 直读）",
}

# 与 ORM 逐字一致的 CHECK 表达式。
CHECK_EXPRESSIONS = {
    "source_role": "binding_role IN ('BASE','NUMERATOR','DENOMINATOR','FILTER')",
    "source_valid_range": "valid_to IS NULL OR valid_to > valid_from",
    "source_candidate": "NOT is_candidate OR confirmed_by IS NULL",
    "source_confirmed": "is_candidate OR (confirmed_by IS NOT NULL AND confirmed_time IS NOT NULL)",
    "property_key_ordinal": "key_ordinal IS NULL OR key_ordinal >= 1",
    "property_valid_range": "valid_to IS NULL OR valid_to > valid_from",
    "property_candidate": "NOT is_candidate OR confirmed_by IS NULL",
    "property_confirmed": "is_candidate OR (confirmed_by IS NOT NULL AND confirmed_time IS NOT NULL)",
}


def upgrade() -> None:
    op.create_table(
        _TABLE_SOURCE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "class_id",
            sa.BigInteger(),
            nullable=False,
            comment=COLUMN_COMMENTS["source_class"],
        ),
        sa.Column(
            "datasource_id",
            sa.BigInteger(),
            nullable=False,
            comment=COLUMN_COMMENTS["source_datasource"],
        ),
        sa.Column(
            "schema_name",
            sa.String(length=100),
            nullable=False,
            comment=COLUMN_COMMENTS["source_schema"],
        ),
        sa.Column(
            "source_table",
            sa.String(length=100),
            nullable=False,
            comment=COLUMN_COMMENTS["source_table"],
        ),
        sa.Column(
            "binding_role",
            sa.String(length=20),
            nullable=False,
            comment=COLUMN_COMMENTS["source_role"],
        ),
        sa.Column(
            "filter_expression",
            sa.Text(),
            nullable=True,
            comment=COLUMN_COMMENTS["source_filter"],
        ),
        sa.Column(
            "filter_allowlist",
            postgresql.ARRAY(sa.String(length=100)),
            nullable=True,
            comment=COLUMN_COMMENTS["source_allowlist"],
        ),
        sa.Column(
            "is_candidate",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment=COLUMN_COMMENTS["source_candidate"],
        ),
        sa.Column(
            "confirmed_by",
            sa.String(length=64),
            nullable=True,
            comment=COLUMN_COMMENTS["source_confirmed_by"],
        ),
        sa.Column(
            "confirmed_time",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=COLUMN_COMMENTS["source_confirmed_time"],
        ),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment=COLUMN_COMMENTS["source_valid_from"],
        ),
        sa.Column(
            "valid_to",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=COLUMN_COMMENTS["source_valid_to"],
        ),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["class_id"],
            ["ontology_class.id"],
            name="fk_procurement_source_binding_class",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["data_source.id"],
            name="fk_procurement_source_binding_datasource",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["source_role"],
            name="ck_procurement_source_binding_role",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["source_valid_range"],
            name="ck_procurement_source_binding_valid_range",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["source_candidate"],
            name="ck_procurement_source_binding_candidate",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["source_confirmed"],
            name="ck_procurement_source_binding_confirmed",
        ),
    )
    op.create_index(
        "uq_procurement_source_binding_active",
        _TABLE_SOURCE,
        ["class_id", "datasource_id", "schema_name", "source_table", "binding_role"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.create_index(
        "ix_procurement_source_binding_table",
        _TABLE_SOURCE,
        ["datasource_id", "schema_name", "source_table"],
    )
    op.create_index(
        "ix_procurement_source_binding_class",
        _TABLE_SOURCE,
        ["class_id"],
    )

    op.create_table(
        _TABLE_PROPERTY,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_binding_id",
            sa.BigInteger(),
            nullable=False,
            comment=COLUMN_COMMENTS["property_source"],
        ),
        sa.Column(
            "property_id",
            sa.BigInteger(),
            nullable=False,
            comment=COLUMN_COMMENTS["property_property"],
        ),
        sa.Column(
            "source_column",
            sa.String(length=100),
            nullable=False,
            comment=COLUMN_COMMENTS["property_column"],
        ),
        sa.Column(
            "key_ordinal",
            sa.SmallInteger(),
            nullable=True,
            comment=COLUMN_COMMENTS["property_key_ordinal"],
        ),
        sa.Column(
            "transform_expression",
            sa.Text(),
            nullable=True,
            comment=COLUMN_COMMENTS["property_transform"],
        ),
        sa.Column(
            "is_candidate",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment=COLUMN_COMMENTS["source_candidate"],
        ),
        sa.Column(
            "confirmed_by",
            sa.String(length=64),
            nullable=True,
            comment=COLUMN_COMMENTS["source_confirmed_by"],
        ),
        sa.Column(
            "confirmed_time",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=COLUMN_COMMENTS["source_confirmed_time"],
        ),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment=COLUMN_COMMENTS["source_valid_from"],
        ),
        sa.Column(
            "valid_to",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=COLUMN_COMMENTS["source_valid_to"],
        ),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["source_binding_id"],
            [f"{_TABLE_SOURCE}.id"],
            name="fk_procurement_property_binding_source",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["ontology_property.id"],
            name="fk_procurement_property_binding_property",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["property_key_ordinal"],
            name="ck_procurement_property_binding_key_ordinal",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["property_valid_range"],
            name="ck_procurement_property_binding_valid_range",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["property_candidate"],
            name="ck_procurement_property_binding_candidate",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["property_confirmed"],
            name="ck_procurement_property_binding_confirmed",
        ),
    )
    op.create_index(
        "uq_procurement_property_binding_active",
        _TABLE_PROPERTY,
        ["source_binding_id", "property_id"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.create_index(
        "uq_procurement_property_binding_key_ordinal",
        _TABLE_PROPERTY,
        ["source_binding_id", "key_ordinal"],
        unique=True,
        postgresql_where=sa.text("key_ordinal IS NOT NULL AND valid_to IS NULL"),
    )
    op.create_index(
        "ix_procurement_property_binding_property",
        _TABLE_PROPERTY,
        ["property_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_procurement_property_binding_property", table_name=_TABLE_PROPERTY
    )
    op.drop_index(
        "uq_procurement_property_binding_key_ordinal", table_name=_TABLE_PROPERTY
    )
    op.drop_index(
        "uq_procurement_property_binding_active", table_name=_TABLE_PROPERTY
    )
    op.drop_table(_TABLE_PROPERTY)

    op.drop_index("ix_procurement_source_binding_class", table_name=_TABLE_SOURCE)
    op.drop_index("ix_procurement_source_binding_table", table_name=_TABLE_SOURCE)
    op.drop_index("uq_procurement_source_binding_active", table_name=_TABLE_SOURCE)
    op.drop_table(_TABLE_SOURCE)
