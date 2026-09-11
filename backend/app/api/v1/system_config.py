"""system_config 管理 API（feat-system-config-admin）。

路由挂载：/api/v1/admin/system-config

  GET  ""                全量列表（登录用户；admin-only 限制走 menu grant 隐式约束）
  GET  "/{key}"          按 key 查询（admin-only，404 if not found）
  PUT  "/{key}"          更新 value（admin-only；audit_log 同事务写入）

设计要点：
- 列表走 getCurrentUser（任何登录用户都能看 system_config），admin-only 限制由
  menu grant 在 GET /menu-config 过滤阶段做（普通用户根本看不到 /admin/system-config
  菜单项）。这样后续若要开放给"能改 value 但不能改 description"的角色也好扩展。
- 写操作走 getAdminOnlyActor（service 层不重复校验）。
- audit_log 写在与 UPDATE 同事务（service 层负责），caller commit。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getAdminOnlyActor, getCurrentUser, getDb
from app.domain.schemas import SystemConfigRead, SystemConfigUpdate
from app.services.system_config_service import SystemConfigService

router = APIRouter(tags=["system-config"])


def _svc() -> SystemConfigService:
    return SystemConfigService()


@router.get("", response_model=list[SystemConfigRead])
async def listSystemConfig(
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[SystemConfigRead]:
    """列出全部 system_config 行（按 key 升序）。"""
    rows = await _svc().listAll(session)
    return [SystemConfigRead.model_validate(r) for r in rows]


@router.get("/{key}", response_model=SystemConfigRead)
async def getSystemConfig(
    key: str,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> SystemConfigRead:
    """按 key 查询 system_config 行；不存在 → 404（NotFoundError）。"""
    row = await _svc().getByKey(session, key)
    return SystemConfigRead.model_validate(row)


@router.put("/{key}", response_model=SystemConfigRead)
async def updateSystemConfig(
    key: str,
    payload: SystemConfigUpdate,
    admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> SystemConfigRead:
    """更新 system_config[key].value（admin-only）。

    - key 不存在 → 404（NotFoundError）。
    - audit_log 由 service 层同事务写入（UPDATE + before/after 双端快照）。
    """
    row = await _svc().updateValue(
        session=session,
        key=key,
        value=payload.value,
        actor=admin,
    )
    await session.commit()
    await session.refresh(row)
    return SystemConfigRead.model_validate(row)