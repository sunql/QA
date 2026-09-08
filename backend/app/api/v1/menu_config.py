"""menu-config API 路由。

面向调用方（前端侧边栏）：
  GET  ""                 返回当前调用方可见菜单（一级类 + 叶子项）

面向管理面（需求 #3，动态增删改菜单，admin only）：
  GET    /admin          全量扁平行（含不可见项 / has_children），供菜单管理与授权勾选
  POST   ""              新增节点（path 空 → section；否则叶子项挂 parent_code）
  PUT    /{code}         编辑节点（可迁父 / 改 visible / 调字段）
  DELETE /{code}         删除叶子节点（section 含子项 → 409；FK 级联清授权）

鉴权：写操作一律 getAdminOnlyActor。code 创建后不可变（权限授予以 code 为键）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getAdminOnlyActor, getCurrentUser
from app.infrastructure.database import getDb
from app.schemas.menu_config import (
    MenuConfigCreate,
    MenuConfigRead,
    MenuConfigTreeNode,
    MenuConfigUpdate,
    MenuRowRead,
)
from app.models.rbac import ADMIN_ROLE_CODE
from app.services.menu_config_service import MenuAdminService, MenuConfigService
from app.services.permission_service import PermissionService

router = APIRouter(tags=["menu-config"])


@router.get("", response_model=MenuConfigRead, status_code=status.HTTP_200_OK)
async def getMenuConfig(
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> MenuConfigRead:
    """返回当前调用方可见菜单的嵌套结构（feat-rbac-identity Phase D 按人过滤）。

    过滤语义（需求 #3「最终权限以合集为准」的执行落点）：
        - DB 身份命中 + 非 admin 角色：allowed_codes = 用户有效菜单合集
          （direct ∪ role ∪ org），由 list_sections 只保留 code ∈ 合集 的可见
          叶子；一级类仅在含 ≥1 个可见子项时展示。
        - admin 角色 / stub 回退用户（dbUserId=None）：不过滤，返回全部可见菜单
          （与 Phase D 前行为一致，保持 dev/test 打开即用）。

    返回顶层 envelope：`{version, sections: [...]}`，sections 中每个
    一级类含 `children` 数组（叶子项）。
    """
    svc = MenuConfigService(session)
    allowed: frozenset[str] | None = None
    if user.dbUserId is not None and ADMIN_ROLE_CODE not in (user.roles or ()):
        all_leaf = await svc.leaf_codes(only_visible=False)
        eff = await PermissionService().computeEffective(
            session, user.dbUserId, all_menu_codes=all_leaf
        )
        allowed = frozenset(eff.menu_codes)
    return await svc.list_sections(allowed_codes=allowed)


@router.get("/admin", response_model=list[MenuRowRead])
async def listMenuRows(
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> list[MenuRowRead]:
    """菜单管理页：全量扁平行（含不可见项、parent_code、has_children）。"""
    return await MenuConfigService(session).list_admin_rows()


@router.get("/tree", response_model=list[MenuConfigTreeNode])
async def listMenuTree(
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> list[MenuConfigTreeNode]:
    """菜单管理页：嵌套树形结构（feat-menu-tree），供树形 UI + 拖拽改父级。

    与 /admin 互为补充：/admin 给 Table 用（parent_code / has_children）；
    /tree 给 antd Tree 用（嵌套 children + parent_code 反向）。含不可见
    项；多根（所有 section）；按 (sort_order, id) 排序。
    """
    return await MenuConfigService(session).list_menu_tree()


@router.post("", response_model=MenuRowRead, status_code=status.HTTP_201_CREATED)
async def createMenu(
    payload: MenuConfigCreate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> MenuRowRead:
    """动态新增菜单节点（admin only）。code 冲突 → 409。"""
    row = await MenuAdminService(session).create(payload, _admin)
    await session.commit()
    return await _row_read_by_code(session, row.code)


@router.put("/{code}", response_model=MenuRowRead)
async def updateMenu(
    code: str,
    payload: MenuConfigUpdate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> MenuRowRead:
    """编辑菜单节点（admin only）。section 移动 / 叶子置顶 / self-parent → 422。"""
    row = await MenuAdminService(session).update(code, payload, _admin)
    await session.commit()
    return await _row_read_by_code(session, row.code)


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteMenu(
    code: str,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> None:
    """删除菜单节点（admin only）。section 含子项 → 409。"""
    await MenuAdminService(session).delete(code, _admin)
    await session.commit()


async def _row_read_by_code(session: AsyncSession, code: str) -> MenuRowRead:
    """从管理扁平行里取指定 code 的最新行（含真实 parent_code / has_children）。"""
    for row in await MenuConfigService(session).list_admin_rows():
        if row.code == code:
            return row
    raise AssertionError(f"create/update 后未能回读菜单行: {code}")
