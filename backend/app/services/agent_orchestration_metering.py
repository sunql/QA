"""Per-call metering, failure accounting and cost aggregation (Task 6).

Why this module exists
----------------------
Every LLM call made by the orchestration subsystem must leave a durable trace:
how many tokens were consumed, what it cost, and whether that cost figure can
be trusted. The legacy chat pipeline writes ``session_token_usage`` rows; the
orchestration subsystem has its own tables (``agent_orchestration_usage`` plus
the run/step counters) because a run is not a chat session.

Cost labelling (the part that must not drift)
---------------------------------------------
- A cost computed locally from the model's configured unit prices is
  ``ESTIMATED`` — never ``MEASURED``. ``MEASURED`` is reserved for an explicit
  provider-reported cost, which this phase does not have; the low-level
  recorder still accepts it so a future provider-cost input has a home.
- Unknown tokens ⇒ ``cost_source=UNKNOWN`` and the run's ``cost_complete``
  turns false. ``summarizeCostSources`` is the single place that decides the
  aggregate, so the run level and the usage level can never disagree.

Single fact source
------------------
The metered client rows are the only source of truth for the orchestration
aggregate. The legacy ``ToolResult.tokens_used`` / ``ToolResult.cost`` fields
are deliberately ignored here — adding them would double count.

Transaction discipline
----------------------
The recorder opens its own short session per call and never holds a
transaction open across an LLM call. Inside its transaction it locks the run
row (``SELECT ... FOR UPDATE``) *before* inserting the usage row, so two
concurrent recorders cannot overwrite the run's ``cost_source`` with a stale
partial aggregate: the aggregate is recomputed from *all* usage rows of that
run inside the lock. Locking first is not cosmetic — inserting first takes a
key-share lock on the referenced run row (foreign key check) and two
recorders that each hold it deadlock when they then ask for the update lock.

Failure semantics
-----------------
Nothing is swallowed. A provider failure is recorded (with the tokens the
exception carries, if any) and the original exception is re-raised; a
recorder failure is logged with run/step/caller context and re-raised, because
silently dropping the accounting failure would hide real spend.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.agent_orchestration_models import (
    AgentOrchestrationRun,
    AgentOrchestrationStep,
    AgentOrchestrationUsage,
)
from app.domain.exceptions import LLMUnavailableError
from app.infrastructure.llm.base_client import (
    BaseLlmClient,
    LlmMessage,
    LlmResponse,
    LlmResponseWithTools,
    StreamChunk,
)
from app.infrastructure.llm.factory import createClient
from app.services.agent_orchestration_types import (
    CallStatus,
    CostSource,
    MeteredCompletion,
)
from app.services.chat_usage import UsageMixin
from app.services.nl2sql_service import _readFloatConfig

logger = logging.getLogger(__name__)

MEASURED = CostSource.MEASURED.value
ESTIMATED = CostSource.ESTIMATED.value
UNKNOWN = CostSource.UNKNOWN.value
MIXED = CostSource.MIXED.value

SUCCEEDED = CallStatus.SUCCEEDED.value
OUTPUT_INVALID = CallStatus.OUTPUT_INVALID.value
FAILED = CallStatus.FAILED.value

CACHE_HIT_MULTIPLIER_KEY = "LLM_CACHE_HIT_MULTIPLIER"
DEFAULT_CACHE_HIT_MULTIPLIER = 0.0

MSG_LLM_UNAVAILABLE = "LLM 服务不可用"

_UNKNOWN_MODEL_NAME = "unknown"


# ---------------------------------------------------------------------------
# cost source summary
# ---------------------------------------------------------------------------


def summarizeCostSources(sources: Iterable[str]) -> tuple[str, bool]:
    """Aggregate usage-row cost sources into ``(source, complete)``.

    Truth table (the run level is the aggregate of that run's usage rows):

    ==========================  ================  ==========
    usage row sources           run cost_source   complete
    ==========================  ================  ==========
    (none)                      UNKNOWN           False
    all MEASURED                MEASURED          True
    all ESTIMATED               ESTIMATED         True
    MEASURED + ESTIMATED        MIXED             True
    anything with UNKNOWN       UNKNOWN           False
    ==========================  ================  ==========

    An unrecognized source is treated like UNKNOWN: an unclassifiable cost
    figure must never be presented as measured or complete.
    """
    values = {str(source) for source in sources if source is not None}
    if not values:
        return (UNKNOWN, False)
    if UNKNOWN in values:
        return (UNKNOWN, False)
    known = values - {UNKNOWN}
    if known == {MEASURED}:
        return (MEASURED, True)
    if known == {ESTIMATED}:
        return (ESTIMATED, True)
    if known == {MEASURED, ESTIMATED} or MIXED in known:
        return (MIXED, True)
    return (UNKNOWN, False)


# ---------------------------------------------------------------------------
# recorder
# ---------------------------------------------------------------------------


class OrchestrationUsageRecorder:
    """Writes per-call usage rows and folds them into run/step counters."""

    def __init__(
        self,
        sessionFactory: async_sessionmaker[AsyncSession],
        config: Any,
    ) -> None:
        self._sessionFactory = sessionFactory
        self._config = config

    @property
    def modelConfigId(self) -> int:
        return int(getattr(self._config, "id", 0) or 0)

    @property
    def modelName(self) -> str:
        name = getattr(self._config, "model_name", None)
        return str(name) if name else _UNKNOWN_MODEL_NAME

    async def calculateCost(
        self,
        promptTokens: int,
        completionTokens: int,
        cachedTokens: int | None = None,
    ) -> Decimal:
        """Price one call with the run's model config.

        The cache-hit multiplier is read per call through the existing
        ``_readFloatConfig`` reader so cached-token pricing has exactly the
        same semantics as the Chat pipeline. The result is a local estimate.
        """
        multiplier = await self._readCacheHitMultiplier()
        return UsageMixin.calculateCost(
            self._config,
            promptTokens,
            completionTokens,
            cachedTokens=cachedTokens,
            cacheHitMultiplier=multiplier,
        )

    async def _readCacheHitMultiplier(self) -> float:
        async with self._sessionFactory() as session:
            return await _readFloatConfig(
                session, CACHE_HIT_MULTIPLIER_KEY, DEFAULT_CACHE_HIT_MULTIPLIER
            )

    async def recordUsage(
        self,
        *,
        runId: int,
        stepId: int | None,
        callerType: str,
        modelConfigId: int,
        modelName: str,
        promptTokens: int,
        completionTokens: int,
        cachedTokens: int | None,
        cost: Decimal,
        costSource: str,
        callStatus: str,
    ) -> int:
        """Insert one usage row and fold it into the run/step counters.

        Transaction order (deliberate): **lock the run row first** → insert
        the usage row (flush for its id) → increment the run (and step)
        counters → recompute the run's cost source from *all* usage rows of
        that run → commit. The run lock is what keeps two concurrent
        recorders from overwriting ``cost_source`` with a stale partial
        aggregate.

        Why the lock comes first: inserting the usage row first takes a
        ``FOR KEY SHARE`` lock on the referenced run row (the foreign key
        check). Two recorders that each hold that key-share lock and then
        ask for ``FOR UPDATE`` on the same row deadlock — the key-share lock
        of one blocks the update the other is waiting for. Locking the run
        row first removes the cycle (verified against real PostgreSQL with
        four concurrent recorders).

        Returns the new usage row id. Raises (never swallows) on failure.
        """
        async with self._sessionFactory() as session:
            try:
                run = await self._lockRun(session, runId)
                usageId = await self._insertUsage(
                    session,
                    runId=runId,
                    stepId=stepId,
                    callerType=callerType,
                    modelConfigId=modelConfigId,
                    modelName=modelName,
                    promptTokens=promptTokens,
                    completionTokens=completionTokens,
                    cachedTokens=cachedTokens,
                    cost=cost,
                    costSource=costSource,
                    callStatus=callStatus,
                )
                await self._applyCounters(
                    session,
                    run=run,
                    stepId=stepId,
                    promptTokens=promptTokens,
                    completionTokens=completionTokens,
                    cost=cost,
                )
                await session.commit()
                return usageId
            except Exception:
                await session.rollback()
                logger.error(
                    "orchestration usage accounting failed: run=%s step=%s "
                    "caller=%s status=%s",
                    runId,
                    stepId,
                    callerType,
                    callStatus,
                    exc_info=True,
                )
                raise

    async def markOutputInvalid(self, usageId: int) -> None:
        """Flip a recorded call to ``OUTPUT_INVALID`` — call status only.

        Token counts and cost are left untouched: the tokens were really
        spent, only the output was unusable.
        """
        async with self._sessionFactory() as session:
            try:
                await session.execute(
                    update(AgentOrchestrationUsage)
                    .where(AgentOrchestrationUsage.id == usageId)
                    .values(call_status=OUTPUT_INVALID)
                )
                await session.commit()
            except Exception:
                await session.rollback()
                logger.error(
                    "orchestration usage %s could not be marked invalid",
                    usageId,
                    exc_info=True,
                )
                raise

    async def _insertUsage(
        self,
        session: AsyncSession,
        *,
        runId: int,
        stepId: int | None,
        callerType: str,
        modelConfigId: int,
        modelName: str,
        promptTokens: int,
        completionTokens: int,
        cachedTokens: int | None,
        cost: Decimal,
        costSource: str,
        callStatus: str,
    ) -> int:
        usage = AgentOrchestrationUsage(
            run_id=runId,
            step_id=stepId,
            caller_type=callerType,
            model_config_id=modelConfigId,
            model_name=modelName,
            prompt_tokens=promptTokens,
            completion_tokens=completionTokens,
            cached_tokens=cachedTokens,
            cost=cost,
            cost_source=costSource,
            call_status=callStatus,
        )
        session.add(usage)
        await session.flush()
        return int(usage.id)

    async def _lockRun(
        self, session: AsyncSession, runId: int
    ) -> AgentOrchestrationRun:
        """Lock the run row (``SELECT ... FOR UPDATE``) and return it."""
        run = (
            await session.execute(
                select(AgentOrchestrationRun)
                .where(AgentOrchestrationRun.id == runId)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(
                f"orchestration run {runId} not found for usage accounting"
            )
        return run

    async def _applyCounters(
        self,
        session: AsyncSession,
        *,
        run: AgentOrchestrationRun,
        stepId: int | None,
        promptTokens: int,
        completionTokens: int,
        cost: Decimal,
    ) -> None:
        if stepId is not None:
            await self._applyToStep(
                session,
                stepId=stepId,
                promptTokens=promptTokens,
                completionTokens=completionTokens,
                cost=cost,
            )
        run.prompt_tokens = int(run.prompt_tokens or 0) + promptTokens
        run.completion_tokens = int(run.completion_tokens or 0) + completionTokens
        run.total_tokens = int(run.total_tokens or 0) + promptTokens + completionTokens
        run.cost = Decimal(str(run.cost or 0)) + cost
        source, complete = await self._recomputeRunSource(session, run.id)
        run.cost_source = source
        run.cost_complete = complete

    async def _applyToStep(
        self,
        session: AsyncSession,
        *,
        stepId: int,
        promptTokens: int,
        completionTokens: int,
        cost: Decimal,
    ) -> None:
        await session.execute(
            update(AgentOrchestrationStep)
            .where(AgentOrchestrationStep.id == stepId)
            .values(
                prompt_tokens=AgentOrchestrationStep.prompt_tokens + promptTokens,
                completion_tokens=AgentOrchestrationStep.completion_tokens + completionTokens,
                cost=AgentOrchestrationStep.cost + cost,
            )
        )

    async def _recomputeRunSource(
        self, session: AsyncSession, runId: int
    ) -> tuple[str, bool]:
        rows = (
            await session.execute(
                select(AgentOrchestrationUsage.cost_source).where(
                    AgentOrchestrationUsage.run_id == runId
                )
            )
        ).scalars().all()
        return summarizeCostSources(rows)


# ---------------------------------------------------------------------------
# metered client
# ---------------------------------------------------------------------------


class MeteredLlmClient(BaseLlmClient):
    """A ``BaseLlmClient`` that records exactly one usage row per call.

    The wrapper never changes the model: it delegates to the wrapped client
    and only adds accounting. A provider failure is recorded (with the tokens
    the exception carries) and the original exception is re-raised — no
    fallback model is requested and no exception is swallowed.
    """

    def __init__(
        self,
        client: BaseLlmClient,
        recorder: OrchestrationUsageRecorder,
        *,
        runId: int,
        stepId: int | None,
        callerType: str,
    ) -> None:
        self._client = client
        self._recorder = recorder
        self._runId = runId
        self._stepId = stepId
        self._callerType = callerType

    # -- BaseLlmClient surface ------------------------------------------

    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> LlmResponse:
        response, _ = await self._complete(
            messages,
            model=model,
            temperature=temperature,
            maxTokens=maxTokens,
            **kwargs,
        )
        return response

    async def complete_with_tools(
        self,
        messages: list[LlmMessage],
        tools: list[dict] | None = None,
        tool_choice: str | dict = "auto",
    ) -> LlmResponseWithTools:
        try:
            result = await self._client.complete_with_tools(messages, tools, tool_choice)
        except Exception as exc:
            await self._recordFailure(exc)
            raise
        promptTokens, completionTokens, cachedTokens = _usageFromMapping(result.usage)
        if result.usage is None:
            # The provider answered but reported no usage: the call succeeded,
            # the cost is unknown.
            await self._recordUnknown(callStatus=SUCCEEDED)
        else:
            await self._recordSuccess(
                promptTokens, completionTokens, cachedTokens
            )
        return result

    async def completeStream(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        terminal: StreamChunk | None = None
        try:
            async for chunk in self._client.completeStream(
                messages,
                model=model,
                temperature=temperature,
                maxTokens=maxTokens,
                **kwargs,
            ):
                if getattr(chunk, "isDone", False):
                    terminal = chunk
                yield chunk
        except Exception as exc:
            await self._recordFailure(exc)
            raise
        if terminal is None:
            # The stream ended without a terminal usage block: the tokens are
            # unknown, the call itself did not fail.
            await self._recordUnknown(callStatus=SUCCEEDED)
            return
        await self._recordSuccess(
            int(terminal.promptTokens or 0),
            int(terminal.completionTokens or 0),
            getattr(terminal, "cachedTokens", None),
        )

    async def close(self) -> None:
        await self._client.close()

    # -- Planner protocol -------------------------------------------------

    async def completeForPlanning(
        self, messages: list[LlmMessage]
    ) -> MeteredCompletion:
        response, usageId = await self._complete(messages)
        return MeteredCompletion(response=response, usageId=usageId)

    async def markOutputInvalid(self, usageId: int) -> None:
        await self._recorder.markOutputInvalid(usageId)

    # -- internals --------------------------------------------------------

    async def _complete(
        self, messages: list[LlmMessage], **kwargs: Any
    ) -> tuple[LlmResponse, int]:
        try:
            response = await self._client.complete(messages, **kwargs)
        except Exception as exc:
            await self._recordFailure(exc)
            raise
        usageId = await self._recordSuccess(
            int(response.promptTokens or 0),
            int(response.completionTokens or 0),
            getattr(response, "cachedTokens", None),
        )
        return response, usageId

    async def _recordSuccess(
        self,
        promptTokens: int,
        completionTokens: int,
        cachedTokens: int | None,
        *,
        callStatus: str = SUCCEEDED,
    ) -> int:
        cost = await self._recorder.calculateCost(
            promptTokens, completionTokens, cachedTokens
        )
        return await self._recorder.recordUsage(
            runId=self._runId,
            stepId=self._stepId,
            callerType=self._callerType,
            modelConfigId=self._recorder.modelConfigId,
            modelName=self._recorder.modelName,
            promptTokens=promptTokens,
            completionTokens=completionTokens,
            cachedTokens=cachedTokens,
            cost=cost,
            costSource=ESTIMATED,
            callStatus=callStatus,
        )

    async def _recordFailure(self, exc: Exception) -> None:
        """Record a failed call: known tokens are priced, unknown ones are not."""
        tokens = getattr(exc, "tokens", None)
        if not isinstance(tokens, (tuple, list)) or len(tokens) < 2:
            await self._recordUnknown(callStatus=FAILED)
            return
        promptTokens, completionTokens = int(tokens[0] or 0), int(tokens[1] or 0)
        await self._recordSuccess(
            promptTokens, completionTokens, None, callStatus=FAILED
        )

    async def _recordUnknown(self, *, callStatus: str = FAILED) -> None:
        await self._recorder.recordUsage(
            runId=self._runId,
            stepId=self._stepId,
            callerType=self._callerType,
            modelConfigId=self._recorder.modelConfigId,
            modelName=self._recorder.modelName,
            promptTokens=0,
            completionTokens=0,
            cachedTokens=None,
            cost=Decimal("0"),
            costSource=UNKNOWN,
            callStatus=callStatus,
        )


def _usageFromMapping(usage: dict | None) -> tuple[int, int, int | None]:
    """Read ``(prompt, completion, cached)`` out of a provider usage mapping."""
    if not isinstance(usage, dict):
        return (0, 0, None)
    promptTokens = int(usage.get("prompt_tokens") or 0)
    completionTokens = int(usage.get("completion_tokens") or 0)
    cachedTokens = usage.get("cached_tokens")
    return (
        promptTokens,
        completionTokens,
        int(cachedTokens) if isinstance(cachedTokens, int) else None,
    )


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


class _UnavailableLlmClient(BaseLlmClient):
    """Stands in for a provider client that could not be created."""

    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> LlmResponse:
        raise LLMUnavailableError(MSG_LLM_UNAVAILABLE)

    def completeStream(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        raise LLMUnavailableError(MSG_LLM_UNAVAILABLE)

    async def complete_with_tools(
        self,
        messages: list[LlmMessage],
        tools: list[dict] | None = None,
        tool_choice: str | dict = "auto",
    ) -> LlmResponseWithTools:
        raise LLMUnavailableError(MSG_LLM_UNAVAILABLE)

    async def close(self) -> None:
        return None


class MeteredLlmFactory:
    """Callable that hands out metered clients for one run/step.

    The factory always wraps ``createClient(self._config)`` — the same config
    object every time — so no model rerouting can happen behind the
    orchestration's back. One factory is built per Planner/TOOL step so the
    usage rows attach to the right step without mutable shared step state.

    When the provider client cannot be created, the returned client raises
    ``LLMUnavailableError`` on use and the failed call is recorded as
    ``UNKNOWN``/``FAILED``: the raise happens at call time because that is the
    moment the attempt can be attributed to a call and written to the ledger.
    ``createMeteredClient`` is the fail-fast variant for callers that want the
    error (and the accounting row) before the first call.
    """

    def __init__(
        self,
        sessionFactory: async_sessionmaker[AsyncSession],
        runId: int,
        stepId: int | None,
        callerType: str,
        config: Any,
    ) -> None:
        self._sessionFactory = sessionFactory
        self._runId = runId
        self._stepId = stepId
        self._callerType = callerType
        self._config = config
        self._recorder = OrchestrationUsageRecorder(sessionFactory, config)

    def __call__(self, ignored: Any = None) -> MeteredLlmClient:
        """Return a metered client for the fixed config (argument ignored)."""
        client = createClient(self._config)
        if client is None:
            client = _UnavailableLlmClient()
        return self._wrap(client)

    async def createMeteredClient(self, ignored: Any = None) -> MeteredLlmClient:
        """Fail-fast variant: an unavailable provider is reported up front."""
        client = createClient(self._config)
        if client is None:
            await self._recorder.recordUsage(
                runId=self._runId,
                stepId=self._stepId,
                callerType=self._callerType,
                modelConfigId=self._recorder.modelConfigId,
                modelName=self._recorder.modelName,
                promptTokens=0,
                completionTokens=0,
                cachedTokens=None,
                cost=Decimal("0"),
                costSource=UNKNOWN,
                callStatus=FAILED,
            )
            raise LLMUnavailableError(MSG_LLM_UNAVAILABLE)
        return self._wrap(client)

    def _wrap(self, client: BaseLlmClient) -> MeteredLlmClient:
        return MeteredLlmClient(
            client,
            self._recorder,
            runId=self._runId,
            stepId=self._stepId,
            callerType=self._callerType,
        )
