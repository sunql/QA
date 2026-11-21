"""采购域「类 → 源表 / 属性 → 列」绑定的 ORM 映射（M1-1.1）。

设计来源：``docs/update/1118/M0/A8-采购源绑定候选与表结构设计.md`` §4；
决策 D2（2026-11-18）：**两张表**，复合键 / 有效期 / 过滤白名单走**独立列**，不进 JSON。

两张表：

- ``procurement_source_binding``：一个本体类落到哪个数据源的哪张物理表。
  唯一键**必须含 ``class_id``** —— 实测 ``DWD_SUPPLIER_PRICE_LIST`` 与
  ``DWD_SUPPLIER_PRICE_LIST_DEL`` 指向同一张表（同表双类）。
- ``procurement_property_binding``：类属性落到该表的哪一列，以及复合主键的**列与序号**。
  没有这张表就无法表达「同属性多源 / 列级变换 / 4 列复合主键」。

三条贯穿两张表的设计纪律：

1. **候选 ≠ 结论**（D-3）：``is_candidate`` 是**库级**状态，候选行不得进入任何读路径 /
   manifest。CHECK 把「候选 ↔ 无确认人」钉死，不靠文档措辞。
2. **有效期是软版本化的唯一机制**：``valid_to IS NULL`` = 当前生效；同一键位只允许
   一条生效绑定（**部分唯一索引**，不是全表唯一）。
3. **证据不悬空**：指向 ``ontology_class`` / ``ontology_property`` / ``data_source`` 的
   FK 一律 ``RESTRICT``；唯一例外是属性绑定 → 源绑定用 ``CASCADE``
   （属性绑定的生命周期**从属于**源绑定）。

与迁移的对应：``alembic/versions/0154_procurement_bindings.py``。
列注释与 CHECK 表达式两边**逐字一致**（单测 ``test_procurement_binding_models.py`` 双向钉住）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntFk, BigIntPk, TimestampMixin


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- 源绑定

_SOURCE_CLASS_COMMENT = "0154，M1：本体类（ontology_class.id；同表双类靠 class_id 区分）"
_SOURCE_DATASOURCE_COMMENT = "0154，M1：数据源（data_source.id）"
_SOURCE_SCHEMA_COMMENT = "0154，M1：schema / owner（Oracle owner、PG schema、MySQL database）"
_SOURCE_TABLE_COMMENT = "0154，M1：物理表名（可 ≠ class_name）"
_SOURCE_ROLE_COMMENT = "0154，M1：绑定角色（复用 MetricSourceRole：BASE / NUMERATOR / DENOMINATOR / FILTER）"
_SOURCE_FILTER_COMMENT = "0154，M1：源表行级谓词（指标级 filter_expression 之外的行过滤）"
_SOURCE_ALLOWLIST_COMMENT = "0154，M1：过滤表达式允许出现的标识符/函数白名单（D2：独立列，不进 JSON）"
_SOURCE_CANDIDATE_COMMENT = "0154，M1：候选标记（TRUE = 未经确认，不得进入任何读路径 / manifest）"
_SOURCE_CONFIRMED_BY_COMMENT = "0154，M1：确认人（问卷核对后填写；候选行为空）"
_SOURCE_CONFIRMED_TIME_COMMENT = "0154，M1：确认时刻"
_SOURCE_VALID_FROM_COMMENT = "0154，M1：生效起始时刻"
_SOURCE_VALID_TO_COMMENT = "0154，M1：失效时刻（NULL = 当前生效）"


class ProcurementSourceBinding(Base, TimestampMixin):
    """一个本体类落到哪个数据源的哪张物理表。"""

    __tablename__ = "procurement_source_binding"
    __table_args__ = (
        CheckConstraint(
            "binding_role IN ('BASE','NUMERATOR','DENOMINATOR','FILTER')",
            name="ck_procurement_source_binding_role",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_procurement_source_binding_valid_range",
        ),
        CheckConstraint(
            "NOT is_candidate OR confirmed_by IS NULL",
            name="ck_procurement_source_binding_candidate",
        ),
        CheckConstraint(
            "is_candidate OR (confirmed_by IS NOT NULL AND confirmed_time IS NOT NULL)",
            name="ck_procurement_source_binding_confirmed",
        ),
        Index(
            "uq_procurement_source_binding_active",
            "class_id",
            "datasource_id",
            "schema_name",
            "source_table",
            "binding_role",
            unique=True,
            postgresql_where=sa_text("valid_to IS NULL"),
        ),
        Index(
            "ix_procurement_source_binding_table",
            "datasource_id",
            "schema_name",
            "source_table",
        ),
        Index("ix_procurement_source_binding_class", "class_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    class_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "ontology_class.id",
            ondelete="RESTRICT",
            name="fk_procurement_source_binding_class",
        ),
        nullable=False,
        comment=_SOURCE_CLASS_COMMENT,
    )
    datasource_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "data_source.id",
            ondelete="RESTRICT",
            name="fk_procurement_source_binding_datasource",
        ),
        nullable=False,
        comment=_SOURCE_DATASOURCE_COMMENT,
    )
    schema_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment=_SOURCE_SCHEMA_COMMENT
    )
    source_table: Mapped[str] = mapped_column(
        String(100), nullable=False, comment=_SOURCE_TABLE_COMMENT
    )
    binding_role: Mapped[str] = mapped_column(
        String(20), nullable=False, comment=_SOURCE_ROLE_COMMENT
    )
    filter_expression: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment=_SOURCE_FILTER_COMMENT
    )
    filter_allowlist: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(100)), nullable=True, comment=_SOURCE_ALLOWLIST_COMMENT
    )
    is_candidate: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=True,
        server_default=sa_text("true"),
        comment=_SOURCE_CANDIDATE_COMMENT,
    )
    confirmed_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment=_SOURCE_CONFIRMED_BY_COMMENT
    )
    confirmed_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment=_SOURCE_CONFIRMED_TIME_COMMENT
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=sa_text("now()"),
        comment=_SOURCE_VALID_FROM_COMMENT,
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment=_SOURCE_VALID_TO_COMMENT
    )
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ---------------------------------------------------------------- 属性绑定

_PROPERTY_SOURCE_COMMENT = "0154，M1：所属源绑定（procurement_source_binding.id；生命周期从属 ⇒ CASCADE）"
_PROPERTY_PROPERTY_COMMENT = "0154，M1：本体属性（ontology_property.id）"
_PROPERTY_COLUMN_COMMENT = "0154，M1：物理列名（允许 ≠ ontology_property.source_column）"
_PROPERTY_KEY_ORDINAL_COMMENT = "0154，M1：复合键位（1..n；NULL = 非键列）"
_PROPERTY_TRANSFORM_COMMENT = "0154，M1：列级表达式（TRUNC / UPPER / 单位换算；为空 = 直读）"
_PROPERTY_CANDIDATE_COMMENT = _SOURCE_CANDIDATE_COMMENT
_PROPERTY_CONFIRMED_BY_COMMENT = _SOURCE_CONFIRMED_BY_COMMENT
_PROPERTY_CONFIRMED_TIME_COMMENT = _SOURCE_CONFIRMED_TIME_COMMENT
_PROPERTY_VALID_FROM_COMMENT = _SOURCE_VALID_FROM_COMMENT
_PROPERTY_VALID_TO_COMMENT = _SOURCE_VALID_TO_COMMENT


class ProcurementPropertyBinding(Base, TimestampMixin):
    """类属性落到源表的哪一列，以及复合主键的列与序号。"""

    __tablename__ = "procurement_property_binding"
    __table_args__ = (
        CheckConstraint(
            "key_ordinal IS NULL OR key_ordinal >= 1",
            name="ck_procurement_property_binding_key_ordinal",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_procurement_property_binding_valid_range",
        ),
        CheckConstraint(
            "NOT is_candidate OR confirmed_by IS NULL",
            name="ck_procurement_property_binding_candidate",
        ),
        CheckConstraint(
            "is_candidate OR (confirmed_by IS NOT NULL AND confirmed_time IS NOT NULL)",
            name="ck_procurement_property_binding_confirmed",
        ),
        Index(
            "uq_procurement_property_binding_active",
            "source_binding_id",
            "property_id",
            unique=True,
            postgresql_where=sa_text("valid_to IS NULL"),
        ),
        Index(
            "uq_procurement_property_binding_key_ordinal",
            "source_binding_id",
            "key_ordinal",
            unique=True,
            postgresql_where=sa_text("key_ordinal IS NOT NULL AND valid_to IS NULL"),
        ),
        Index("ix_procurement_property_binding_property", "property_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    source_binding_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "procurement_source_binding.id",
            ondelete="CASCADE",
            name="fk_procurement_property_binding_source",
        ),
        nullable=False,
        comment=_PROPERTY_SOURCE_COMMENT,
    )
    property_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "ontology_property.id",
            ondelete="RESTRICT",
            name="fk_procurement_property_binding_property",
        ),
        nullable=False,
        comment=_PROPERTY_PROPERTY_COMMENT,
    )
    source_column: Mapped[str] = mapped_column(
        String(100), nullable=False, comment=_PROPERTY_COLUMN_COMMENT
    )
    key_ordinal: Mapped[int | None] = mapped_column(
        SmallInteger(), nullable=True, comment=_PROPERTY_KEY_ORDINAL_COMMENT
    )
    transform_expression: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment=_PROPERTY_TRANSFORM_COMMENT
    )
    is_candidate: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=True,
        server_default=sa_text("true"),
        comment=_PROPERTY_CANDIDATE_COMMENT,
    )
    confirmed_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment=_PROPERTY_CONFIRMED_BY_COMMENT
    )
    confirmed_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment=_PROPERTY_CONFIRMED_TIME_COMMENT
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=sa_text("now()"),
        comment=_PROPERTY_VALID_FROM_COMMENT,
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment=_PROPERTY_VALID_TO_COMMENT
    )
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
