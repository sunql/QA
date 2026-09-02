"""审计日志服务（Phase 4.5 governance hardening，遗留 #68）。

通用 AuditService：
1. 写入 audit_log（record）
2. 查询 audit_log（listByEntity / listByActor / listAll / getById）

仅写入，不提供修改/删除 API（不可变是治理前提）。

用法（service 层）：
    audit = AuditService()
    await audit.record(session, entity_type="kpi_catalog", entity_id=kpi.id,
                      action="CREATE", actor=user.userId, actor_departments=...,
                      after_json=kpi.to_dict())

事务语义：`record()` 只 session.add，不 commit。调用方需 commit，
让审计写入与业务写入处于同一事务（要么都成功，要么都回滚）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, AsyncIterator

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AuditLog

logger = logging.getLogger(__name__)

_VALID_ACTIONS = frozenset({"CREATE", "UPDATE", "DELETE"})

# 查询默认上限（防止一次拉太多）
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


class AuditService:
    """写入 + 查询 audit_log 通用审计日志。"""

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def record(
        self,
        session: AsyncSession,
        entity_type: str,
        entity_id: int,
        action: str,
        actor: str,
        actor_departments: tuple[str, ...] | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        outbox_id: int | None = None,
    ) -> AuditLog:
        """记录一次变更到 audit_log。

        参数：
            entity_type：实体类型名（e.g. 'kpi_catalog'），与 ORM 表名一致
            entity_id：实体 PK
            action：CREATE / UPDATE / DELETE（其他值被 DB CHECK 约束拒绝）
            actor：用户标识（X-User-Id）
            actor_departments：用户所属部门（tuple of str），逗号拼接存储
            before：变更前快照（CREATE 为 None，UPDATE 必有，DELETE 必有）
            after：变更后快照（CREATE 必有，UPDATE 必有，DELETE 为 None）
            outbox_id：outbox 幂等键（feat-audit-outbox，仅 worker 消费路径填写；
                业务同事务直写路径保持 None）

        不会自己 commit — 与调用方事务绑定，避免「业务成功 + 审计失败」的不一致。
        """
        if action not in _VALID_ACTIONS:
            # 程序错误，尽早暴露；DB CHECK 约束也是兜底
            raise ValueError(
                f"audit action 非法：{action!r}；应为 {sorted(_VALID_ACTIONS)}"
            )
        if action == "CREATE" and after is None:
            raise ValueError("audit CREATE 必须提供 after")
        if action == "DELETE" and before is None:
            raise ValueError("audit DELETE 必须提供 before")
        if action == "UPDATE" and (before is None or after is None):
            raise ValueError("audit UPDATE 必须提供 before 与 after")

        depts_str = ",".join(actor_departments) if actor_departments else None
        row = AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor=actor,
            actor_departments=depts_str,
            before_json=before,
            after_json=after,
            outbox_id=outbox_id,
        )
        session.add(row)
        logger.debug(
            "审计写入：%s/%s %s by %s",
            entity_type,
            entity_id,
            action,
            actor,
        )
        return row

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def listByEntity(
        self,
        session: AsyncSession,
        entity_type: str,
        entity_id: int,
        *,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[AuditLog]:
        """按实体查询审计记录（entity_type + entity_id 精确匹配）。"""
        limit = min(limit, _MAX_LIMIT)
        stmt = (
            select(AuditLog)
            .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def listByActor(
        self,
        session: AsyncSession,
        actor: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[AuditLog]:
        """按用户 ID 查询审计记录。"""
        limit = min(limit, _MAX_LIMIT)
        stmt = (
            select(AuditLog)
            .where(AuditLog.actor == actor)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def listAll(
        self,
        session: AsyncSession,
        *,
        entity_type: str | None = None,
        action: str | None = None,
        actor: str | None = None,
        entity_id: str | None = None,
        actor_departments: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[list[AuditLog], int]:
        """全局审计记录查询（支持 entity_type/action/actor fuzzy/actor_departments/since/until）。"""
        limit = min(limit, _MAX_LIMIT)
        where = []
        if entity_type:
            where.append(AuditLog.entity_type == entity_type)
        if action:
            where.append(AuditLog.action == action)
        if actor:
            where.append(AuditLog.actor.ilike(f"%{actor}%"))
        if entity_id:
            where.append(cast(AuditLog.entity_id, String).ilike(f"%{entity_id}%"))
        if actor_departments:
            where.append(AuditLog.actor_departments.ilike(f"%{actor_departments}%"))
        if since:
            where.append(AuditLog.created_at >= since)
        if until:
            where.append(AuditLog.created_at < until)

        base_stmt = select(AuditLog).where(*where).order_by(AuditLog.created_at.desc())
        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total = (await session.execute(count_stmt)).scalar_one()

        stmt = base_stmt.limit(limit).offset(offset)
        rows = list((await session.execute(stmt)).scalars().all())
        return rows, total

    async def iterAll(
        self,
        session: AsyncSession,
        *,
        entity_type: str | None = None,
        action: str | None = None,
        actor: str | None = None,
        entity_id: str | None = None,
        actor_departments: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        max_rows: int = 100_000,
    ) -> AsyncIterator[AuditLog]:
        """流式 yield 审计记录（session.stream 避免一次加载）。超过 max_rows 截断。"""
        where = []
        if entity_type:
            where.append(AuditLog.entity_type == entity_type)
        if action:
            where.append(AuditLog.action == action)
        if actor:
            where.append(AuditLog.actor.ilike(f"%{actor}%"))
        if entity_id:
            where.append(cast(AuditLog.entity_id, String).ilike(f"%{entity_id}%"))
        if actor_departments:
            where.append(AuditLog.actor_departments.ilike(f"%{actor_departments}%"))
        if since:
            where.append(AuditLog.created_at >= since)
        if until:
            where.append(AuditLog.created_at < until)

        stmt = select(AuditLog).where(*where).order_by(AuditLog.created_at.desc()).limit(max_rows)
        result = await session.stream(stmt)
        emitted = 0
        async for row in result.scalars():
            yield row
            emitted += 1
            if emitted >= max_rows:
                logger.warning("iterAll emitted %d rows (max_rows=%d) — truncating", emitted, max_rows)
                break

    async def getById(self, session: AsyncSession, id: int) -> AuditLog | None:
        """按 ID 查单条审计记录。"""
        entity = await session.get(AuditLog, id)
        return entity