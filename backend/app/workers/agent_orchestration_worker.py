"""通用 Agent 编排 Worker（Task 8）：原子 claim + 租约式轮询。

独立进程按下述节奏工作：每轮用**新会话**原子 claim 一条 QUEUED run，
claim 事务提交并关闭后才调用注入的 ``ClaimedRunRunner``（LLM/工具调用期间
绝不持有数据库长事务）；随后等待 ``agentOrchestrationPollSeconds`` 再下一轮。

- 依赖注入：``settings`` / ``sessionFactory`` / ``runner``，本模块**不在导入期**
  依赖后续的 Runner 实现（只有 ``main`` 内函数级导入）；
- feature flag 关闭：``run`` 立即返回并记 info，不轮询、不建会话；
- 优雅停机：SIGTERM/SIGINT -> ``requestStop``（仅主线程注册，测试里静默跳过）；
- 异常隔离：Runner 抛错记 run/worker 上下文后继续轮询；租约丢失属于控制流，
  只记 info 不记 error。

启动（Task 9 提供 Runner 实现后）：
    ENABLE_AGENT_ORCHESTRATION=true uv run python -m app.workers.agent_orchestration_worker
不得随 API 启动。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, getSettings
from app.services.agent_orchestration_store import AgentOrchestrationStore
from app.services.agent_orchestration_types import ClaimedRunRunner, LeaseLostError

logger = logging.getLogger(__name__)


def _defaultWorkerId() -> str:
    """进程级稳定标识（长度 <= 128，满足 lease_owner 列宽）。"""
    return f"{socket.gethostname()}:{os.getpid()}"[:128]


class AgentOrchestrationWorker:
    """轮询 QUEUED run：claim 一条、交给 Runner、等待下一轮。"""

    def __init__(
        self,
        settings: Settings,
        sessionFactory: async_sessionmaker[AsyncSession],
        runner: ClaimedRunRunner,
        *,
        workerId: str | None = None,
        store: AgentOrchestrationStore | None = None,
    ) -> None:
        self._settings = settings
        self._sessionFactory = sessionFactory
        self._runner = runner
        self._workerId = workerId or _defaultWorkerId()
        self._store = store or AgentOrchestrationStore()
        self._stopEvent = asyncio.Event()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def requestStop(self) -> None:
        """请求优雅停机（当前 run 处理完后退出轮询）。"""
        self._stopEvent.set()

    async def run(self) -> None:
        """主循环；feature flag 关闭时立即返回（不轮询）。"""
        if not self._settings.enableAgentOrchestration:
            logger.info(
                "agent orchestration 未启用（ENABLE_AGENT_ORCHESTRATION=false），worker 不轮询"
            )
            return

        registerSignalHandlers(self.requestStop)
        logger.info(
            "agent orchestration worker 启动（worker=%s poll=%.1fs lease=%ds）",
            self._workerId,
            self._settings.agentOrchestrationPollSeconds,
            self._settings.agentOrchestrationLeaseSeconds,
        )
        try:
            while not self._stopEvent.is_set():
                await self._pollOnce()
                if self._stopEvent.is_set():
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopEvent.wait(),
                        timeout=self._settings.agentOrchestrationPollSeconds,
                    )
        finally:
            logger.info("agent orchestration worker 已停止（worker=%s）", self._workerId)

    async def runOnce(self) -> bool:
        """claim 一个 run 并执行一次；没有可 claim 的 run 时返回 False。

        返回 True 表示「本轮 claim 成功并已交给 Runner」——即使 Runner 抛错，
        claim 本身已完成，调用方仍应立即进入下一轮。
        """
        now = datetime.now(UTC)
        leaseSeconds = self._settings.agentOrchestrationLeaseSeconds
        # 会话在进入 Runner 之前关闭：claim 事务已在 claimNext 内提交。
        async with self._sessionFactory() as session:
            run = await self._store.claimNext(session, self._workerId, leaseSeconds, now)
        if run is None:
            return False

        runId = run.id
        logger.info(
            "worker %s claim run %s（lease=%ds）", self._workerId, runId, leaseSeconds
        )
        try:
            await self._runner.runClaimed(runId, self._workerId)
        except LeaseLostError as error:
            # 控制流：租约丢失/已被取消，Runner 已停止推进，无需重试本 run。
            logger.info(
                "worker %s run %s 租约丢失（%s），停止推进",
                self._workerId,
                runId,
                error.reason,
            )
        except Exception:
            logger.exception(
                "worker %s run %s 执行异常，继续轮询", self._workerId, runId
            )
        return True

    async def _pollOnce(self) -> None:
        """单轮异常隔离：DB/claim 故障记日志后由主循环等待重试。"""
        try:
            await self.runOnce()
        except Exception:
            logger.exception("worker %s 轮询失败，下轮重试", self._workerId)


def registerSignalHandlers(requestStop: Any) -> None:
    """注册 SIGTERM/SIGINT -> 优雅停机（仅主线程可用；非主线程静默跳过）。"""

    def _handler(signum: int, frame: Any) -> None:
        logging.getLogger(__name__).info("收到信号 %s，准备停机", signum)
        requestStop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        # 非主线程（测试并发场景）无法注册信号：静默跳过。
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _handler)


async def main() -> None:
    """进程入口：ENABLE_AGENT_ORCHESTRATION=true uv run python -m app.workers.agent_orchestration_worker"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # 函数级导入：本模块导入时不得依赖 Task 9 的 Runner 实现。
    from app.infrastructure.database import getSessionFactory
    from app.services.agent_orchestration_runner import AgentOrchestrationRunner

    worker = AgentOrchestrationWorker(
        getSettings(),
        getSessionFactory(),
        AgentOrchestrationRunner(),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
