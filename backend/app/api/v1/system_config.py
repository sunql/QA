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
- feat-chat-concurrency-params：特定 key 在 UPDATE 成功后触发运行时副作用：
  * ``RATE_LIMIT_KEY_STRATEGY`` → 立即刷新 ``rate_limit._rate_limit_strategy_cache``，
    下一个请求即用新策略（无需重启）。
  * ``DB_POOL_SIZE`` / ``DB_MAX_OVERFLOW`` → 仅记 warning 日志告知需重启容器，
    不做实际生效（engine pool 在 startup 一次性定死）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getAdminOnlyActor, getCurrentUser, getDb
from app.domain.schemas import SystemConfigRead, SystemConfigUpdate
from app.services.system_config_service import SystemConfigService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system-config"])

_RESTART_REQUIRED_KEYS = frozenset({"DB_POOL_SIZE", "DB_MAX_OVERFLOW"})


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
    - 特定 key 写入成功后触发运行时副作用（见模块顶注释）。
    """
    row = await _svc().updateValue(
        session=session,
        key=key,
        value=payload.value,
        actor=admin,
    )
    await session.commit()
    await session.refresh(row)
    _applyRuntimeSideEffect(key, payload.value)
    return SystemConfigRead.model_validate(row)


def _applyRuntimeSideEffect(key: str, value: str | None) -> None:
    """特定 key 写入后触发运行时副作用；DB 池类仅写日志。

    单调失败不应阻断 PUT 响应：副作用失败仅 logger.warning，不抛错。
    DB 已经在 service.updateValue 中提交；前端拿到 200 + 最新 value 后由其
    自行提示「需重启」。
    """
    if key == "RATE_LIMIT_KEY_STRATEGY":
        try:
            from app.infrastructure.rate_limit import set_rate_limit_strategy

            set_rate_limit_strategy(value or "ip")
        except Exception:
            logger.exception("RATE_LIMIT_KEY_STRATEGY 缓存刷新失败, 下次请求仍走 TTL 过期")
        return
    if key == "LLM_CONCURRENCY_LIMIT":
        try:
            from app.infrastructure.llm.factory import reload_llm_concurrency_limit

            limit = int(value or "20")
            if limit < 1:
                raise ValueError("must be positive")
            reload_llm_concurrency_limit(limit)
        except (TypeError, ValueError):
            logger.warning(
                "LLM_CONCURRENCY_LIMIT=%r 非法（须为正整数），已忽略；保持原 limit",
                value,
            )
        except Exception:
            logger.exception("LLM_CONCURRENCY_LIMIT 刷新失败；保持原 limit")
        return
    if key in _RESTART_REQUIRED_KEYS:
        logger.warning(
            "system_config[%s] 已更新为 %r；engine pool 在 startup 一次性定死，"
            "需重启容器 (qa-backend) 才生效",
            key,
            value,
        )