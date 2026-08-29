"""pytest 共享 fixtures。

提供：
- settings：测试专用 Settings（用固定 Fernet 密钥）
- dbEngine / dbSession：内存 SQLite 异步引擎与会话【过渡遗留】
- client：FastAPI TestClient（基于内存 DB）【过渡遗留】
- mockLlmClient：确定性 LLM 客户端

【过渡遗留】sqlite 内存库 fixtures 仅服务尚未迁移的存量测试。按
Harness/rules/测试规范.md 强制规则，新测试与迁移批次必须用真实 PostgreSQL +
完整 API 链路，见 app/tests/_pg_support.py（pgApiClient）。逐步迁移直至清零 sqlite。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

# 在导入 app 之前设定测试环境变量
_TEST_FERNET = Fernet.generate_key().decode()
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", _TEST_FERNET)
os.environ.setdefault("SESSION_BUDGET", "0.1")
# 限流在测试环境强制关闭，避免影响既有用例；限流专项测试自行临时开启
os.environ["RATE_LIMIT_ENABLED"] = "false"

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.config import getSettings  # noqa: E402
from app.domain.models import Base  # noqa: E402
from app.infrastructure import database as dbModule  # noqa: E402
from app.infrastructure.security import crypto  # noqa: E402


@pytest.fixture(scope="session")
def eventLoop() -> Iterator[asyncio.AbstractEventLoop]:
    """会话级事件循环。"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture()
def settings() -> Any:
    """返回测试 Settings（重置缓存确保使用测试环境变量）。"""
    getSettings.cache_clear()
    crypto.resetFernet()
    return getSettings()


@pytest.fixture()
async def dbEngine(settings: Any) -> AsyncIterator[Any]:
    """内存 SQLite 异步引擎，每个测试函数重建表。使用 StaticPool 保证单连接共享同一内存库。"""
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 临时替换全局引擎/会话工厂，使 getDb 依赖使用测试库
    dbModule._engine = engine
    dbModule._sessionFactory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    dbModule._engine = None
    dbModule._sessionFactory = None


@pytest.fixture()
async def dbSession(dbEngine: Any) -> AsyncIterator[AsyncSession]:
    """提供独立的数据库会话。"""
    factory = dbModule.getSessionFactory()
    async with factory() as session:
        yield session


@pytest.fixture()
async def client(dbEngine: Any) -> AsyncIterator[AsyncClient]:
    """FastAPI 异步测试客户端（基于内存 DB + 重置设置缓存）。"""
    getSettings.cache_clear()
    crypto.resetFernet()

    from app.tests._testapp import buildTestApp

    # dbEngine fixture 已把 dbModule._sessionFactory 换成内存库测试工厂
    testFactory = dbModule.getSessionFactory()
    transport = ASGITransport(app=buildTestApp(testFactory))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class MockLlmClient:
    """确定性 LLM 客户端，供测试注入。"""

    def __init__(self, responseText: str = "mocked response", usage: dict[str, int] | None = None) -> None:
        self.responseText = responseText
        self.usage = usage or {"prompt_tokens": 10, "completion_tokens": 5}
        self.callCount = 0
        self.lastMessages: list[dict[str, Any]] | None = None

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.callCount += 1
        self.lastMessages = messages

        class _Usage:
            prompt_tokens = self.usage.get("prompt_tokens", 0)
            completion_tokens = self.usage.get("completion_tokens", 0)
            total_tokens = prompt_tokens + completion_tokens

        class _Resp:
            content = self.responseText
            usage = _Usage()
            modelName = kwargs.get("model", "mock-model")

        return _Resp()


@pytest.fixture()
def mockLlmClient() -> MockLlmClient:
    return MockLlmClient()
