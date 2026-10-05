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
from app.services.multi_step_retry import ERROR_KIND_PERMANENT, classifyStepError
from app.utils.json_safe import jsonSafe

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
        # spec §6.1：分类与落库解耦。kill switch 关掉时 `_openRun` 返回 None，
        # 调用方没有 step 行可传（只能传 None），但**仍然需要 kind** 去决定要不要
        # 重试 —— 故先分类，只在落库处短路。
        #
        # 缺这个守卫（`_persistStepSuccess` 早有同名守卫）会让 recordStepError 在
        # `step.attempt_count`（multi_step_persistence.py:114）抛 AttributeError，
        # 把原始的步错误顶掉：kill switch 一关，失败路径反而崩在守卫自身。
        kind = classifyStepError(exc)
        if step is None:
            return kind
        await self._recordStepFailure(
            session, step,
            message=f"{type(exc).__name__}: {exc}", kind=kind,
            run=run, tokens=tokens, cost=cost,
        )
        return kind

    async def _persistStepSoftFailure(
        self,
        session: AsyncSession,
        step: MultiStepStep,
        *,
        message: str,
        run: MultiStepRun | None = None,
        tokens: int = 0,
        cost: float = 0,
    ) -> None:
        """软失败的记档入口：`_executeDataStep` **不抛异常**，失败被收敛成
        `sql=None` 的错误结果行 —— 但 spec §6.1 仍要求落 `last_error` /
        `last_error_kind` / `attempt_count`，故由调用方把错误文案交回。

        与 `_persistStepFailure`（异常入口）共用 `_recordStepFailure` 的记档体。

        分类固定 `permanent`：`_executeDataStep` 交给调用方的只有错误文案，异常
        本体在它内部就被折叠成字符串了，分类线索已丢。这也正是诚实的结论 ——
        真正值得自动重试的瞬态故障，`_executeDataStep` 内部的回灌重试已经重试过
        （`_runQueryWithRetry`）；能走到「软失败」说明重试没救回来，转人工是对的。
        """
        if step is None:
            return
        await self._recordStepFailure(
            session, step,
            message=message, kind=ERROR_KIND_PERMANENT,
            run=run, tokens=tokens, cost=cost,
        )

    async def _recordStepFailure(
        self,
        session: AsyncSession,
        step: MultiStepStep,
        *,
        message: str,
        kind: str,
        run: MultiStepRun | None,
        tokens: int,
        cost: float,
    ) -> None:
        """硬失败与软失败共用的记档体（per-attempt 记录 + run 指针前置）。

        状态保持 `running`（`recordStepError` 的 per-attempt 语义）；步的终态由
        调用方在判定终止时以 `finishStep` 落（spec §6.3）。
        """
        await persistence.recordStepError(
            session, step, message=message, kind=kind, tokens=tokens, cost=cost,
        )
        if run is not None:
            await persistence.updateRun(
                session, run, status=RUN_STATUS_FAILED, currentStepIdx=step.step_index
            )

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
            # `data_compressed` 同样是裸 JSONB 列。压缩器内部已归一过一次，这里在
            # 落库边界再兜一层：不依赖压缩器的内部纪律（否则它哪天漏归一一个值，
            # 炸的是落库语句，而不是压缩器自己）。
            step.data_compressed = jsonSafe(compressed)
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


def _maxInputTokens(pipelineContext: Any) -> int:
    """当前选中模型配置的输入上限（token）。

    取不到时返回 0 —— `shouldCompress` 对 `maxInputTokens <= 0` 直接返回 False，
    即「读不到上限就不压缩」，不会把 0 当成「预算耗尽」而误触发压缩。

    **只认 `selected` 那一份配置**：`_PipelineContext.selected` 是路由器选中的
    `LlmConfig`（`max_input_tokens` 是它的列）。早先按
    `getattr(cfg, "selected", False)` 遍历 `configs` 的写法取不到任何值
    （`LlmConfig` 没有 `selected` 列）⇒ 恒返回 0 ⇒ 压缩永不触发（静默死码）。
    """
    selected = getattr(pipelineContext, "selected", None)
    return int(getattr(selected, "max_input_tokens", 0) or 0)
