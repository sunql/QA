"""RBAC 路由共享授权助手（feat-rbac-identity）。

三个资源路由（users/roles/organizations）的 `PUT .../permissions` 端点共享
同一 set-replace 语义：校验目标主体存在（各自路由完成）→ 以当前所有叶子
菜单 code 为合法集校验入参 → 整组替换 PermissionService.replaceSubjectGrants。
未知菜单 code 抛 ValueError → 统一转 422 ValidationError（fail fast）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import GrantSubjectType
from app.domain.exceptions import ValidationError
from app.services.menu_config_service import MenuConfigService
from app.services.permission_service import PermissionService


async def replaceSubjectMenuGrants(
    session: AsyncSession,
    subject_type: GrantSubjectType,
    subject_id: int,
    menu_codes: list[str],
    *,
    actor: str,
) -> None:
    """set-replace：将某主体（用户/角色/组织）的直接菜单授权替换为 menu_codes。

    写审计由 replaceSubjectGrants 在调用方事务内入队（路由 commit）。
    """
    valid = await MenuConfigService(session).leaf_codes(only_visible=False)
    try:
        await PermissionService().replaceSubjectGrants(
            session,
            subject_type.value,
            subject_id,
            menu_codes,
            valid_menu_codes=valid,
            actor=actor,
        )
    except ValueError as e:
        raise ValidationError(str(e)) from e
