"""fix(uq_wol_active): COALESCE null chunk_id so page-level links are unique.

wiki-ontology-link Task 2 Fix Round 1.

PostgreSQL B-tree treats NULL as distinct from NULL. The original partial unique
index on (page_id, chunk_id, ontology_type, ontology_id) did NOT enforce
uniqueness when chunk_id IS NULL, allowing duplicate page-level links to be
created. Fix by wrapping chunk_id in COALESCE(..., '').

down_revision: 0091_wiki_ontology_link
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision: str = "0092_fix_uq_wol_active"
down_revision: str = "0091_wiki_ontology_link"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Drop the broken partial unique index (NULLs not properly deduped)
    op.execute(text("DROP INDEX IF EXISTS uq_wol_active"))
    # Recreate with COALESCE: NULL chunk_id treated as '' for uniqueness
    op.execute(text(
        "CREATE UNIQUE INDEX uq_wol_active ON wiki_ontology_link ("
        "page_id, COALESCE(chunk_id, ''), ontology_type, ontology_id"
        ") WHERE revoked_time IS NULL"
    ))


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS uq_wol_active"))
    op.execute(text(
        "CREATE UNIQUE INDEX uq_wol_active ON wiki_ontology_link ("
        "page_id, chunk_id, ontology_type, ontology_id"
        ") WHERE revoked_time IS NULL"
    ))
