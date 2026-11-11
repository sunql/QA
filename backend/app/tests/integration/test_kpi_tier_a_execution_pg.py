"""Tier A 执行器的真实 PG 行数上限（integration）：真实 asyncpg + 真实只读事务。

把测试元数据库自身当作业务库（不建任何表），用 generate_series 验证
execute_read_only(maxRows=...) 在真实连接上确实截断。
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DataSourceType
from app.domain.models import DataSource
from app.infrastructure.business_db_pool import dispose_adapter, get_adapter
from app.infrastructure.security.crypto import encryptApiKey
from app.tests._pg_support import resolveTestDatabaseUrl

pytestmark = pytest.mark.asyncio


async def _seedSelfDatasource(dbSession: AsyncSession) -> DataSource:
    """把测试元数据库自身当作业务库：真实 asyncpg + 真实只读事务，不建任何表。"""
    url = make_url(resolveTestDatabaseUrl())
    ds = DataSource(
        name="tier-a-self-pg",
        type=DataSourceType.POSTGRESQL,
        host=url.host,
        port=url.port or 5432,
        database_name=url.database,
        username=url.username,
        password_encrypted=encryptApiKey(url.password or ""),
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


async def test_max_rows_truncates_real_query(dbSession: AsyncSession) -> None:
    ds = await _seedSelfDatasource(dbSession)
    adapter = get_adapter(ds.id, ds)
    try:
        rows = await adapter.execute_read_only(
            "SELECT g AS n FROM generate_series(1, 50) AS g", maxRows=10
        )
    finally:
        await dispose_adapter(ds.id)
    assert len(rows) == 10
