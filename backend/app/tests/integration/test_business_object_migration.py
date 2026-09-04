"""Alembic 0038 应创建 business_object 表（含 unique + CHECK + 索引）。"""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_business_object_table_exists_with_constraints():
    engine = create_async_engine(
        "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"
    )
    async with engine.connect() as conn:
        rows = await conn.execute(text("""
            SELECT column_name, data_type, is_nullable, character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'business_object'
            ORDER BY ordinal_position
        """))
        cols = {r[0]: r for r in rows.fetchall()}
        assert "code" in cols
        assert cols["code"][1] == "character varying"
        assert cols["code"][3] == 20
        assert cols["code"][2] == "NO"
        assert "name" in cols
        assert cols["name"][3] == 100
        assert "header_class_id" in cols
        assert "graph_label" in cols
        assert "description" in cols

        pk_rows = await conn.execute(text("""
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'business_object'::regclass AND i.indisprimary
        """))
        pk_cols = [r[0] for r in pk_rows.fetchall()]
        assert pk_cols == ["code"]

        check_rows = await conn.execute(text("""
            SELECT conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'business_object'::regclass AND contype = 'c'
        """))
        checks = {r[0]: r[1] for r in check_rows.fetchall()}
        assert "ck_business_object_code_upper" in checks
        # PG renders `UPPER(code)` as `upper((code)::text)` (lowercase fn + explicit cast).
        # Both forms confirm the CHECK enforces uppercase normalization of the code column.
        rendered = checks["ck_business_object_code_upper"].lower()
        assert "upper(code)" in rendered or "upper((code)::text)" in rendered

        idx_rows = await conn.execute(text("""
            SELECT indexname FROM pg_indexes WHERE tablename = 'business_object'
        """))
        idxs = [r[0] for r in idx_rows.fetchall()]
        assert any("ix_business_object_header_class" in i for i in idxs)
