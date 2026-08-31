"""审计 Outbox 服务（feat-audit-outbox，Phase 4.5 扩展）。

业务 service 的唯一审计入口：在同一事务内写 audit_outbox 行（不直接写
audit_log / *_history），由独立 worker 进程消费。审计失败不回滚业务。

事务语义：enqueue 只 session.add，不 commit -- 与业务写入同一事务原子提交。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AuditOutbox

logger = logging.getLogger(__name__)

# event_type 形如 '<entity>_<created|updated|deleted>'：
# entity 部分对应 audit_log.entity_type（snake_case 单数/复数按业务表名），
# 动作部分与 AuditService.record 的 action 大写化对齐。
_EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,48}_(created|updated|deleted)$")

# 安全护栏：防止超大 payload 导致 worker OOM 或存储耗尽（MEDIUM 修复）
_MAX_PAYLOAD_BYTES = 1_000_000  # 1 MB，JSONB 列实际上限以内

# entity_type 最大长度（对应 DB VARCHAR(50)）
_MAX_ENTITY_TYPE_LEN = 50


class OutboxService:
    """审计 outbox 入队（写入侧）。消费侧见 app/workers/audit_worker.py。"""

    async def enqueue(
        self,
        session: AsyncSession,
        *,
        event_type: str,
        entity_type: str,
        entity_id: int | None,
        actor: str,
        payload: dict[str, Any],
        actor_departments: tuple[str, ...] | None = None,
    ) -> AuditOutbox:
        """入队一条审计事件（与业务同事务，不 commit）。

        参数：
            event_type：'<entity>_<created|updated|deleted>'（kpi_created 等）
            entity_type：实体类型名，与 audit_log.entity_type 一致
            entity_id：实体 PK；DELETE 事件业务行已删仍传 id（可为 None 兜底）
            actor：用户标识（CurrentUser.userId）
            payload：{"before": ..., "after": ...}，与 AuditService.record 同构
            actor_departments：用户部门 tuple（JSONB 存 list）

        校验失败抛 ValueError（fail fast，业务事务整体回滚 -- 这是调用方
        的程序错误，不是审计写入故障）。
        """
        if not _EVENT_TYPE_PATTERN.fullmatch(event_type):
            raise ValueError(
                f"event_type 非法：{event_type!r}；应为 '<entity>_<created|updated|deleted>'"
            )
        if not actor:
            raise ValueError("actor 必填（审计可追溯性底线）")
        if len(entity_type) > _MAX_ENTITY_TYPE_LEN:
            raise ValueError(
                f"entity_type 超出最大长度 {_MAX_ENTITY_TYPE_LEN}：{entity_type!r}"
            )
        import json
        payload_bytes = len(json.dumps(payload).encode())
        if payload_bytes > _MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"payload 超出最大体积 {_MAX_PAYLOAD_BYTES} 字节（实际 {payload_bytes}）"
            )

        row = AuditOutbox(
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor,
            # tuple 不能 JSON 序列化，统一转 list；JSONB 侧读回即 list
            actor_departments=list(actor_departments) if actor_departments else None,
            payload=payload,
        )
        session.add(row)
        logger.debug(
            "outbox 入队：%s %s/%s by %s",
            event_type,
            entity_type,
            entity_id,
            actor,
        )
        return row
