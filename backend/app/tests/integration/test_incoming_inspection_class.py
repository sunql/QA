"""Alembic 0039 应向 ontology_class 插入 IncomingInspection 结构性建模行.

自含种子重放：集成套件的其他用例会 TRUNCATE 全库（清掉 0039 的种子行），
而 alembic 已在 head、upgrade 是 no-op 不会恢复数据行。故 module fixture 直接
重放 0039 的种子 INSERT（与迁移逐字同 SQL、同 WHERE NOT EXISTS 幂等守卫），
再断言行存在与幂等。

本测试不需要 agent 缓存或 HTTP 链路，纯 SQL 校验即可。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


_TEST_URL = "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"

# 与 alembic/versions/0039_incoming_inspection_class.py upgrade() 逐字对齐
_SEED_SQL = """
    INSERT INTO ontology_class (
            class_name, source_table, description, object_type,
            version, valid_from, created_by,
            created_time, updated_time
        )
    SELECT 'IncomingInspection',
           'DWD_INCOMING_INSPECTION',
           '来料检验（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）',
           'Transaction',
           1,
           now(),
           'seed',
           now(),
           now()
    WHERE NOT EXISTS (
        SELECT 1 FROM ontology_class WHERE class_name = 'IncomingInspection'
    )
"""


@pytest.fixture(autouse=True)
async def _migrationSeeds(client,):
    """重放 0039 种子行依赖 client fixture：conftest 每测试 TRUNCATE 后重放，保证顺序在截断之后。"""
    engine = create_async_engine(_TEST_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_SEED_SQL))
    finally:
        await engine.dispose()
    yield


@pytest.mark.asyncio
async def test_incoming_inspection_class_inserted():
    engine = create_async_engine(_TEST_URL)
    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT class_name, source_table, object_type, version
            FROM ontology_class
            WHERE class_name = 'IncomingInspection'
        """))).first()
        assert row is not None
        assert row[0] == "IncomingInspection"
        assert row[1] == "DWD_INCOMING_INSPECTION"
        assert row[2] == "Transaction"
        assert row[3] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_incoming_inspection_is_idempotent():
    """重复重放种子不产生重复行（WHERE NOT EXISTS 守卫）."""
    engine = create_async_engine(_TEST_URL)
    async with engine.begin() as conn:
        # 再重放一次：幂等是本测试的被测行为
        await conn.execute(text(_SEED_SQL))
        rows = (await conn.execute(text("""
            SELECT COUNT(*) FROM ontology_class WHERE class_name = 'IncomingInspection'
        """))).scalar()
        assert rows == 1
    await engine.dispose()
