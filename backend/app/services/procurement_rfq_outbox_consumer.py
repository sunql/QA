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

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workflow_models import WorkflowOutbox
from app.services.workflow_store import WorkflowStore

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 60
_BATCH_SIZE = 20


async def pollRfqOutboxOnce(
    session: AsyncSession, *, workerId: str, batchSize: int = _BATCH_SIZE
) -> int:
    """领取并处理 `rfq.%` outbox 事件，返回**本轮处理条数**（幂等：已处理跳过）。"""
    store = WorkflowStore()
    events = await store.claimOutboxEvents(
        session, eventTypePrefix="rfq.", limit=batchSize
    )
    if not events:
        return 0
    processed = 0
    for event in events:
        try:
            if event.processed_at is not None:
                continue  # 已被并发 worker 处理 ⇒ 幂等跳过
            await _dispatch(event)
            await store.markOutboxEventProcessed(session, event)
            processed += 1
        except Exception as exc:
            await store.markOutboxEventFailed(session, event, str(exc))
            logger.warning(
                "outbox 事件 %s 处理失败（attempts 递增，下轮重试）", event.id
            )
    await session.commit()
    return processed


async def _dispatch(event: WorkflowOutbox) -> None:
    """事件分派（DRAFT-only：无外部副作用；M6 在此接 ERP/SRM 端口）。"""
    logger.info(
        "rfq outbox 事件已处理 id=%s type=%s payload=%s",
        event.id, event.event_type, event.payload,
    )
