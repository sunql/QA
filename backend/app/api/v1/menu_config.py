"""menu-config API 路由。

GET /api/v1/menu-config —— 返回当前所有可见菜单（一级类 + 叶子项）。

鉴权：stub auth 兜底（AUTH_STUB_ENABLED=1），任意已认证用户可访问，
用于前端侧边栏渲染。生产部署必须由 JWT/IdP 解析用户身份并关闭 stub
（参见 app/dependencies.py 的安全护栏段）。

本期不消费 permissionCode / roles 字段；接口字段透传以备后续接入。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser
from app.infrastructure.database import getDb
from app.schemas.menu_config import MenuConfigRead
from app.services.menu_config_service import MenuConfigService

router = APIRouter(tags=["menu-config"])


@router.get("", response_model=MenuConfigRead, status_code=status.HTTP_200_OK)
async def getMenuConfig(
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> MenuConfigRead:
    """返回当前所有可见菜单的嵌套结构。

    返回顶层 envelope：`{version, sections: [...]}`，sections 中每个
    一级类含 `children` 数组（叶子项）。路径与权限字段透传，由前端
    根据业务需要消费。
    """
    return await MenuConfigService(session).list_sections()