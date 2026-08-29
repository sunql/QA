"""数据库基础设施 - PostgreSQL 异步引擎与会话。

仅负责 QA 系统自身元数据库。业务数据源的动态引擎池在 Phase 3 的
datasource_service / 一个独立的 BusinessDbPool 中管理。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import getSettings

_engine: AsyncEngine | None = None
_sessionFactory: async_sessionmaker[AsyncSession] | None = None


def getEngine() -> AsyncEngine:
    """返回元数据库的异步引擎单例（懒加载）。"""
    global _engine
    if _engine is None:
        settings = getSettings()
        _engine = create_async_engine(
            settings.databaseUrl,
            echo=not settings.isProduction,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def getSessionFactory() -> async_sessionmaker[AsyncSession]:
    """返回会话工厂单例。"""
    global _sessionFactory
    if _sessionFactory is None:
        _sessionFactory = async_sessionmaker(
            getEngine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionFactory


async def getDb() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：提供数据库会话，请求结束自动关闭。"""
    factory = getSessionFactory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def disposeEngine() -> None:
    """应用关闭时释放引擎连接池。"""
    global _engine, _sessionFactory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionFactory = None
