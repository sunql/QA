"""多步 run/step 的落库与查询（spec §3/§4）。纯数据访问，无 LLM 调用。"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.multi_step_models import (
    RUN_STATUS_RUNNING,
    STEP_STATUS_PENDING,
    STEP_STATUS_RUNNING,
    MultiStepRun,
    MultiStepStep,
    _utcnow,
)


async def createRun(
    session: AsyncSession,
    *,
    sessionId: uuid.UUID,
    question: str,
    modelId: int | None,
    datasourceId: int | None = None,
    totalSteps: int,
) -> MultiStepRun:
    if totalSteps < 0:
        raise ValueError(f"totalSteps must be >= 0, got {totalSteps}")
    run = MultiStepRun(
        id=uuid.uuid4(),
        session_id=sessionId,
        question=question,
        model_id=modelId,
        datasource_id=datasourceId,
        status=RUN_STATUS_RUNNING,
        total_steps=totalSteps,
    )
    session.add(run)
    await session.flush()
    return run


async def createSteps(
    session: AsyncSession, *, runId: uuid.UUID, subQuestions: list[str]
) -> list[MultiStepStep]:
    steps = [
        MultiStepStep(
            id=uuid.uuid4(),
            run_id=runId,
            step_index=index,
            status=STEP_STATUS_PENDING,
            sub_question=text,
        )
        for index, text in enumerate(subQuestions)
    ]
    session.add_all(steps)
    await session.flush()
    return steps


async def markStepRunning(session: AsyncSession, step: MultiStepStep) -> None:
    step.status = STEP_STATUS_RUNNING
    step.started_at = _utcnow()
    await session.flush()


async def finishStep(
    session: AsyncSession,
    step: MultiStepStep,
    *,
    status: str,
    sql: str | None = None,
    data: list | None = None,
    chartOption: dict | None = None,
    modelUsed: str | None = None,
    tokens: int = 0,
    cost: float = 0,
) -> None:
    step.status = status
    if sql is not None:
        step.sql = sql
        step.sql_hash = _sqlHash(sql)
    if data is not None:
        step.data = data
    if chartOption is not None:
        step.chart_option = chartOption
    if modelUsed is not None:
        step.model_used = modelUsed
    step.tokens_used = (step.tokens_used or 0) + tokens
    step.cost = Decimal(str(step.cost or 0)) + Decimal(str(cost))
    step.finished_at = _utcnow()
    await session.flush()


async def recordStepError(
    session: AsyncSession, step: MultiStepStep, *, message: str, kind: str
) -> None:
    step.attempt_count = (step.attempt_count or 0) + 1
    step.last_error = message[:2000]
    step.last_error_kind = kind
    step.status = STEP_STATUS_RUNNING
    await session.flush()


async def updateRun(
    session: AsyncSession,
    run: MultiStepRun,
    *,
    status: str | None = None,
    completedSteps: int | None = None,
    currentStepIdx: int | None = None,
    compressedCount: int | None = None,
    errorSummary: str | None = None,
    finished: bool = False,
) -> None:
    if status is not None:
        run.status = status
    if completedSteps is not None:
        run.completed_steps = completedSteps
    if currentStepIdx is not None:
        run.current_step_idx = currentStepIdx
    if compressedCount is not None:
        run.compressed_count = compressedCount
    if errorSummary is not None:
        run.error_summary = errorSummary[:2000]
    if finished:
        run.finished_at = _utcnow()
    await session.flush()


async def loadRun(session: AsyncSession, runId: uuid.UUID) -> MultiStepRun | None:
    return (
        await session.execute(select(MultiStepRun).where(MultiStepRun.id == runId))
    ).scalar_one_or_none()


async def loadSteps(session: AsyncSession, runId: uuid.UUID) -> list[MultiStepStep]:
    rows = (
        await session.execute(
            select(MultiStepStep)
            .where(MultiStepStep.run_id == runId)
            .order_by(MultiStepStep.step_index)
        )
    ).scalars().all()
    return list(rows)


async def resetStepsFrom(
    session: AsyncSession, *, runId: uuid.UUID, fromStepIndex: int
) -> int:
    """把 >= fromStepIndex 的步重置为 pending 并清空错误；返回受影响行数。"""
    steps = await loadSteps(session, runId)
    touched = 0
    for step in steps:
        if step.step_index < fromStepIndex:
            continue
        step.status = STEP_STATUS_PENDING
        step.last_error = None
        step.last_error_kind = None
        step.attempt_count = 0
        step.finished_at = None
        touched += 1
    await session.flush()
    return touched


async def appendIdempotencyKey(session: AsyncSession, run: MultiStepRun, key: str) -> None:
    existing = list(run.idempotency_keys or [])
    if key in existing:
        return
    run.idempotency_keys = existing + [key]
    await session.flush()


def _sqlHash(sql: str) -> str:
    import hashlib

    return hashlib.sha256(sql.encode("utf-8")).hexdigest()
