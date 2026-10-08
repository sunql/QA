"""多步 run 保留期清理（spec §10.4）。成功 30 天，失败/部分失败 7 天，未终态不删。"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.multi_step_models import (
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIALLY_FAILED,
    RUN_STATUS_SUCCEEDED,
    MultiStepRun,
)

logger = logging.getLogger(__name__)

SUCCEEDED_RETENTION_DAYS = 30
FAILED_RETENTION_DAYS = 7

_TERMINAL = (RUN_STATUS_SUCCEEDED, RUN_STATUS_FAILED, RUN_STATUS_PARTIALLY_FAILED)


async def cleanupMultiStepRuns(
    session: AsyncSession,
    *,
    succeededRetentionDays: int = SUCCEEDED_RETENTION_DAYS,
    failedRetentionDays: int = FAILED_RETENTION_DAYS,
    now: datetime | None = None,
) -> int:
    reference = now or datetime.now(UTC)
    succeededBefore = reference - timedelta(days=succeededRetentionDays)
    failedBefore = reference - timedelta(days=failedRetentionDays)

    expiredIds = (
        await session.execute(
            select(MultiStepRun.id).where(
                MultiStepRun.status.in_(_TERMINAL),
                or_(
                    (MultiStepRun.status == RUN_STATUS_SUCCEEDED)
                    & (MultiStepRun.finished_at < succeededBefore),
                    MultiStepRun.status.in_((RUN_STATUS_FAILED, RUN_STATUS_PARTIALLY_FAILED))
                    & (MultiStepRun.finished_at < failedBefore),
                ),
            )
        )
    ).scalars().all()

    if not expiredIds:
        return 0
    await session.execute(delete(MultiStepRun).where(MultiStepRun.id.in_(expiredIds)))
    logger.info("多步 run 清理：删除 %d 条（截止 %s）", len(expiredIds), reference.isoformat())
    return len(expiredIds)
