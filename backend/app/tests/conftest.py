"""pytest 共享 fixtures。

提供：
- settings：测试专用 Settings（用固定 Fernet 密钥）
- mockLlmClient：确定性 LLM 客户端
- pg_engine / db_session / pg_client：真实 PostgreSQL fixtures（强制规则：
  Harness/rules/测试规范.md — 禁止 sqlite 内存库，所有测试必须走真实 PG）

真实 PG fixtures 由 _pg_support.py 提供 pgApiClient 机制，integration 测试通过
conftest/integration/conftest.py 注入完整 API 链路客户端。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from pathlib import Path

import dotenv

# 优先从 .env 加载真实 PG URL（覆盖后续 sqlite setdefault）
# .env 在 backend/，conftest.py 在 backend/app/tests/，需 parents[2]
_backend_dotenv = Path(__file__).resolve().parents[2] / ".env"
dotenv.load_dotenv(_backend_dotenv, override=True)

# 在导入 app 之前设定测试环境变量
_TEST_FERNET = Fernet.generate_key().decode()
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SECRET_KEY", _TEST_FERNET)
os.environ.setdefault("SESSION_BUDGET", "0.1")
# 限流在测试环境强制关闭，避免影响既有用例；限流专项测试自行临时开启
os.environ["RATE_LIMIT_ENABLED"] = "false"
# Milvus 指向独立测试库：integration 的清理夹具会 drop 本体集合，若连默认库就是
# 删线上向量（2026-09-30 事故）。用 setdefault 之外还要覆盖：外部若不慎预设了
# 空值/默认库，测试必须仍然隔离。故直接赋值，并保留显式覆盖测试库名的能力。
os.environ["MILVUS_DB_NAME"] = os.environ.get("MILVUS_TEST_DB_NAME", "qa_test")

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.config import getSettings  # noqa: E402
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


# ---------------------------------------------------------------------------
# 真实 PostgreSQL fixtures（强制规则：Harness/rules/测试规范.md）
# ---------------------------------------------------------------------------

from app.tests._pg_support import _newEngine, _truncateAll  # noqa: E402


@pytest.fixture()
async def pg_engine(settings: Any) -> Any:
    """每个测试新建真实 PG 引擎，测试结束后 dispose。"""
    engine = await _newEngine()
    yield engine
    await engine.dispose()


@pytest.fixture()
async def db_session(pg_engine: Any) -> AsyncIterator[AsyncSession]:
    """提供真实 PG 会话（用于数据准备与断言）。"""
    factory = async_sessionmaker(
        pg_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with factory() as session:
        yield session


@pytest.fixture()
async def pg_client(pg_engine: Any) -> AsyncIterator[AsyncClient]:
    """完整 API 链路测试客户端：真实 PG + 每个测试前 truncate 清库 + 已认证。"""
    # truncate 清库（隔离测试）
    await _truncateAll(pg_engine)
    # 替换全局会话工厂，使 getDb() 走真实 PG
    old_factory = dbModule._sessionFactory
    old_engine = dbModule._engine
    factory = async_sessionmaker(
        pg_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    dbModule._sessionFactory = factory
    dbModule._engine = pg_engine
    try:
        from app.tests._testapp import buildTestApp

        transport = ASGITransport(app=buildTestApp(factory))
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={
                "X-User-Id": "test-user-001",
                "X-User-Departments": "IT,QA",
            },
        ) as ac:
            yield ac
    finally:
        dbModule._sessionFactory = old_factory
        dbModule._engine = old_engine
