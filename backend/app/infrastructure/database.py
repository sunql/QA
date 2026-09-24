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

# feat-chat-concurrency-params：连接池参数由 system_config 行 DB_POOL_SIZE /
# DB_MAX_OVERFLOW 运行时注入（main.py lifespan 启动期一次性读），未注入时使用
# Settings 默认值（env 可覆盖）。``init_db_pool_config`` 在 lifespan init 早期
# 调用一次；engine 在 ``getEngine()`` 懒加载时读取最新值，因此重启后变更生效。
_db_pool_config: dict[str, int] = {
    "pool_size": getSettings().dbPoolSize,
    "max_overflow": getSettings().dbMaxOverflow,
}


def init_db_pool_config(*, pool_size: int, max_overflow: int) -> None:
    """lifespan 启动期一次性写入 system_config 读到的连接池参数。

    仅修改 module-level dict，不重建 engine；engine 在下一次 ``getEngine()``
    调用时按当前 dict 值创建。已存在 engine 不会被 dispose（需 ``disposeEngine``
    显式释放）。
    """
    _db_pool_config["pool_size"] = pool_size
    _db_pool_config["max_overflow"] = max_overflow


def get_db_pool_config() -> dict[str, int]:
    """返回当前生效的连接池参数（test / admin 调试用）。"""
    return dict(_db_pool_config)


def getEngine() -> AsyncEngine:
    """返回元数据库的异步引擎单例（懒加载）。"""
    global _engine
    if _engine is None:
        settings = getSettings()
        _engine = create_async_engine(
            settings.databaseUrl,
            echo=not settings.isProduction,
            pool_pre_ping=True,
            pool_size=_db_pool_config["pool_size"],
            max_overflow=_db_pool_config["max_overflow"],
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
