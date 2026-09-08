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


class MenuConfigCreate(CamelModel):
    """动态新增菜单节点。path 为空 → section（一级类）；否则叶子项。"""

    code: str = Field(min_length=1, max_length=64)
    label_key: str = Field(min_length=1, max_length=128)
    icon_code: str | None = Field(default=None, max_length=64)
    sort_order: int = 0
    path: str | None = Field(default=None, max_length=256)
    parent_code: str | None = Field(default=None, max_length=64)
    permission_code: str | None = Field(default=None, max_length=64)
    visible: bool = True


class MenuConfigUpdate(CamelModel):
    """编辑菜单节点。PATCH 语义：仅显式提供字段生效；icon_code/path 传 null 清空。

    code 创建后不可变（权限授予以 code 为键）；parent_code 传 null 无效
    （叶子必须挂 section）。
    """

    label_key: str | None = Field(default=None, min_length=1, max_length=128)
    icon_code: str | None = Field(default=None, max_length=64)
    sort_order: int | None = Field(default=None)
    path: str | None = Field(default=None, max_length=256)
    parent_code: str | None = Field(default=None, max_length=64)
    permission_code: str | None = Field(default=None, max_length=64)
    visible: bool | None = None


class MenuRowRead(CamelModel):
    """管理页扁平行（含不可见项 / parent 信息），供菜单树管理与授权勾选。"""

    id: int
    code: str
    parent_id: int | None = None
    parent_code: str | None = None
    label_key: str
    icon_code: str | None = None
    sort_order: int
    path: str | None = None
    permission_code: str | None = None
    visible: bool
    has_children: bool = False


class MenuConfigTreeNode(CamelModel):
    """菜单嵌套树节点（feat-menu-tree）。

    与 MenuSectionRead 字段对齐，但额外携带 visible + parent_code 用于
    树形管理 UI 的拖拽改父级。section（path=None）作为根节点，children
    数组挂子 section 或叶子项。返回结构支持任意层数（section 嵌套），
    但 UI 默认折叠 + 当前 seed 仅 2 层。
    """

    code: str
    label_key: str
    icon_code: str | None = None
    sort_order: int
    path: str | None = None
    permission_code: str | None = None
    visible: bool
    parent_code: str | None = None
    children: list["MenuConfigTreeNode"] = Field(default_factory=list)


MenuConfigTreeNode.model_rebuild()
