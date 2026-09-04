"""Alembic 0040 应向 ontology_class 插入 Supplier/ItemMaster/PurchaseOrder/Receipt 结构性建模行.

注意：conftest 的 warmAgentCaches(autouse=True) 会 TRUNCATE 所有表，
所以本文件用 module-level warmAgentCaches(autouse=False) override 中和。
"""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


# 中和 conftest 的 warmAgentCaches(autouse=True) — 后者会 TRUNCATE ontology_class
@pytest.fixture(autouse=False)
def warmAgentCaches():
    """Module-level override (no-op); pytest resolves closer-scope over conftest."""
    return None


EXPECTED = {
    "Supplier":       ("DWD_SUPPLIER",       "Master"),
    "ItemMaster":     ("DWD_MATERIAL",       "Master"),
    "PurchaseOrder":  ("DWD_PURCHASE_ORDER", "Transaction"),
    "Receipt":        ("DWD_GOODS_RECEIPT",  "Transaction"),
}


async def _fetch(engine, class_name: str):
    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT class_name, source_table, object_type, version
            FROM ontology_class WHERE class_name = :name
        """), {"name": class_name})).first()
    return row


@pytest.mark.asyncio
async def test_four_classes_inserted():
    engine = create_async_engine(
        "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"
    )
    try:
        for class_name, (source_table, object_type) in EXPECTED.items():
            row = await _fetch(engine, class_name)
            assert row is not None, f"{class_name} row missing"
            assert row[0] == class_name
            assert row[1] == source_table, f"{class_name} source_table mismatch"
            assert row[2] == object_type, f"{class_name} object_type mismatch"
            assert row[3] == 1, f"{class_name} version mismatch"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_idempotent_no_duplicates():
    """重复运行 0040 不会重复插入（每个 class_name 都用 WHERE NOT EXISTS 守卫）."""
    engine = create_async_engine(
        "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"
    )
    try:
        async with engine.connect() as conn:
            for class_name in EXPECTED:
                cnt = (await conn.execute(text("""
                    SELECT COUNT(*) FROM ontology_class WHERE class_name = :name
                """), {"name": class_name})).scalar()
                assert cnt == 1, f"{class_name} expected 1 row, got {cnt}"
    finally:
        await engine.dispose()
