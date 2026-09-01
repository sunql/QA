"""MenuConfigService.list_sections 集成测试（真实 PG）。"""

from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.menu_config import MenuConfig
from app.schemas.menu_config import MenuConfigRead, MenuSectionRead
from app.services.menu_config_service import MenuConfigService

pytestmark = pytest.mark.integration


async def _clean_menu_config(session: AsyncSession) -> None:
    await session.execute(delete(MenuConfig).where(MenuConfig.id.is_not(None)))
    await session.commit()


async def test_list_sections_returns_envelope(dbSession: AsyncSession) -> None:
    await _clean_menu_config(dbSession)
    session_row = MenuConfig(code="section.aiAgent", label_key="menu.section.aiAgent",
                             icon_code="robot", sort_order=100)
    leaf_row = MenuConfig(code="item.chat", parent_id=None, label_key="menu.item.chat",
                          icon_code="message", sort_order=110, path="/chat")
    dbSession.add_all([session_row, leaf_row])
    await dbSession.flush()
    leaf_row.parent_id = session_row.id
    await dbSession.commit()

    svc = MenuConfigService(dbSession)
    result = await svc.list_sections()

    assert isinstance(result, MenuConfigRead)
    assert result.version == "2026-09-01"
    assert len(result.sections) == 1
    section = result.sections[0]
    assert isinstance(section, MenuSectionRead)
    assert section.code == "section.aiAgent"
    assert section.path is None
    assert len(section.children) == 1
    assert section.children[0].code == "item.chat"
    assert section.children[0].path == "/chat"


async def test_list_sections_sorts_children_by_sort_order(dbSession: AsyncSession) -> None:
    await _clean_menu_config(dbSession)
    section = MenuConfig(code="section.x", label_key="k", sort_order=100)
    dbSession.add(section)
    await dbSession.flush()
    for code, order in [("item.b", 120), ("item.a", 110), ("item.c", 130)]:
        dbSession.add(MenuConfig(code=code, parent_id=section.id, label_key="k",
                                  sort_order=order, path=f"/{code}"))
    await dbSession.commit()

    svc = MenuConfigService(dbSession)
    result = await svc.list_sections()
    codes = [c.code for c in result.sections[0].children]
    assert codes == ["item.a", "item.b", "item.c"]


async def test_list_sections_skips_invisible(dbSession: AsyncSession) -> None:
    await _clean_menu_config(dbSession)
    dbSession.add(MenuConfig(code="section.visible", label_key="k", sort_order=100, visible=True))
    dbSession.add(MenuConfig(code="section.hidden", label_key="k", sort_order=200, visible=False))
    await dbSession.commit()

    svc = MenuConfigService(dbSession)
    result = await svc.list_sections()
    codes = [s.code for s in result.sections]
    assert codes == ["section.visible"]


async def test_list_sections_roles_split_csv(dbSession: AsyncSession) -> None:
    await _clean_menu_config(dbSession)
    dbSession.add(MenuConfig(code="section.r", label_key="k", sort_order=100,
                              roles="admin,editor, "))
    await dbSession.commit()

    svc = MenuConfigService(dbSession)
    result = await svc.list_sections()
    assert result.sections[0].roles == ["admin", "editor"]


async def test_list_sections_section_with_path_raises(dbSession: AsyncSession) -> None:
    from app.services.menu_config_service import MenuConfigStructureError

    await _clean_menu_config(dbSession)
    dbSession.add(MenuConfig(code="section.bad", label_key="k", sort_order=100, path="/bad"))
    await dbSession.commit()

    svc = MenuConfigService(dbSession)
    with pytest.raises(MenuConfigStructureError):
        await svc.list_sections()
