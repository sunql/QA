"""MenuConfig Pydantic schema 测试。"""

from app.schemas.menu_config import MenuConfigRead, MenuItemRead, MenuSectionRead


def test_menu_item_read_camel_alias() -> None:
    item = MenuItemRead(
        code="item.chat",
        label_key="menu.item.chat",
        icon_code="message",
        sort_order=110,
        path="/chat",
    )
    dumped = item.model_dump(by_alias=True)
    assert dumped["labelKey"] == "menu.item.chat"
    assert dumped["iconCode"] == "message"
    assert dumped["sortOrder"] == 110
    assert dumped["path"] == "/chat"
    assert dumped["permissionCode"] is None
    assert dumped["roles"] == []


def test_menu_section_read_nests_children() -> None:
    section = MenuSectionRead(
        code="section.aiAgent",
        label_key="menu.section.aiAgent",
        icon_code="robot",
        sort_order=100,
        children=[
            MenuItemRead(
                code="item.chat",
                label_key="menu.item.chat",
                icon_code="message",
                sort_order=110,
                path="/chat",
            )
        ],
    )
    assert len(section.children) == 1
    assert section.children[0].path == "/chat"


def test_menu_config_read_top_level_envelope() -> None:
    payload = MenuConfigRead(
        version="2026-09-01",
        sections=[
            MenuSectionRead(
                code="section.aiAgent",
                label_key="menu.section.aiAgent",
                icon_code="robot",
                sort_order=100,
            )
        ],
    )
    dumped = payload.model_dump(by_alias=True)
    assert dumped["version"] == "2026-09-01"
    assert len(dumped["sections"]) == 1