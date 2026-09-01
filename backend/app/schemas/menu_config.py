"""menu_config API DTO。

CamelModel 自动产出 labelKey / sortOrder / permissionCode 等驼峰键。
"""

from __future__ import annotations

from pydantic import Field

from app.domain.schemas import CamelModel


class MenuItemRead(CamelModel):
    """叶子项或一级类的可序列化视图。"""

    code: str
    label_key: str
    icon_code: str | None = None
    sort_order: int
    permission_code: str | None = None
    roles: list[str] = Field(default_factory=list)
    path: str | None = None


class MenuSectionRead(MenuItemRead):
    """一级类 + children 数组。"""

    children: list[MenuItemRead] = Field(default_factory=list)


class MenuConfigRead(CamelModel):
    """API 顶层 envelope。"""

    version: str
    sections: list[MenuSectionRead]