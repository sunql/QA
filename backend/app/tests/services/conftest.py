"""services 目录测试统一走真实 PostgreSQL（强制规则：Harness/rules/测试规范.md）。

覆盖 root conftest 的 sqlite dbSession fixture：
services/ 目录内的测试如果触 DB，必须使用真实 PG（由 integration/conftest.py 的
pgApiClient 机制注入的全局会话工厂），禁止 sqlite。

每个测试在真实 PG 上独立运行（TRUNCATE 隔离）。
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.dependencies import CurrentUser
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.infrastructure import database as dbModule
from app.tests import _pg_support

ADMIN_ROLE = "admin"
_ADMIN = CurrentUser(userId="t-admin", roles=(ADMIN_ROLE,), departments=())


def _resolveTestDbUrl() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    dbUrl = os.environ.get("DATABASE_URL", "")
    if dbUrl.startswith("postgresql"):
        return re.sub(r"/[^/]*$", "/qa_metadata_test", dbUrl)
    raise RuntimeError(
        "services/ 测试需要 TEST_DATABASE_URL（或 DATABASE_URL 指向真实 PostgreSQL），"
        "禁止 sqlite。示例：TEST_DATABASE_URL=postgresql+asyncpg://user:pw@localhost:5432/qa_metadata_test"
    )


@pytest.fixture()
async def dbSession() -> AsyncIterator[AsyncSession]:
    """真实 PG 会话（覆盖 root conftest 的 sqlite dbSession）。

    使用与 integration/ 测试相同的 pgApiClient 机制：
    - 每个测试新建引擎（避免 loop 错配）
    - 对测试库执行 Alembic upgrade head（与生产同路径）
    - TRUNCATE 清库隔离
    """
    url = _resolveTestDbUrl()
    _pg_support._ensureSchema(url)
    engine = await _pg_support._newEngine()
    try:
        await _pg_support._truncateAll(engine)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
        # 替换全局工厂（使 getDb 依赖走真实 PG）
        old_factory = dbModule._sessionFactory
        old_engine = dbModule._engine
        dbModule._sessionFactory = factory
        dbModule._engine = engine
        try:
            async with factory() as session:
                yield session
        finally:
            dbModule._sessionFactory = old_factory
            dbModule._engine = old_engine
    finally:
        await engine.dispose()
