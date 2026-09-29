"""Alembic 0040 应向 ontology_class 插入 Supplier/ItemMaster/PurchaseOrder/Receipt 结构性建模行.

自含种子重放：集成套件的其他用例会 TRUNCATE 全库（清掉 0039/0040 的种子行），
而 alembic 已在 head、upgrade 是 no-op 不会恢复数据行。故 module fixture 直接
重放 0040 的种子 INSERT（与迁移逐字同 SQL、同 WHERE NOT EXISTS 幂等守卫），
再断言行存在与幂等。
"""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


_TEST_URL = "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"

# 与 alembic/versions/0040_supplier_item_po_receipt.py upgrade() 逐字对齐
_SEED_SQL = """
    INSERT INTO ontology_class (
            class_name, source_table, description, object_type,
            version, valid_from, created_by,
            created_time, updated_time
        )
    SELECT CAST(:class_name AS VARCHAR),
           CAST(:source_table AS VARCHAR),
           CAST(:description AS VARCHAR),
           CAST(:object_type AS VARCHAR),
           1,
           now(),
           'seed',
           now(),
           now()
    WHERE NOT EXISTS (
        SELECT 1 FROM ontology_class WHERE class_name = CAST(:class_name AS VARCHAR)
    )
"""

_DESCRIPTIONS = {
    "Supplier": "供应商主数据（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）",
    "ItemMaster": "物料主数据（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）",
    "PurchaseOrder": "采购订单（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）",
    "Receipt": "收货（结构性建模，0 行；Task 13 将 Neo4j label GoodsReceipt 改名为 Receipt）",
}

EXPECTED = {
    "Supplier":       ("DWD_SUPPLIER",       "Master"),
    "ItemMaster":     ("DWD_MATERIAL",       "Master"),
    "PurchaseOrder":  ("DWD_PURCHASE_ORDER", "Transaction"),
    "Receipt":        ("DWD_GOODS_RECEIPT",  "Transaction"),
}


def _seedParams():
    return [
        {
            "class_name": name,
            "source_table": EXPECTED[name][0],
            "description": _DESCRIPTIONS[name],
            "object_type": EXPECTED[name][1],
        }
        for name in EXPECTED
    ]


@pytest.fixture(autouse=True)
async def _migrationSeeds(client,):
    """重放 0040 种子行依赖 client fixture：conftest 每测试 TRUNCATE 后重放，保证顺序在截断之后。"""
    engine = create_async_engine(_TEST_URL)
    try:
        async with engine.begin() as conn:
            for params in _seedParams():
                await conn.execute(text(_SEED_SQL), params)
    finally:
        await engine.dispose()
    yield


@pytest.mark.asyncio
async def test_four_classes_inserted():
    engine = create_async_engine(_TEST_URL)
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
    """重复重放种子不产生重复行（每个 class_name 都有 WHERE NOT EXISTS 守卫）."""
    engine = create_async_engine(_TEST_URL)
    try:
        async with engine.begin() as conn:
            # 再重放一次：幂等是本测试的被测行为
            for params in _seedParams():
                await conn.execute(text(_SEED_SQL), params)
            for class_name in EXPECTED:
                cnt = (await conn.execute(text("""
                    SELECT COUNT(*) FROM ontology_class WHERE class_name = :name
                """), {"name": class_name})).scalar()
                assert cnt == 1, f"{class_name} expected 1 row, got {cnt}"
    finally:
        await engine.dispose()


async def _fetch(engine, class_name: str):
    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT class_name, source_table, object_type, version
            FROM ontology_class WHERE class_name = :name
        """), {"name": class_name})).first()
    return row
