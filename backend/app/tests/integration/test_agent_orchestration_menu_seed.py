"""编排菜单显式种子脚本的幂等性 + 隔离性集成测试（Task 11）。

三条契约：
1. **默认不暴露**：跑完默认 ``seed_menu_config`` 后，编排菜单的两个 path
   必须在库里不存在（默认 seed 提前暴露 = 提前上线）。
2. **显式播种幂等**：显式脚本连续跑两次，库里各恰好一行，且挂在
   ``section.aiAgent`` 下。
3. **UI 优先**：已存在的行（哪怕被 UI 改过）不被 seed 覆盖。

⚠️ 假绿防线：每条用例都断言**行数**（不是「有就行」）；seed 脚本一行都没插
也必须红。
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import scripts.seed_agent_orchestration_menu as seed_module
from app.infrastructure import database as dbModule
from app.models.menu_config import MenuConfig
from scripts.seed_agent_orchestration_menu import seed_agent_orchestration_menu
from scripts.seed_menu_config import seed_menu_config

pytestmark = pytest.mark.integration

ORCHESTRATION_CODES = {"item.agentOrchestration", "item.agentTasks"}
ORCHESTRATION_PATHS = {"/agent-orchestrations", "/agent-tasks"}


async def _clean(pg_session: AsyncSession) -> None:
    await pg_session.execute(delete(MenuConfig).where(MenuConfig.id.is_not(None)))
    await pg_session.commit()


async def _rowsByCode(pg_session: AsyncSession) -> dict[str, list[MenuConfig]]:
    rows = (await pg_session.execute(select(MenuConfig))).scalars().all()
    grouped: dict[str, list[MenuConfig]] = {}
    for row in rows:
        grouped.setdefault(row.code, []).append(row)
    return grouped


async def test_default_menu_seed_does_not_expose_orchestration_items(
    dbSession: AsyncSession, client: object
) -> None:
    """默认菜单种子不得包含编排入口 —— 这是「不提前上线」的第一道闸门。"""
    await _clean(dbSession)
    factory = dbModule.getSessionFactory()
    await seed_menu_config(factory)

    grouped = await _rowsByCode(dbSession)
    assert grouped.keys().isdisjoint(ORCHESTRATION_CODES), (
        f"默认 seed 不该插入编排菜单：{sorted(ORCHESTRATION_CODES & grouped.keys())}"
    )
    paths = {row.path for rows in grouped.values() for row in rows if row.path}
    assert paths.isdisjoint(ORCHESTRATION_PATHS), (
        f"默认 seed 不该出现编排路径：{sorted(ORCHESTRATION_PATHS & paths)}"
    )


async def test_orchestration_seed_inserts_exactly_one_row_per_item(
    dbSession: AsyncSession, client: object
) -> None:
    await _clean(dbSession)
    factory = dbModule.getSessionFactory()
    await seed_menu_config(factory)

    inserted = await seed_agent_orchestration_menu(factory)
    assert inserted == 2, f"首次播种应插入 2 行，实际 {inserted}"

    grouped = await _rowsByCode(dbSession)
    for code in ORCHESTRATION_CODES:
        assert len(grouped.get(code, [])) == 1, (
            f"{code} 应恰好 1 行，实际 {len(grouped.get(code, []))}"
        )

    section = grouped["section.aiAgent"][0]
    orchestration = grouped["item.agentOrchestration"][0]
    tasks = grouped["item.agentTasks"][0]
    assert orchestration.parent_id == section.id
    assert tasks.parent_id == section.id

    assert orchestration.label_key == "menu.item.agentOrchestration"
    assert orchestration.icon_code == "appstore"
    assert orchestration.sort_order == 140
    assert orchestration.path == "/agent-orchestrations"
    assert tasks.label_key == "menu.item.agentTasks"
    assert tasks.icon_code == "thunderbolt"
    assert tasks.sort_order == 150
    assert tasks.path == "/agent-tasks"


async def test_orchestration_seed_is_idempotent(
    dbSession: AsyncSession, client: object
) -> None:
    """连跑两次：第二次插入 0 行，库里仍各 1 行。"""
    await _clean(dbSession)
    factory = dbModule.getSessionFactory()
    await seed_menu_config(factory)

    assert await seed_agent_orchestration_menu(factory) == 2
    assert await seed_agent_orchestration_menu(factory) == 0

    grouped = await _rowsByCode(dbSession)
    for code in ORCHESTRATION_CODES:
        assert len(grouped.get(code, [])) == 1


async def test_orchestration_seed_does_not_overwrite_ui_edited_row(
    dbSession: AsyncSession, client: object
) -> None:
    """UI 优先策略：已存在的行不被 seed 回滚（灰度期运维手改的路径要保住）。"""
    await _clean(dbSession)
    factory = dbModule.getSessionFactory()
    await seed_menu_config(factory)
    await seed_agent_orchestration_menu(factory)

    row = (
        await dbSession.execute(
            select(MenuConfig).where(MenuConfig.code == "item.agentOrchestration")
        )
    ).scalar_one()
    row.label_key = "menu.item.agentOrchestration.custom"
    row.path = "/agent-orchestrations-custom"
    row.icon_code = "star"
    row.sort_order = 9999
    row.visible = False
    await dbSession.commit()

    assert await seed_agent_orchestration_menu(factory) == 0

    refreshed = (
        await dbSession.execute(
            select(MenuConfig).where(MenuConfig.code == "item.agentOrchestration")
        )
    ).scalar_one()
    assert refreshed.label_key == "menu.item.agentOrchestration.custom"
    assert refreshed.path == "/agent-orchestrations-custom"
    assert refreshed.icon_code == "star"
    assert refreshed.sort_order == 9999
    assert refreshed.visible is False


async def test_orchestration_seed_fails_fast_without_parent_section(
    dbSession: AsyncSession, client: object
) -> None:
    """父菜单不存在时必须显式报错，且不插入任何行。

    静默跳过会把「菜单挂错层级/没挂上」变成线上才发现的问题。
    """
    await _clean(dbSession)
    factory = dbModule.getSessionFactory()

    with pytest.raises(RuntimeError, match="section.aiAgent"):
        await seed_agent_orchestration_menu(factory)

    grouped = await _rowsByCode(dbSession)
    assert grouped.keys().isdisjoint(ORCHESTRATION_CODES)


async def test_main_runs_end_to_end_and_disposes_engine(
    dbSession: AsyncSession, client: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() 走真实 PG 端到端：建引擎 → 播种 → dispose。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import Settings, getSettings

    await _clean(dbSession)
    factory = dbModule.getSessionFactory()
    await seed_menu_config(factory)

    test_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    assert test_url, "TEST_DATABASE_URL or DATABASE_URL must be set"

    def _fake_settings() -> Settings:
        real = getSettings()
        return real.model_copy(update={"databaseUrl": test_url})

    monkeypatch.setattr(
        "scripts.seed_agent_orchestration_menu.getSettings", _fake_settings
    )

    created_engines: list[Any] = []

    def _capturing_engine(url: Any, **kwargs: Any) -> Any:
        engine = create_async_engine(url, **kwargs)
        created_engines.append(engine)
        return engine

    monkeypatch.setattr(
        "scripts.seed_agent_orchestration_menu.create_async_engine", _capturing_engine
    )

    await seed_module.main()

    assert len(created_engines) == 1
    await created_engines[0].dispose()

    grouped = await _rowsByCode(dbSession)
    for code in ORCHESTRATION_CODES:
        assert len(grouped.get(code, [])) == 1
