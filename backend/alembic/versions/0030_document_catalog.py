"""document_catalog + document_entity_relation（Phase 5.1）。

文档目录表（document_catalog）：存储文档元数据，支持 CONTRACT / 8D_REPORT /
AUDIT_REPORT / SPEC / SOP 等类型，含 security_level（L1/L2/L3）控制
向量检索权限。

文档-实体关联表（document_entity_relation）：文档与业务实体（SUPPLIER /
MATERIAL / PO / GR / IQC 等）的多对多关联，用于「某供应商有哪些合同」类
查询，以及 RAG 检索时按实体权限过滤。

关联唯一约束：同一文档对同一实体同一关系类型至多一条。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030_document_catalog"
down_revision: str | None = "0029_audit_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # document_catalog
    # -------------------------------------------------------------------------
    op.create_table(
        "document_catalog",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.String(length=50), nullable=False),
        sa.Column("document_name", sa.String(length=255), nullable=False),
        sa.Column(
            "document_type",
            sa.String(length=30),
            nullable=False,
            server_default="CONTRACT",
        ),
        sa.Column("version", sa.String(length=20), nullable=False, server_default="v1.0"),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column("owner", sa.String(length=100), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("security_level", sa.String(length=10), nullable=False, server_default="L1"),
        sa.Column("storage_url", sa.String(length=512), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", name="uq_document_id"),
        sa.CheckConstraint(
            "security_level IN ('L1','L2','L3')",
            name="ck_document_security_level",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','EXPIRED','ARCHIVED')",
            name="ck_document_status",
        ),
    )
    op.create_index(
        "ix_document_catalog_type", "document_catalog", ["document_type"]
    )
    op.create_index(
        "ix_document_catalog_entity", "document_catalog", ["document_id"]
    )
    op.create_index(
        "ix_document_catalog_security", "document_catalog", ["security_level"]
    )

    # -------------------------------------------------------------------------
    # document_entity_relation
    # -------------------------------------------------------------------------
    op.create_table(
        "document_entity_relation",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.String(length=50), nullable=False),
        sa.Column(
            "entity_type",
            sa.String(length=30),
            nullable=False,
        ),
        sa.Column("entity_key", sa.BigInteger(), nullable=False),
        sa.Column(
            "relation_type",
            sa.String(length=30),
            nullable=False,
            server_default="CONTRACT",
        ),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "entity_type", "entity_key", "relation_type",
            name="uq_doc_entity_rel",
        ),
    )
    op.create_index(
        "ix_doc_rel_entity", "document_entity_relation", ["entity_type", "entity_key"]
    )
    op.create_index(
        "ix_doc_rel_document", "document_entity_relation", ["document_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_doc_rel_document", table_name="document_entity_relation")
    op.drop_index("ix_doc_rel_entity", table_name="document_entity_relation")
    op.drop_table("document_entity_relation")
    op.drop_index("ix_document_catalog_security", table_name="document_catalog")
    op.drop_index("ix_document_catalog_entity", table_name="document_catalog")
    op.drop_index("ix_document_catalog_type", table_name="document_catalog")
    op.drop_table("document_catalog")
