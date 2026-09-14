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


async def test_seed_inserts_seven_sections_and_thirty_five_items(
    dbSession: AsyncSession, client: object
) -> None:
    await _clean(dbSession)

    factory = dbModule.getSessionFactory()
    count = await seed_menu_config(factory)
    # 7 sections + 36 items = 43 rows（feat-rbac-identity 追加 adminUsers/Roles/
    # Organizations/Menus + adminSystemConfig + bizConfig 追加 dataQualityGenerate/
    # ontologyProperties/businessObjects/adminFeatureRules + foundation 追加
    # localImport + feat-wiki-knowledge 追加一级类 enterpriseWiki 与其下 6 项
    # wikiPages/wikiImport/wikiConflicts/wikiSuggestions/wikiCoverage/wikiGraph）
    assert count == 43

    svc = MenuConfigService(dbSession)
    result = await svc.list_sections()
    assert len(result.sections) == 7
    total_items = sum(len(s.children) for s in result.sections)
    assert total_items == 36


async def test_seed_is_idempotent(
    dbSession: AsyncSession, client: object
) -> None:
    await _clean(dbSession)

    factory = dbModule.getSessionFactory()
    await seed_menu_config(factory)
    await seed_menu_config(factory)

    rows = (await dbSession.execute(select(MenuConfig))).scalars().all()
    assert len(rows) == 43
    assert len({r.code for r in rows}) == 43


async def test_seed_does_not_overwrite_ui_edited_parent_id(
    dbSession: AsyncSession, client: object
) -> None:
    """回归：菜单管理 UI 把叶子项 parent_id 改到别的 section 后，seed 重跑
    必须保留 UI 的修改，不强制回滚到静态列表默认 parent。

    场景：adminUsers 默认 parent=systemConfig；模拟 UI 把它改到 bizConfig；
    再跑 seed，断言 parent 仍是 bizConfig（不被回滚）。
    """
    await _clean(dbSession)
    factory = dbModule.getSessionFactory()
    await seed_menu_config(factory)

    # 查 adminUsers 当前 parent_id 与 bizConfig 的 id
    admin_users = (
        await dbSession.execute(
            select(MenuConfig).where(MenuConfig.code == "item.adminUsers")
        )
    ).scalar_one()
    biz_section = (
        await dbSession.execute(
            select(MenuConfig).where(MenuConfig.code == "section.bizConfig")
        )
    ).scalar_one()
    assert admin_users.parent_id != biz_section.id  # 初始：parent=systemConfig

    # 模拟 UI 编辑
    admin_users.parent_id = biz_section.id
    await dbSession.commit()

    # 重跑 seed
    await seed_menu_config(factory)

    # 断言 parent 仍是 bizSection.id（没被回滚）
    refreshed = (
        await dbSession.execute(
            select(MenuConfig).where(MenuConfig.code == "item.adminUsers")
        )
    ).scalar_one()
    assert refreshed.parent_id == biz_section.id, (
        "seed 不应覆盖 UI 编辑后的 parent_id；"
        f"expected={biz_section.id} got={refreshed.parent_id}"
    )


async def test_seed_does_not_overwrite_ui_edited_display_fields(
    dbSession: AsyncSession, client: object
) -> None:
    """回归：菜单管理 UI 修改 label_key / icon_code / path / visible /
    sort_order 后，seed 重跑必须保留 UI 的修改，不强制回滚到 seed 默认。

    设计变更：seed 仅 INSERT 默认值；冲突 DO NOTHING。AdminMenusPage 是这
    5 个字段的运行时真值。父级 parent_id 由 sibling 测试覆盖。
    """
    await _clean(dbSession)
    factory = dbModule.getSessionFactory()
    await seed_menu_config(factory)

    # 拿 item.documents 原始 seed 值
    original = (
        await dbSession.execute(
            select(MenuConfig).where(MenuConfig.code == "item.documents")
        )
    ).scalar_one()
    original_label = original.label_key
    original_icon = original.icon_code
    original_path = original.path
    original_sort = original.sort_order
    original_visible = original.visible

    # 模拟 UI 全部改了
    original.label_key = "menu.item.documents.custom"
    original.icon_code = "star"
    original.path = "/documents-custom"
    original.sort_order = 9999
    original.visible = False
    await dbSession.commit()

    # 重跑 seed
    await seed_menu_config(factory)

    refreshed = (
        await dbSession.execute(
            select(MenuConfig).where(MenuConfig.code == "item.documents")
        )
    ).scalar_one()
    assert refreshed.label_key == "menu.item.documents.custom", (
        f"seed 不应覆盖 UI 编辑的 label_key；got={refreshed.label_key}"
    )
    assert refreshed.icon_code == "star", (
        f"seed 不应覆盖 UI 编辑的 icon_code；got={refreshed.icon_code}"
    )
    assert refreshed.path == "/documents-custom", (
        f"seed 不应覆盖 UI 编辑的 path；got={refreshed.path}"
    )
    assert refreshed.sort_order == 9999, (
        f"seed 不应覆盖 UI 编辑的 sort_order；got={refreshed.sort_order}"
    )
    assert refreshed.visible is False, (
        f"seed 不应覆盖 UI 编辑的 visible；got={refreshed.visible}"
    )

    # 防御：seed 默认值确实存在（确保 sanity check 不被 silent pass）
    assert original_label != "menu.item.documents.custom"


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
        "/ontology", "/data-quality", "/data-quality/generate",
        "/lineage", "/entity-mapping",
        "/kpi-catalog", "/features", "/business-objects", "/ontology-properties",
        "/datasource", "/local-import", "/documents", "/usage", "/graph", "/vectors",
        "/models", "/embeddings", "/status",
        "/admin/audit", "/admin/feature-rules",
        # feat-rbac-identity：RBAC 管理 4 页（admin 路由统一 /admin/*）
        "/admin/users", "/admin/roles", "/admin/organizations", "/admin/menus",
        # feat-system-config-admin
        "/admin/system-config",
        # feat-wiki-knowledge：企业 Wiki 一级类下的 6 个二级项
        "/admin/wiki-pages", "/admin/wiki-import", "/admin/wiki-conflicts",
        "/admin/wiki-suggestions", "/admin/wiki-coverage", "/admin/wiki-graph",
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
    assert len(rows) == 43
    assert len({r.code for r in rows}) == 43
