"""integration 测试统一走真实 PostgreSQL + 完整 API 链路（强制规则：Harness/rules/测试规范.md）。

覆盖根 conftest 的 sqlite client/dbSession fixtures：
- client：真实 PG + 每测试 TRUNCATE + buildTestApp，从 HTTP 入口走完整链路
- dbSession：真实 PG 会话（与 client 同一引擎/工厂），供 Arrange 造数 / Assert 验库

每个测试独立引擎并在结束 dispose（pytest-asyncio 每测试独立事件循环，避免 loop 错配）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.infrastructure import database as dbModule
from app.infrastructure.llm.embedding_provider_factory import resetEmbeddingClientCache
from app.infrastructure.security import crypto
from app.tests import _pg_support


@pytest.fixture()
async def client() -> AsyncIterator[AsyncClient]:
    """完整 API 链路客户端：真实 PG（覆盖根 conftest 的 sqlite 内存库 client）。"""
    getSettings.cache_clear()
    crypto.resetFernet()
    resetEmbeddingClientCache()  # embedding provider 缓存跨测试清理（seed 数据会被 TRUNCATE）
    async for ac in _pg_support.pgApiClient():
        yield ac


@pytest.fixture()
async def dbSession(client: AsyncClient) -> AsyncIterator[AsyncSession]:
    """真实 PG 会话：依赖 client 确保 pgApiClient 已替换全局会话工厂。"""
    factory = dbModule.getSessionFactory()
    async with factory() as session:
        yield session
