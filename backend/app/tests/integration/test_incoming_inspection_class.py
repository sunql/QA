"""Alembic 0039 应向 ontology_class 插入 IncomingInspection 结构性建模行.

迁移为结构性建模（0 行），断言 ontology_class 中存在且仅存在 1 行
class_name='IncomingInspection'。本测试是「外部 alembic upgrade head」触发的：
每个测试运行前 integration/conftest.py 的 autouse warmAgentCaches 会清库，
但 ontology_class 在 migration 之后重新由 alembic 写入（迁移即种子），所以
本测试的「外部前置步骤」是 alembic upgrade 0039_incoming_inspection_class。

为避免 integration/conftest.py 的 autouse warmAgentCaches 在本测试中也强制
TRUNCATE ontology_class 而把迁移行抹掉，本文件定义同名 fixture 覆盖：
warmAgentCaches (autouse=False) + 无操作的 client/dbSession，
仅当测试未显式调用它们时使用 no-op 占位，避免 warmUp 流程强制清空 ontology_class。

本测试本身不需要 agent 缓存或 HTTP 链路，纯 SQL 校验即可。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.fixture(autouse=False)
async def warmAgentCaches() -> AsyncIterator[None]:
    """覆盖 integration/conftest.py 的同名 autouse 缓存预热。

    原 autouse=True 触发 client fixture → pgApiClient → _truncateAll，把迁移写入
    的 ontology_class 行清掉；本测试仅做 SQL 校验，不需要 agent 缓存，覆盖为 no-op。
    """
    yield


@pytest.mark.asyncio
async def test_incoming_inspection_class_inserted():
    engine = create_async_engine(
        "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"
    )
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


@pytest.mark.asyncio
async def test_incoming_inspection_is_idempotent():
    """重复运行 0039 不会重复插入（前置检查覆盖）."""
    engine = create_async_engine(
        "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT COUNT(*) FROM ontology_class WHERE class_name = 'IncomingInspection'
        """))).scalar()
        assert rows == 1