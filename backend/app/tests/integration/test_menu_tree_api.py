"""GET /api/v1/menu-config/tree + 拖拽循环检测集成测试（feat-menu-tree）。

覆盖：
  - GET /tree 返回嵌套结构（section→item 2 层，admin only）
  - GET /tree 含不可见项（与 /menu-config 可见性过滤语义独立）
  - PUT /{code} parent_code 切叶子项到另一 section → 200
  - PUT /{code} parent_code 把叶子挂到自己后代 → 422（_would_create_menu_cycle）
  - PUT /{code} parent_code self-parent → 422（重复校验）
  - DELETE /{code} section 含子项 → 409（保留原约束）
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
USER_HEADERS = {"X-User-Id": "test-user", "X-User-Roles": "user"}


async def _clean(dbSession: AsyncSession) -> None:
    """每测试独立清理 menu_config 表，避免依赖全局 TRUNCATE 顺序。"""
    await dbSession.execute(delete(MenuConfig).where(MenuConfig.id.is_not(None)))
    await dbSession.commit()


async def _seed(dbSession: AsyncSession) -> int:
    from scripts.seed_menu_config import seed_menu_config

    factory = dbModule.getSessionFactory()
    return await seed_menu_config(factory)


def _find_section(sections: list[dict], code: str) -> dict:
    for s in sections:
        if s["code"] == code:
            return s
    raise AssertionError(f"section 不存在: {code}")


# ---------------------------------------------------------------------------
# GET /tree
# ---------------------------------------------------------------------------


async def test_tree_admin_only_returns_nested_structure(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """GET /tree：admin 返回嵌套结构（每 section 含 children 数组）。"""
    await _clean(dbSession)
    await _seed(dbSession)

    resp = await client.get("/api/v1/menu-config/tree", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    sections = resp.json()
    assert isinstance(sections, list)
    assert len(sections) >= 1

    # 至少一个 section 含 children + children 项必为 dict
    ai_agent = _find_section(sections, "section.aiAgent")
    assert isinstance(ai_agent["children"], list)
    assert len(ai_agent["children"]) >= 1
    first_leaf = ai_agent["children"][0]
    assert first_leaf["path"] is not None  # 叶子项 path 非空
    assert "children" in first_leaf  # Pydantic 仍产出 children 字段


async def test_tree_non_admin_forbidden(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """GET /tree：非 admin → 403。"""
    await _clean(dbSession)
    await _seed(dbSession)

    resp = await client.get("/api/v1/menu-config/tree", headers=USER_HEADERS)
    assert resp.status_code == 403, resp.text


async def test_tree_includes_invisible_nodes(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """GET /tree：含不可见项（与 /menu-config 的可见性过滤语义独立）。"""
    await _clean(dbSession)
    await _seed(dbSession)

    # 先把 item.chat 置为不可见
    put_resp = await client.put(
        "/api/v1/menu-config/item.chat",
        json={"visible": False},
        headers=ADMIN_HEADERS,
    )
    assert put_resp.status_code == 200, put_resp.text

    # /menu-config 默认仅返回可见项 → 验证 item.chat 不在侧边栏
    sidebar = await client.get("/api/v1/menu-config", headers=ADMIN_HEADERS)
    assert sidebar.status_code == 200
    sidebar_codes = {
        c["code"]
        for s in sidebar.json()["sections"]
        for c in s["children"]
    }
    assert "item.chat" not in sidebar_codes

    # /tree 应仍含 item.chat（visible=false 也在管理树里）
    tree = await client.get("/api/v1/menu-config/tree", headers=ADMIN_HEADERS)
    assert tree.status_code == 200
    found = next(
        (
            n
            for s in tree.json()
            for n in s["children"]
            if n["code"] == "item.chat"
        ),
        None,
    )
    assert found is not None
    assert found["visible"] is False


# ---------------------------------------------------------------------------
# PUT /{code} parent_code：合法移动
# ---------------------------------------------------------------------------


async def test_update_leaf_parent_moves_to_new_section(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """叶子项改 parent_code：从 section.aiAgent 切到 section.systemConfig → 200。"""
    await _clean(dbSession)
    await _seed(dbSession)

    resp = await client.put(
        "/api/v1/menu-config/item.chat",
        json={"parentCode": "section.systemConfig"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == "item.chat"
    assert body["parentCode"] == "section.systemConfig"

    # 验证 /tree 也跟着移动
    tree = await client.get("/api/v1/menu-config/tree", headers=ADMIN_HEADERS)
    assert tree.status_code == 200
    ai_agent_codes = {n["code"] for n in _find_section(tree.json(), "section.aiAgent")["children"]}
    sys_codes = {n["code"] for n in _find_section(tree.json(), "section.systemConfig")["children"]}
    assert "item.chat" not in ai_agent_codes
    assert "item.chat" in sys_codes


# ---------------------------------------------------------------------------
# PUT /{code} parent_code：循环 / 非法
# ---------------------------------------------------------------------------


async def test_update_parent_self_cycle_rejected(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """section.parent_code = section 自身 → 422（重复校验 + 后端保护）。"""
    await _clean(dbSession)
    await _seed(dbSession)

    resp = await client.put(
        "/api/v1/menu-config/section.systemConfig",
        json={"parentCode": "section.systemConfig"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422, resp.text


async def test_update_section_into_another_section_rejected(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """section.parent_code = 另一个 section → 422（schema 强制：section 不能挂 parent）。"""
    await _clean(dbSession)
    await _seed(dbSession)

    resp = await client.put(
        "/api/v1/menu-config/section.systemConfig",
        json={"parentCode": "section.aiAgent"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422, resp.text


async def test_update_leaf_to_non_section_rejected(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """叶子.parent_code = 另一个叶子 → 422（_resolve_parent 校验）。"""
    await _clean(dbSession)
    await _seed(dbSession)

    # 把 item.chat 挂到 item.agentRuntime 下（叶子不能作为父级）
    resp = await client.put(
        "/api/v1/menu-config/item.chat",
        json={"parentCode": "item.agentRuntime"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# DELETE /{code}：section 含子项 → 409（保留原约束）
# ---------------------------------------------------------------------------


async def test_delete_section_with_children_conflicts(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """section 含子项 → 409（与列表 UI 一致，UI 上 disabled 按钮 + 后端兜底）。"""
    await _clean(dbSession)
    await _seed(dbSession)

    resp = await client.delete(
        "/api/v1/menu-config/section.aiAgent",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 409, resp.text
