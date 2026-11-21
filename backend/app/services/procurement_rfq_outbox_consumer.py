"""M5-4 RFQ outbox 消费者（DoD 6：至少一次投递 + 按**事件主键**幂等）。

P5 `workflow_outbox` 此前**无消费者**（Worker 只轮询 `business_command`）。
本消费者处理 `event_type = 'rfq.%'` 的事件：

- **claim**：`FOR UPDATE SKIP LOCKED` + lease（lease_owner/lease_expires_at，
  列已在 0150 建好）⇒ 多 worker 不重复领取；
- **幂等**：`processed_at` 非空 ⇒ 跳过（同一事件重复投递无二次效果）；
- **DRAFT-only 语义**：当前 handler = 记录处理（无外部副作用）——M6 接
  ERP/SRM 端口时在此分派真实动作；
- **异常**：单事件失败 ⇒ attempts+1 + last_error 留痕，不阻塞其余事件。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workflow_models import WorkflowOutbox

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 60
_BATCH_SIZE = 20


async def pollRfqOutboxOnce(
    session: AsyncSession, *, workerId: str, batchSize: int = _BATCH_SIZE
) -> int:
    """领取并处理 `rfq.%` outbox 事件，返回**本轮处理条数**（幂等：已处理跳过）。"""
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            text(
                "SELECT id FROM workflow_outbox "
                "WHERE event_type LIKE 'rfq.%' AND status IN ('PENDING','FAILED') "
                "AND processed_at IS NULL "
                "AND (lease_expires_at IS NULL OR lease_expires_at < :now) "
                "ORDER BY id LIMIT :limit "
                "FOR UPDATE SKIP LOCKED"
            ),
            {"now": now, "limit": batchSize},
        )
    ).scalars().all()
    if not rows:
        return 0
    processed = 0
    for eventId in rows:
        try:
            event = await session.get(WorkflowOutbox, eventId)
            if event is None or event.processed_at is not None:
                continue  # 已被并发 worker 处理 ⇒ 幂等跳过
            await _dispatch(event)
            event.status = "PROCESSED"
            event.processed_at = _now()
            await session.flush()
            processed += 1
        except Exception as exc:
            event = await session.get(WorkflowOutbox, eventId)
            if event is not None:
                event.attempts = (event.attempts or 0) + 1
                event.last_error = str(exc)[:500]
                event.status = "FAILED"
                await session.flush()
            logger.warning(
                "outbox 事件 %s 处理失败（attempts 递增，下轮重试）", eventId
            )
    await session.commit()
    return processed


def _now() -> datetime:
    return datetime.now(UTC)


async def _dispatch(event: WorkflowOutbox) -> None:
    """事件分派（DRAFT-only：无外部副作用；M6 在此接 ERP/SRM 端口）。"""
    logger.info(
        "rfq outbox 事件已处理 id=%s type=%s payload=%s",
        event.id, event.event_type, event.payload,
    )
