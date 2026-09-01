"""GET /api/v1/menu-config 集成测试。

覆盖：
  - 无 header 走 stub auth 兜底仍返回 200 + 6 类（默认 admin）；
  - admin headers + seed 后顶层 envelope 字段 + section camelCase shape 正确。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure import database as dbModule
from app.models.menu_config import MenuConfig

pytestmark = pytest.mark.integration


ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


async def _clean(dbSession: AsyncSession) -> None:
    """每测试独立清理 menu_config 表，避免依赖全局 TRUNCATE 顺序。"""
    await dbSession.execute(delete(MenuConfig).where(MenuConfig.id.is_not(None)))
    await dbSession.commit()


async def _seedMenuConfig(dbSession: AsyncSession) -> int:
    """通过全局会话工厂跑幂等 seed。"""
    from scripts.seed_menu_config import seed_menu_config

    factory = dbModule.getSessionFactory()
    return await seed_menu_config(factory)


async def test_returns_200_with_default_stub_user_when_no_headers(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """stub auth 兜底：无 header 也返回 200（生产部署必须关闭 stub）。

    复盘：dependencies.py 的 getCurrentUser 在 AUTH_STUB_ENABLED=1 时
    不抛 401 而是回退到 CurrentUser(userId='anonymous', roles=('user','admin'))，
    所以这个端点对所有登录用户都开放（菜单渲染需要）。
    """
    await _clean(dbSession)
    await _seedMenuConfig(dbSession)

    resp = await client.get("/api/v1/menu-config")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == "2026-09-01"
    assert len(body["sections"]) == 6


async def test_authenticated_returns_six_sections_with_expected_shape(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """admin headers + seed：顶层 envelope + section camelCase 字段。"""
    await _clean(dbSession)
    await _seedMenuConfig(dbSession)

    resp = await client.get("/api/v1/menu-config", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == "2026-09-01"
    assert isinstance(body["sections"], list)
    assert len(body["sections"]) == 6

    first = body["sections"][0]
    # Pydantic CamelModel 产出的字段名（与 brief 一致）
    assert set(first.keys()) >= {
        "code",
        "labelKey",
        "iconCode",
        "sortOrder",
        "permissionCode",
        "roles",
        "children",
    }
    assert isinstance(first["children"], list)
    # AI Agent 类别有 3 个叶子项（chat / agentRuntime / agents）
    assert len(first["children"]) >= 1


async def test_section_children_have_path_and_camel_case_keys(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """叶子项：camelCase 字段齐全 + path 非空。"""
    await _clean(dbSession)
    await _seedMenuConfig(dbSession)

    resp = await client.get("/api/v1/menu-config", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    sections = resp.json()["sections"]

    # 找一个含叶子项的 section（AI Agent 必有 item.chat）
    children = next(s["children"] for s in sections if s["children"])
    assert len(children) >= 1

    leaf = children[0]
    assert set(leaf.keys()) >= {
        "code",
        "labelKey",
        "iconCode",
        "sortOrder",
        "permissionCode",
        "roles",
        "path",
    }
    assert isinstance(leaf["path"], str) and leaf["path"].startswith("/")


async def test_empty_db_returns_zero_sections_not_error(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """空库：返回 0 sections 而非 5xx（允许菜单系统尚未初始化）。"""
    await _clean(dbSession)

    resp = await client.get("/api/v1/menu-config", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == "2026-09-01"
    assert body["sections"] == []
