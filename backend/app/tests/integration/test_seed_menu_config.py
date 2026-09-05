"""seed_menu_config 幂等性 + 路由对齐测试。"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure import database as dbModule
from app.models.menu_config import MenuConfig
from app.services.menu_config_service import MenuConfigService
import scripts.seed_menu_config as seed_module
from scripts.seed_menu_config import seed_menu_config

pytestmark = pytest.mark.integration


async def _clean(pg_session: AsyncSession) -> None:
    await pg_session.execute(delete(MenuConfig).where(MenuConfig.id.is_not(None)))
    await pg_session.commit()


async def test_seed_inserts_six_sections_and_twenty_items(
    dbSession: AsyncSession, client: object
) -> None:
    await _clean(dbSession)

    factory = dbModule.getSessionFactory()
    count = await seed_menu_config(factory)
    assert count == 28

    svc = MenuConfigService(dbSession)
    result = await svc.list_sections()
    assert len(result.sections) == 6
    total_items = sum(len(s.children) for s in result.sections)
    assert total_items == 22


async def test_seed_is_idempotent(
    dbSession: AsyncSession, client: object
) -> None:
    await _clean(dbSession)

    factory = dbModule.getSessionFactory()
    await seed_menu_config(factory)
    await seed_menu_config(factory)

    rows = (await dbSession.execute(select(MenuConfig))).scalars().all()
    assert len(rows) == 28
    assert len({r.code for r in rows}) == 28


async def test_seed_paths_aligned_with_frontend_routes(
    dbSession: AsyncSession, client: object
) -> None:
    """验证种子中所有 item.path 都在前端路由集合内。"""
    await _clean(dbSession)
    factory = dbModule.getSessionFactory()
    await seed_menu_config(factory)

    # 前端路由清单（App.tsx 当前实际路径）
    frontend_routes = {
        "/chat", "/agents/run", "/agents",
        "/supplier-360", "/supplier-risk",
        "/ontology", "/data-quality", "/lineage", "/entity-mapping",
        "/kpi-catalog", "/features", "/business-objects",
        "/datasource", "/documents", "/usage", "/graph", "/vectors",
        "/models", "/embeddings", "/status",
        "/admin/audit", "/admin/feature-rules",
    }

    svc = MenuConfigService(dbSession)
    result = await svc.list_sections()
    item_paths = {c.path for s in result.sections for c in s.children if c.path}
    assert item_paths == frontend_routes, (
        f"Mismatch: missing={frontend_routes - item_paths}, "
        f"extra={item_paths - frontend_routes}"
    )


async def test_main_runs_end_to_end_and_disposes_engine(
    dbSession: AsyncSession, client: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() 走真实 PG 端到端，monkeypatch getSettings + create_async_engine。

    保证 main() body（建引擎 / dispose / print）被覆盖，并验证独立引擎在 finally
    中正确释放（独立引擎指向测试 PG，与全局工厂同 URL → 写入可见）。
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import Settings, getSettings

    await _clean(dbSession)

    # client fixture 必须已替换全局工厂指向真实测试库
    if dbModule._engine is None:
        pytest.skip("test PG engine not initialized by client fixture")

    # 直接从环境变量取测试 PG URL（URL 对象可能密码被 mask）
    test_url = os.environ.get("TEST_DATABASE_URL", "")
    assert test_url, "TEST_DATABASE_URL must be set"

    def _fake_settings() -> Settings:
        # 用测试 PG URL 覆盖默认 SQLite；其余字段从真实 settings 复制
        real = getSettings()
        return real.model_copy(update={"databaseUrl": test_url})

    monkeypatch.setattr("scripts.seed_menu_config.getSettings", _fake_settings)

    created_engines: list[Any] = []

    def _capturing_engine(url: Any, **kwargs: Any) -> Any:
        engine = create_async_engine(url, **kwargs)
        created_engines.append(engine)
        return engine

    monkeypatch.setattr(
        "scripts.seed_menu_config.create_async_engine", _capturing_engine,
    )

    await seed_module.main()

    assert len(created_engines) == 1
    # 验证独立引擎已被 dispose（finally 块执行）；再 dispose 一次必须幂等无异常
    await created_engines[0].dispose()
    # 验证种子落库（独立引擎与全局工厂指向同一 URL）
    rows = (await dbSession.execute(select(MenuConfig))).scalars().all()
    assert len(rows) == 28
    assert len({r.code for r in rows}) == 28
