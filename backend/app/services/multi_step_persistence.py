"""多步 run/step 的落库与查询（spec §3/§4）。纯数据访问，无 LLM 调用。"""
from __future__ import annotations

import logging
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
from app.utils.json_safe import jsonSafe

logger = logging.getLogger(__name__)


async def createRun(
    session: AsyncSession,
    *,
    sessionId: str,
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
        # `data` 是裸 JSONB 列：SQL 结果里的 NUMERIC 列回来是 Decimal，不归一会在
        # 编译期抛 StatementError（整条 UPDATE 失败、该步什么都不落）。jsonSafe
        # 把 Decimal 归一成 float（数值口径不变），见 app/utils/json_safe.py。
        step.data = jsonSafe(data)
    if chartOption is not None:
        step.chart_option = jsonSafe(chartOption)
    if modelUsed is not None:
        step.model_used = modelUsed
    step.tokens_used = (step.tokens_used or 0) + tokens
    step.cost = Decimal(str(step.cost or 0)) + Decimal(str(cost))
    step.finished_at = _utcnow()
    await session.flush()


async def recordStepError(
    session: AsyncSession,
    step: MultiStepStep,
    *,
    message: str,
    kind: str,
    tokens: int = 0,
    cost: float = 0,
) -> None:
    """记录一次失败尝试。

    tokens/cost 是该次尝试已消耗的用量（默认 0），累加进本步、不覆盖
    （spec §6.2「每次重试 tokens_used / cost 累加」）。调用方拿不到用量时留空，
    不要为了凑数传假值。
    状态保持 running：本函数是 per-attempt 语义，步的终态（failed / skipped）
    由执行链路在判定终止时落库（spec §6.3）。
    """
    step.attempt_count = (step.attempt_count or 0) + 1
    step.last_error = message[:2000]
    step.last_error_kind = kind
    step.tokens_used = (step.tokens_used or 0) + tokens
    step.cost = Decimal(str(step.cost or 0)) + Decimal(str(cost))
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


async def adoptRunForResume(
    session: AsyncSession,
    *,
    runId: uuid.UUID,
    subQuestions: list[str],
) -> tuple[MultiStepRun | None, int]:
    """续跑：复用既有 run 并把它的步行对齐到**本次**计划。返回 (run, 起始步号)。

    调用方（Task 6 的接线）**绝不**再调 `createRun` —— 那会新建第二个 run，而
    被 `prepareResume` 重置过的原 run 会永远停在 running（僵尸）。

    起始步的唯一事实来源是 `run.current_step_idx`（Task 7 的 `prepareResume` 写入）。
    只有当**计划的形状与原 run 逐字一致**时才沿用它；形状变了（`model_override`
    换了模型、或重新规划出不同的子问题）时归零整跑，因为旧的「已完成」对应的
    是别的子问题，跳过它们就是跑错。

    形状对齐同时负责行数：变长补行、变短删尾（否则 `stepsByIdx[index]` 会 KeyError
    或留下对不上的孤儿行）。

    重置范围是 `>= start`：更早的已成功步**原样保留**（`sql` / `data` 都不动）——
    续跑省掉重跑正是靠它。`data` 按 spec §5.3 永不删除；`sql` / `sql_hash` 清掉是
    刻意的：既不复用旧 SQL，也不给将来「sql_hash 命中即复用」的遗留项留一个会
    误命中的陈旧哈希。
    """
    run = await loadRun(session, runId)
    if run is None:
        return None, 0
    steps = await loadSteps(session, runId)
    shapesMatch = [s.sub_question for s in steps] == list(subQuestions)
    rawStart = int(run.current_step_idx or 0)
    # 越界即视为「没有可续跑的进度」：写侧 `_closeRun` 落的是 len(plan.steps)（含汇总步）
    # 这个越界哨兵，读侧必须容忍，否则续跑会把整跑跳过还把它封成终态。
    # 与「形状不匹配」同处置 —— 两者都意味着 current_step_idx 不是本次可用的起点。
    if shapesMatch and rawStart < len(subQuestions):
        start = rawStart
    else:
        start = 0
        logger.info(
            "续跑起点归零：run=%s 形状%s、current_step_idx=%d、本次 %d 步",
            run.id, "一致" if shapesMatch else "变化", rawStart, len(subQuestions),
        )

    byIndex = {s.step_index: s for s in steps}
    for surplus in steps:
        if surplus.step_index >= len(subQuestions):
            await session.delete(surplus)
    for index, text in enumerate(subQuestions):
        step = byIndex.get(index)
        if step is None:
            session.add(MultiStepStep(
                id=uuid.uuid4(), run_id=runId, step_index=index,
                status=STEP_STATUS_PENDING, sub_question=text,
            ))
            continue
        step.sub_question = text
        if index >= start:
            step.status = STEP_STATUS_PENDING
            step.last_error = None
            step.last_error_kind = None
            step.attempt_count = 0
            step.finished_at = None
            step.data_compressed = None
            step.sql = None
            step.sql_hash = None
    run.total_steps = len(subQuestions)
    run.current_step_idx = start
    await session.flush()
    return run, start


def _sqlHash(sql: str) -> str:
    import hashlib

    return hashlib.sha256(sql.encode("utf-8")).hexdigest()
