"""menu_config icon_code ↔ 前端 ICON_REGISTRY 对齐测试。

回归：`seed_menu_config.py` 写了 `icon_code="xxx"`，但前端
`frontend/src/components/common/menuIcons.tsx` 的 ICON_REGISTRY 没注册
→ 菜单那一项没有图标，UI 不报错。

契约：DB 端任意 menu_config.icon_code 必须落在 KNOWN_ICON_CODES 白名单里。
白名单与前端 ICON_REGISTRY 的 key 集合保持一致（手工同步——本测试失败
意味着需要同步两处）。
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

# 与 frontend/src/components/common/menuIcons.tsx ICON_REGISTRY keys 保持一致
# 加新 icon 时：①seed_menu_config.py 用 ②ICON_REGISTRY 注册 ③本白名单追加
KNOWN_ICON_CODES: frozenset[str] = frozenset({
    # 6 个 section
    "robot", "fund", "setting", "database", "api", "safety",
    # 叶子项
    "message", "thunderbolt", "appstore", "barchart", "alert",
    "partition", "audit", "node", "code", "number", "cluster",
    "tags", "tool", "file", "dashboard", "apartment", "heart",
})


async def _clean(dbSession: AsyncSession) -> None:
    await dbSession.execute(delete(MenuConfig).where(MenuConfig.id.is_not(None)))
    await dbSession.commit()


async def _seedMenuConfig(dbSession: AsyncSession) -> int:
    from scripts.seed_menu_config import seed_menu_config

    factory = dbModule.getSessionFactory()
    return await seed_menu_config(factory)


def _collect_icon_codes(sections: list[dict]) -> set[str]:
    """从 menu-config 响应的 sections 树里抽出所有 iconCode（含 null 过滤）。"""
    codes: set[str] = set()
    for sec in sections:
        if sec.get("iconCode"):
            codes.add(sec["iconCode"])
        for child in sec.get("children", []) or []:
            if child.get("iconCode"):
                codes.add(child["iconCode"])
    return codes


async def test_all_db_icon_codes_resolve_to_known_icons(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """回归：seed 后所有 menu_config.icon_code 必须在 KNOWN_ICON_CODES 里。

    用户踩过的 bug：icon_code="tags" 出现在 seed 但前端 ICON_REGISTRY 没注册
    → 菜单「本体属性管理」前面没有图标。
    """
    await _clean(dbSession)
    await _seedMenuConfig(dbSession)

    resp = await client.get("/api/v1/menu-config", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    used = _collect_icon_codes(body["sections"])
    assert used, "seed 后 menu_config 应至少有一个 icon_code，否则菜单全是文字"

    unregistered = used - KNOWN_ICON_CODES
    assert not unregistered, (
        f"以下 icon_code 在 DB 里用了但不在前端 ICON_REGISTRY 中:\n"
        f"  {sorted(unregistered)}\n"
        f"→ 在 frontend/src/components/common/menuIcons.tsx 加 wrap(SomeOutlined)\n"
        f"→ 同步追加到本测试的 KNOWN_ICON_CODES 白名单"
    )


async def test_known_icon_codes_actually_used_in_db(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """防 ICON_REGISTRY 死代码：白名单里的每个 icon_code 至少要被一处 menu_config 使用。

    不阻塞过渡期——输出 warn 让人工 review；强制断言会让「先加 ICON_REGISTRY、
    再用 seed 引用」的工作流被破坏。
    """
    await _clean(dbSession)
    await _seedMenuConfig(dbSession)

    resp = await client.get("/api/v1/menu-config", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()

    used = _collect_icon_codes(body["sections"])
    unused = KNOWN_ICON_CODES - used
    if unused:
        # 不 assert.fail()，留 warn 让 review 决定：
        # - 如果是新图标但还没 seed：忽略（等待后续 seed 引用）
        # - 如果是死代码：从 ICON_REGISTRY 和本白名单同时删除
        import warnings

        warnings.warn(
            f"以下 icon_code 在 KNOWN_ICON_CODES 白名单里但当前 seed 未引用:\n"
            f"  {sorted(unused)}\n"
            f"→ 若是新增图标，等待 seed_menu_config.py 后续引用；"
            f"若是死代码，从前端 ICON_REGISTRY + 本白名单同步移除",
            stacklevel=1,
        )