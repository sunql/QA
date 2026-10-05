"""多步持久化的编排钩子（spec §3-§6）。

单独成模块的原因：chat_multistep.py 已达 827 行且被豁免到 1000 行，新逻辑不得再挤进去。
本 mixin 只做「调用仓储 + 决定要不要压缩/落库」，不含 NL2SQL 业务逻辑。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.multi_step_models import (
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIALLY_FAILED,
    RUN_STATUS_SUCCEEDED,
    STEP_STATUS_COMPRESSED as _STEP_STATUS_COMPRESSED,
    STEP_STATUS_FAILED as _STEP_STATUS_FAILED,
    STEP_STATUS_SKIPPED as _STEP_STATUS_SKIPPED,
    STEP_STATUS_SUCCEEDED as _STEP_STATUS_SUCCEEDED,
    MultiStepRun,
    MultiStepStep,
)
from app.services import multi_step_persistence as persistence
from app.services.multi_step_compressor import (
    COMPRESS_THRESHOLD,
    compressStepData,
    estimatePromptTokens,
    shouldCompress,
)
from app.services.multi_step_retry import classifyStepError

logger = logging.getLogger(__name__)


class MultiStepPersistMixin:
    """给 ChatService 提供 run/step 落库、压缩判定、失败分类的钩子。"""

    async def _isPersistEnabled(self, session: AsyncSession) -> bool:
        return bool(getSettings().multiStepPersistEnabled)

    async def _openRun(
        self,
        session: AsyncSession,
        *,
        sessionId: Any,
        question: str,
        modelId: int | None,
        subQuestions: list[str],
        datasourceId: int | None = None,
    ) -> MultiStepRun | None:
        if not await self._isPersistEnabled(session):
            return None
        run = await persistence.createRun(
            session,
            sessionId=sessionId,
            question=question,
            modelId=modelId,
            datasourceId=datasourceId,
            totalSteps=len(subQuestions),
        )
        await persistence.createSteps(session, runId=run.id, subQuestions=subQuestions)
        return run

    async def _persistStepSuccess(
        self,
        session: AsyncSession,
        step: MultiStepStep,
        *,
        status: str = _STEP_STATUS_SUCCEEDED,
        sql: str | None = None,
        data: list | None = None,
        chartOption: dict | None = None,
        modelUsed: str | None = None,
        tokens: int = 0,
        cost: float = 0,
    ) -> None:
        if step is None:
            return
        await persistence.finishStep(
            session, step, status=status, sql=sql, data=data,
            chartOption=chartOption, modelUsed=modelUsed, tokens=tokens, cost=cost,
        )

    async def _persistStepFailure(
        self,
        session: AsyncSession,
        step: MultiStepStep,
        exc: Exception,
        *,
        run: MultiStepRun | None = None,
        tokens: int = 0,
        cost: float = 0,
    ) -> str:
        kind = classifyStepError(exc)
        await persistence.recordStepError(
            session, step,
            message=f"{type(exc).__name__}: {exc}", kind=kind,
            tokens=tokens, cost=cost,
        )
        if run is not None:
            await persistence.updateRun(
                session, run, status=RUN_STATUS_FAILED, currentStepIdx=step.step_index
            )
        return kind

    async def _closeRun(
        self,
        session: AsyncSession,
        run: MultiStepRun | None,
        *,
        status: str,
        completedSteps: int,
        currentStepIdx: int,
        errorSummary: str | None = None,
    ) -> None:
        if run is None:
            return
        await persistence.updateRun(
            session, run,
            status=status,
            completedSteps=completedSteps,
            currentStepIdx=currentStepIdx,
            errorSummary=errorSummary,
            finished=True,
        )

    async def _maybeCompressPriorSteps(
        self,
        session: AsyncSession,
        run: MultiStepRun | None,
        steps: list[MultiStepStep],
        *,
        nextStepIdx: int,
        maxInputTokens: int,
        injectionText: str,
    ) -> bool:
        """构造第 nextStepIdx 步 prompt 前调用。超阈值则压缩已成功步的 data。"""
        if run is None:
            return False
        estimated = estimatePromptTokens(injectionText)
        if not shouldCompress(estimated, maxInputTokens):
            return False

        compressedCount = 0
        for step in steps:
            if step.step_index >= nextStepIdx:
                break
            if step.status not in (_STEP_STATUS_SUCCEEDED, _STEP_STATUS_COMPRESSED):
                continue
            if step.data_compressed is not None:
                continue
            if not step.data:
                continue
            compressed = compressStepData(list(step.data))
            await persistence.finishStep(
                session, step, status=_STEP_STATUS_COMPRESSED
            )
            step.data_compressed = compressed
            compressedCount += 1

        if compressedCount:
            await persistence.updateRun(
                session, run,
                compressedCount=(run.compressed_count or 0) + compressedCount,
            )
            logger.info("多步压缩：run=%s 压缩 %d 步（估算 %d > %.0f%% of %d）",
                        run.id, compressedCount, estimated, COMPRESS_THRESHOLD * 100, maxInputTokens)
        return compressedCount > 0


def runStatusFor(completed: int, total: int, anyFailed: bool, anySkipped: bool) -> str:
    """run 终态判定（spec §4.2）。"""
    if anyFailed and not anySkipped:
        return RUN_STATUS_FAILED
    if anySkipped:
        return RUN_STATUS_PARTIALLY_FAILED
    if completed >= total:
        return RUN_STATUS_SUCCEEDED
    return RUN_STATUS_FAILED
