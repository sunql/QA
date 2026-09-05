"""unit/ 测试统一走真实 PostgreSQL（强制规则：Harness/rules/测试规范.md）。

覆盖根 conftest 的 sqlite fixtures；所有触 DB 测试的 engine/session 由本 conftest
通过 monkeypatch 注入到 seed_ontology 模块（不再各自创建 sqlite）。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import seed_ontology
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.tests import _pg_support


def _resolveTestDbUrl() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    dbUrl = os.environ.get("DATABASE_URL", "")
    if dbUrl.startswith("postgresql"):
        import re
        return re.sub(r"/[^/]*$", "/qa_metadata_test", dbUrl)
    raise RuntimeError(
        "unit/ 测试需要 TEST_DATABASE_URL（或 DATABASE_URL 指向真实 PostgreSQL），"
        "禁止 sqlite。"
    )


@pytest.fixture()
async def seedEngine(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[async_sessionmaker, AsyncSession]]:
    """真实 PG 引擎 + 会话，monkeypatch 进 seed_ontology 模块级函数。

    返回 (factory, engine)，seed() 内部会调用 engine.dispose()。
    测试可直接用 factory() 开新 session 做 assert。
    覆盖根 conftest 的 sqlite fixtures。
    """
    url = _resolveTestDbUrl()
    _pg_support._ensureSchema(url)
    engine = await _pg_support._newEngine()
    try:
        await _pg_support._truncateAll(engine)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
        # 替换 seed_ontology 的模块级函数，使 seed() 走真实 PG
        monkeypatch.setattr(seed_ontology, "getEngine", lambda: engine)
        monkeypatch.setattr(seed_ontology, "getSessionFactory", lambda: factory)
        yield factory, engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def warmBusinessObjectRegistry(dbSession: AsyncSession) -> AsyncIterator[None]:
    """每个 unit 测试前 warmUp businessObjectRegistry。

    Unit 测试不走 lifespan（TestClient/TestApp 不触发 startup），原 in-memory
    registry 未 warmed；BeforeValidator（_validateBusinessObjectCode）会因
    'Registry 未 warmUp' 抛 RuntimeError。

    seed_business_objects 先 upsert FK 目标行，确保 registry warmUp 能加载到数据。

    commit() 关掉 warmUp SELECT 留下的隐式事务，避免 AccessShareLock 阻塞
    并发测试的 TRUNCATE（d01a4e1 教训）。
    """
    from app.services.business_object_registry import businessObjectRegistry
    from scripts.seed_business_objects import seedBusinessObjects

    await seedBusinessObjects(dbSession)
    businessObjectRegistry.invalidate()
    await businessObjectRegistry.warmUp(dbSession)
    await dbSession.commit()
    yield
    businessObjectRegistry.invalidate()


# 别名：只用 session 的地方注入 dbSession 即可
@pytest.fixture()
async def dbSession(seedEngine) -> AsyncIterator[AsyncSession]:
    factory, _ = seedEngine
    async with factory() as session:
        yield session
