"""Alembic 环境配置（异步）。

数据库 URL 从 app.config.Settings 注入，支持 asyncpg。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import getSettings
from app.domain.models import Base

# 确保所有模型已注册到 Base.metadata
import app.domain.models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

targetMetadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本，不连接数据库。"""
    url = getSettings().databaseUrl
    context.configure(
        url=url,
        target_metadata=targetMetadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=targetMetadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """在线模式（异步）：连接数据库执行迁移。"""
    settings = getSettings()
    connectable = async_engine_from_config(
        {"sqlalchemy.url": settings.databaseUrl},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """根据 URL 协议选择同步或异步执行。"""
    settings = getSettings()
    url = settings.databaseUrl
    if url.startswith("sqlite"):
        # 同步 SQLite（便于本地快速测试）
        connectable = engine_from_config(
            {"sqlalchemy.url": url},
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        try:
            with connectable.connect() as connection:
                _do_run_migrations(connection)
        finally:
            connectable.dispose()
    else:
        asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
