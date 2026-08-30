"""审计日志服务（Phase 4.5 governance hardening，遗留 #68）。

通用 AuditService：捕获任意实体（entity_type+entity_id）的 CREATE/UPDATE/DELETE 事件。
仅写入，不提供查询/修改 API（查询走原生 SQL 即可；不可变是治理前提）。

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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AuditLog

logger = logging.getLogger(__name__)

_VALID_ACTIONS = frozenset({"CREATE", "UPDATE", "DELETE"})


class AuditService:
    """写入 audit_log 通用审计日志。"""

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