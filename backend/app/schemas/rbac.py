"""RBAC API DTO（feat-rbac-identity, 2026-09-07）。

CamelModel 自动产出 username / displayName / roleCodes / menuCodes 等驼峰键。
Create/Update 均不含 subject 引用字段之外的越权面：用户/角色/组织 CRUD 与
授权采用 set-replace 语义（PUT 全量替换），幂等且防重复。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.domain.schemas import CamelModel

_USERNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
# code 命名空间风格：首段小写开头，后续段允许 camelCase（与现有 menu seed
# 一致：`section.aiAgent` / `item.adminUsers`）。不接受大写开头、下划线/数字
# 开头、空段或连续点。同步至前端 pages/Admin{Roles,Organizations,Menus}Page.tsx。
_CODE_PATTERN = r"^[a-z][a-zA-Z0-9_]*(?:\.[a-z][a-zA-Z0-9_]*)*$"


class UserCreate(CamelModel):
    """新增用户。username 不可变（作为 X-User-Id 映射键）。"""

    username: str = Field(min_length=1, max_length=64, pattern=_USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    email: str | None = Field(default=None, max_length=255)
    enabled: bool = True


class UserUpdate(CamelModel):
    """编辑用户。PATCH 语义：仅提供的字段被更新；email 传空串视为清空。"""

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    email: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None


class UserRead(CamelModel):
    """用户视图（含角色 / 组织编码，供管理列表一屏可见）。"""

    id: int
    username: str
    display_name: str
    email: str | None = None
    enabled: bool
    role_ids: list[int] = Field(default_factory=list)
    role_codes: list[str] = Field(default_factory=list)
    organization_ids: list[int] = Field(default_factory=list)
    organization_codes: list[str] = Field(default_factory=list)
    created_time: datetime
    updated_time: datetime | None = None


class RoleCreate(CamelModel):
    """新增角色。code='admin' 为内置超管角色（服务端唯一创建点）。"""

    code: str = Field(min_length=1, max_length=64, pattern=_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)


class RoleUpdate(CamelModel):
    """编辑角色。code 不可变（X-User-Roles / 权限归属以 code 为键）。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)


class RoleRead(CamelModel):
    id: int
    code: str
    name: str
    description: str | None = None
    is_builtin: bool = False
    created_time: datetime
    updated_time: datetime | None = None


class OrganizationCreate(CamelModel):
    """新增组织。parent_id 用于树形层级（同 parent 下按 sort_order 排序）。"""

    code: str = Field(min_length=1, max_length=64, pattern=_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    parent_id: int | None = None
    description: str | None = Field(default=None, max_length=1000)
    sort_order: int = Field(default=0, ge=0)


class OrganizationUpdate(CamelModel):
    """编辑组织。code 不可变。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: int | None = None
    description: str | None = Field(default=None, max_length=1000)
    sort_order: int | None = Field(default=None, ge=0)


class OrganizationRead(CamelModel):
    id: int
    code: str
    name: str
    parent_id: int | None = None
    description: str | None = None
    sort_order: int = 0
    created_time: datetime
    updated_time: datetime | None = None


class OrganizationTreeNode(CamelModel):
    """树形节点（含 children 嵌套）。"""

    id: int
    code: str
    name: str
    sort_order: int = 0
    description: str | None = None
    children: list["OrganizationTreeNode"] = []


OrganizationTreeNode.model_rebuild()


class RoleIdsUpdate(CamelModel):
    """set-replace：用户持有的角色 ID 全量。"""

    role_ids: list[int] = Field(default_factory=list)


class OrganizationIdsUpdate(CamelModel):
    """set-replace：用户所属组织 ID 全量。"""

    organization_ids: list[int] = Field(default_factory=list)


class MenuCodesUpdate(CamelModel):
    """set-replace：主体（用户/角色/组织）直接授权的菜单 code 全量。"""

    menu_codes: list[str] = Field(default_factory=list)


class SubjectPermissionsRead(CamelModel):
    """角色 / 组织维度直接授权视图（需求 #4）。"""

    subject_id: int
    menu_codes: list[str] = Field(default_factory=list)


class GrantSource(CamelModel):
    """授权来源拆分中的一条（角色 / 组织）。"""

    subject_id: int
    code: str
    name: str
    menu_codes: list[str] = Field(default_factory=list)


class UserEffectivePermissionsRead(CamelModel):
    """用户有效权限视图（需求 #4）：三来源合集 + 逐来源拆分。

    is_superuser=True 时 menu_codes 为全部可见菜单（admin 旁路）。
    """

    user_id: int
    is_superuser: bool = False
    role_codes: list[str] = Field(default_factory=list)
    organization_codes: list[str] = Field(default_factory=list)
    menu_codes: list[str] = Field(default_factory=list)
    direct_grants: list[str] = Field(default_factory=list)
    role_grants: list[GrantSource] = Field(default_factory=list)
    organization_grants: list[GrantSource] = Field(default_factory=list)
