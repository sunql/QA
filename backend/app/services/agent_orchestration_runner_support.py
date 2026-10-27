"""Runner 的协作组件：计量客户端跟踪、执行守卫适配、只读快照。

这些类型不参与状态机决策，只把「外部边界」适配成 Runner 需要的形状，
单独存放以便 `agent_orchestration_runner.py` 专注于状态机本身：

- ``MeteredStep``：为单个步骤签发计量客户端，并在步骤提交前 drain 后台
  补记（被放弃的流也要留下用量行）；
- ``ToolCallGuard``：prepare 专用执行守卫，跳过 Runner 已预占的 iteration
  预算，其余预算照常；
- ``AgentOrchestrationRunReader``：每次新会话读取快照，返回脱离会话的对象；
- ``RetryBudget``：Planner 纠正额度与基础设施重试额度。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.agent_orchestration_models import (
    AgentOrchestrationRun,
    AgentOrchestrationStep,
)
from app.domain.exceptions import LlmClientError, LLMUnavailableError
from app.domain.models import LlmConfig
from app.services.agent_orchestration_store import AgentOrchestrationStore
from app.services.agent_orchestration_types import PlannerProtocolError

INFRASTRUCTURE_RETRY_LIMIT = 2

INFRASTRUCTURE_EXCEPTIONS = (
    LlmClientError,
    LLMUnavailableError,
    TimeoutError,
    ConnectionError,
    OSError,
    SQLAlchemyError,
)

MSG_PLANNER_INVALID = "Planner 输出不符合编排协议"


class RunFinished(Exception):
    """控制流：本轮已把 run 推进到暂停/终态，正常退出驱动循环。"""


class RetryBudget:
    """Planner 纠正额度（基础设施重试额度由各阶段自己的有界循环持有）。"""

    def __init__(self, maxCorrections: int) -> None:
        self.maxCorrections = maxCorrections
        self.protocolFailures = 0
        self.correction: str | None = None

    def recordProtocolFailure(self, error: PlannerProtocolError) -> bool:
        """记一次协议错误；仍可纠正时更新纠正提示并返回 True。"""
        self.protocolFailures += 1
        if self.protocolFailures > self.maxCorrections:
            return False
        self.correction = str(error) or MSG_PLANNER_INVALID
        return True


class ToolCallGuard:
    """prepare 专用执行守卫：跳过 iteration，其余预算照常。

    Runner 在每轮入口按 iteration 预算放行并预占本轮（建 PLAN 时 +1），
    因此 prepare 时看到的 iteration 用尽是**本轮自身**造成的；若不再跳过，
    最后一个合法迭代永远无法执行工具。tool/cost/time 仍由存储层检查。
    """

    def __init__(self, store: AgentOrchestrationStore) -> None:
        self._store = store

    async def assertExecutionAllowed(
        self, session: AsyncSession, run: Any, leaseOwner: str, now: datetime
    ) -> None:
        await self._store.assertExecutionAllowed(
            session, run, leaseOwner, now, skipIterationBudget=True
        )


class MeteredStep:
    """为一个步骤签发计量客户端，并保证收尾前 drain 掉后台补记。

    工具/Planner 每次 `__call__` 拿到一个包装真实 client 的计量客户端。真实
    ``MeteredLlmFactory`` 不保留客户端，所以由本适配器记住它们：被消费者
    提前放弃的流会派后台任务补记用量，`drain` 在**步骤提交之前**把这些行
    落库，否则暂停/终态之后的花费会永久丢失。
    """

    def __init__(
        self,
        factoryClass: Any,
        sessionFactory: async_sessionmaker[AsyncSession],
        runId: int,
        stepId: int | None,
        callerType: str,
        config: Any,
    ) -> None:
        self._base = factoryClass(sessionFactory, runId, stepId, callerType, config)
        self._clients: list[Any] = []

    def __call__(self, ignored: Any = None) -> Any:
        client = self._base(ignored)
        if client is not None:
            self._clients.append(client)
        return client

    async def drain(self) -> None:
        pending, self._clients = self._clients, []
        for client in pending:
            flush = getattr(client, "flushPendingRecords", None)
            if flush is not None:
                await flush()


class AgentOrchestrationRunReader:
    """只读快照读取：每次都用新会话，返回脱离会话的对象。"""

    def __init__(self, sessionFactory: async_sessionmaker[AsyncSession]) -> None:
        self._sessionFactory = sessionFactory

    async def loadRun(self, runId: int) -> AgentOrchestrationRun | None:
        async with self._sessionFactory() as session:
            run = await session.get(AgentOrchestrationRun, runId)
            if run is not None:
                session.expunge(run)
            return run

    async def loadSteps(self, runId: int) -> list[AgentOrchestrationStep]:
        async with self._sessionFactory() as session:
            steps = list(
                (
                    await session.execute(
                        select(AgentOrchestrationStep)
                        .where(AgentOrchestrationStep.run_id == runId)
                        .order_by(AgentOrchestrationStep.step_no)
                    )
                )
                .scalars()
                .all()
            )
            for step in steps:
                session.expunge(step)
            return steps

    async def loadModelConfig(self, run: AgentOrchestrationRun) -> LlmConfig | None:
        configId = int(getattr(run, "model_config_id", 0) or 0)
        if configId <= 0:
            return None
        async with self._sessionFactory() as session:
            config = await session.get(LlmConfig, configId)
            if config is not None:
                session.expunge(config)
            return config


def isInfrastructureError(error: Exception) -> bool:
    return isinstance(error, INFRASTRUCTURE_EXCEPTIONS)


def utcnow() -> datetime:
    return datetime.now(UTC)
