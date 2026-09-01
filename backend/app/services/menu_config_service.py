"""菜单配置服务。

list_sections() 单查询 + 内存分组构造嵌套结构。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.menu_config import MenuConfig
from app.schemas.menu_config import (
    MenuConfigRead,
    MenuItemRead,
    MenuSectionRead,
)


class MenuConfigError(RuntimeError):
    """菜单配置结构错误基类。"""


class MenuConfigDuplicateError(MenuConfigError):
    """code 列出现重复。"""


class MenuConfigStructureError(MenuConfigError):
    """一级类填了 path 或叶子项缺 path。"""


class MenuConfigService:
    """菜单数据访问与组装。"""

    DEFAULT_VERSION = "2026-09-01"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_sections(self, *, version: str = DEFAULT_VERSION) -> MenuConfigRead:
        stmt = (
            select(MenuConfig)
            .where(MenuConfig.visible.is_(True))
            .order_by(MenuConfig.sort_order, MenuConfig.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()

        # Duplicate-code guard
        seen_codes: set[str] = set()
        for row in rows:
            if row.code in seen_codes:
                raise MenuConfigDuplicateError(f"menu_config duplicate code: {row.code}")
            seen_codes.add(row.code)

        # Group by parent_id
        section_rows: list[MenuConfig] = []
        children_by_parent: dict[int, list[MenuConfig]] = {}
        for row in rows:
            if row.parent_id is None:
                section_rows.append(row)
            else:
                children_by_parent.setdefault(row.parent_id, []).append(row)

        sections: list[MenuSectionRead] = []
        for srow in section_rows:
            if srow.path is not None:
                raise MenuConfigStructureError(
                    f"section {srow.code!r} must have path=None (got {srow.path!r})"
                )
            kids = children_by_parent.get(srow.id, [])
            sections.append(
                MenuSectionRead(
                    code=srow.code,
                    label_key=srow.label_key,
                    icon_code=srow.icon_code,
                    sort_order=srow.sort_order,
                    permission_code=srow.permission_code,
                    roles=_split_roles(srow.roles),
                    path=None,
                    children=[_to_item(child) for child in kids],
                )
            )
        return MenuConfigRead(version=version, sections=sections)


def _split_roles(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [r.strip() for r in raw.split(",") if r.strip()]


def _to_item(row: MenuConfig) -> MenuItemRead:
    return MenuItemRead(
        code=row.code,
        label_key=row.label_key,
        icon_code=row.icon_code,
        sort_order=row.sort_order,
        permission_code=row.permission_code,
        roles=_split_roles(row.roles),
        path=row.path,
    )
