"""多步续跑（spec §7）。校验 → 重置后续步 → 复用流式执行。"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.multi_step_models import (
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIALLY_FAILED,
    RUN_STATUS_RUNNING,
    STEP_STATUS_COMPRESSED,
    STEP_STATUS_FAILED,
    STEP_STATUS_SKIPPED,
    STEP_STATUS_SUCCEEDED,
    MultiStepRun,
)
from app.services import multi_step_persistence as persistence

logger = logging.getLogger(__name__)

RESUMABLE_STATUSES = frozenset({RUN_STATUS_FAILED, RUN_STATUS_PARTIALLY_FAILED})


class ResumeConflict(Exception):
    """并发续跑或状态不允许。"""


class ResumeNotAllowed(Exception):
    """本函数查不到该 run —— 只兜「路由查过之后、这里再查之前被删掉」的竞态。

    **不含归属校验**：归属由调用方 `backend/app/api/v1/chat.py` 的
    `assertSessionOwnership` 负责（缺失 404 / 非归属 403，见该路由）。
    """


async def prepareResume(
    session: AsyncSession,
    *,
    runId: uuid.UUID,
    fromStepIndex: int | None,
    idempotencyKey: str | None,
) -> tuple[MultiStepRun, int]:
    """校验并重置；返回 (run, 起始步号)。run 不存在时抛 ResumeNotAllowed（永不返回 None）。"""
    run = await persistence.loadRun(session, runId)
    if run is None:
        raise ResumeNotAllowed(f"run {runId} not found")

    if idempotencyKey and idempotencyKey in (run.idempotency_keys or []):
        raise ResumeConflict("duplicate idempotency key")
    if run.status not in RESUMABLE_STATUSES:
        raise ResumeConflict(f"run status {run.status} not resumable")

    steps = await persistence.loadSteps(session, runId)
    start = fromStepIndex
    if start is None:
        start = next(
            (
                s.step_index for s in steps
                if s.status in (STEP_STATUS_FAILED, STEP_STATUS_SKIPPED)
            ),
            0,
        )
    if start < 0 or start >= len(steps):
        raise ResumeConflict(f"from_step_index {start} out of range 0..{len(steps) - 1}")
    for step in steps:
        if step.step_index < start and step.status not in (
            STEP_STATUS_SUCCEEDED, STEP_STATUS_COMPRESSED,
        ):
            raise ResumeConflict(f"step {step.step_index} not completed; cannot resume from {start}")

    await persistence.resetStepsFrom(session, runId=runId, fromStepIndex=start)
    # spec §5.3：data_compressed 仅当 status=compressed 时有值。resetStepsFrom 只回退
    # 状态、不清 data_compressed，故这里显式清空被重置范围，否则会留下
    # 「status=pending 但 data_compressed 非空」的非法态，且压缩钩子见非空即跳过
    # （_maybeCompressPriorSteps）⇒ 该步此后永远无法再压缩。
    for step in steps:
        if step.step_index >= start:
            step.data_compressed = None
    if idempotencyKey:
        await persistence.appendIdempotencyKey(session, run, idempotencyKey)
    run.resume_count = (run.resume_count or 0) + 1
    run.version = (run.version or 0) + 1
    run.status = RUN_STATUS_RUNNING
    run.finished_at = None
    run.current_step_idx = start
    await session.commit()
    return run, start
