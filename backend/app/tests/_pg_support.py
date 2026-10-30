"""真实 PostgreSQL 测试支撑（强制规则：Harness/rules/测试规范.md）。

- 独立测试库（qa_metadata_test），表结构由 Alembic `upgrade head` 产生（与生产同路径），
  禁止 Base.metadata.create_all。
- 完整 API 链路：pgApiClient 从 HTTP 入口发起，经路由/中间件/service/repository/真实 PG，
  数据准备与断言都落在真实行上。
- 每个测试 TRUNCATE 清库（RESTART IDENTITY CASCADE），互不依赖执行顺序。

运行要求：必须提供 TEST_DATABASE_URL（或 DATABASE_URL 指向真实 PostgreSQL），
未配置时 fail-fast（不允许静默降级 sqlite）。示例：
    TEST_DATABASE_URL=postgresql+asyncpg://qa_user:pass@localhost:5432/qa_metadata_test \\
        uv run pytest app/tests/integration/test_ontology_api.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.infrastructure import database as dbModule

_BACKEND_ROOT = Path(__file__).resolve().parents[2]  # backend/
_TEST_DB = "qa_metadata_test"

_schemaReady = False


def resolveTestDatabaseUrl() -> str:
    """解析真实 PG 测试库 URL；未配置真实 PG 时 fail-fast。"""
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    dbUrl = os.environ.get("DATABASE_URL", "")
    if dbUrl.startswith("postgresql"):
        return re.sub(r"/[^/]*$", f"/{_TEST_DB}", dbUrl)
    raise RuntimeError(
        "真实 PG 测试需要 TEST_DATABASE_URL（或 DATABASE_URL 指向真实 PostgreSQL），"
        f"禁止 sqlite 内存库。示例：TEST_DATABASE_URL=postgresql+asyncpg://user:pw@localhost:5432/{_TEST_DB}"
    )


def _ensureSchema(url: str) -> None:
    """与生产同路径：对测试库执行 Alembic upgrade head（幂等，进程内只跑一次）。"""
    global _schemaReady
    if _schemaReady:
        return
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Alembic upgrade head 失败: {result.stderr[-2000:]}")
    _schemaReady = True


async def _newEngine() -> Any:
    """每个测试新建真实 PG 引擎。

    不能跨测试复用引擎：pytest-asyncio 每个测试独立事件循环，
    共享的 asyncpg 连接池绑定在首个测试的 loop 上，复用会报
    "Future attached to a different loop"。测试结束后 dispose 释放连接。
    """
    url = resolveTestDatabaseUrl()
    _ensureSchema(url)
    return create_async_engine(url, echo=False)


async def _truncateAll(engine: Any) -> None:
    """清空所有业务表（保留 alembic_version），保证测试隔离。"""
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        )
        tables = [row[0] for row in rows]
        if tables:
            await conn.execute(
                text(f'TRUNCATE TABLE {", ".join(tables)} RESTART IDENTITY CASCADE')
            )


async def seedHeaderOntologyClasses(session: AsyncSession) -> int:
    """补种 business_object header 所需的最小本体类集合（仅测试库）。

    0133 收紧 business_object.header_class_id NOT NULL 后，seedBusinessObjects
    需经 ontology_class 解析 header；测试库每测试 TRUNCATE 全表，ontology_class
    为空会导致 seed 行全被过滤。生产由 seed_ontology 全量维护，不走本函数。

    **必须带 seed_ontology.CLASSES 同款 source_table**：后续调 seed_ontology.seed()
    的测试（seedEngine 路径）经 _seedClasses 按 source_table 查重复用，缺了会
    撞 uq_ontology_class_name_version（class_name, version）唯一约束。
    CLASSES 没有的类（IncomingInspection）source_table 置 None，无碰撞面。
    """
    import seed_ontology

    from app.domain.models import OntologyClass
    from scripts.seed_business_objects import HEADER_CLASS_NAMES

    sourceTableByName = {
        c["class_name"]: c["source_table"] for c in seed_ontology.CLASSES
    }
    inserted = 0
    for name in HEADER_CLASS_NAMES:
        exists = (
            await session.execute(
                text("SELECT 1 FROM ontology_class WHERE class_name = :name"),
                {"name": name},
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        session.add(
            OntologyClass(
                class_name=name,
                source_table=sourceTableByName.get(name),
                created_by="seed-test-header",
            )
        )
        inserted += 1
    await session.commit()
    return inserted


async def pgApiClient() -> AsyncIterator[AsyncClient]:
    """完整 API 链路测试客户端：真实 PG + 每个测试清库。

    通过替换 dbModule 的全局会话工厂，使 getDb() 依赖走真实 PG（与 sqlite
    client fixture 同机制），结束后恢复原工厂。
    """
    engine = await _newEngine()
    try:
        await _truncateAll(engine)
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        oldFactory = dbModule._sessionFactory
        oldEngine = dbModule._engine
        dbModule._sessionFactory = factory
        dbModule._engine = engine
        try:
            from app.tests._testapp import buildTestApp

            transport = ASGITransport(app=buildTestApp(factory))
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac
        finally:
            dbModule._sessionFactory = oldFactory
            dbModule._engine = oldEngine
    finally:
        await engine.dispose()
