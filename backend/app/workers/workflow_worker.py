"""P5 工作流 Worker（独立进程轮询 PG 表）。

D-7：**独立进程**轮询 PostgreSQL，**不引入队列库**（仓内零 celery/arq/dramatiq
依赖 ⇒ 引入即新运维面）。形态照抄 ``agent_orchestration_worker.py``：

- 独立进程、**不得随 API 启动**；
- feature flag 关闭 ⇒ ``run()`` 直接 return（**不建会话、不轮询**）
  ⇒ 回滚 = 关开关 + 停 Worker（G6 判据）；
- SIGTERM / SIGINT ⇒ 优雅停机（当前这轮处理完就退出）；
- 异常隔离：单轮失败记日志后继续下一轮。

两条职责：

1. **执行待处理命令**：``SKIP LOCKED`` 原子 claim ⇒ 交给
   ``CommandExecutor``（claim → 执行 → 终态在**同一个事务**里
   ⇒ worker 中途死掉 ⇒ 事务回滚 ⇒ 命令回到 PENDING 被重领）。
2. **清扫超期审批**：超期未决的审批留在 PENDING ⇒ 实例永远卡在「等待审批」
   ⇒ 必须有清扫（按节流间隔跑，不是每轮都打 DB）。

⚠️ **写操作的唯一入口仍是 Command**：Worker 只负责「领取 + 驱动」，
真正的外部写发生在适配器里（G6 由源码级反向守卫钉住）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, getSettings
from app.services.workflow_approval_service import WorkflowApprovalService
from app.services.workflow_command_executor import CommandExecutor
from app.services.workflow_store import WorkflowStore

logger = logging.getLogger(__name__)

# 审批超时清扫的默认节流间隔（秒）：太短 ⇒ 每轮都打一次 DB；太长 ⇒ 超时响应慢。
DEFAULT_APPROVAL_SWEEP_SECONDS = 60.0


def _defaultWorkerId() -> str:
    """进程级稳定标识（长度 <= 128，满足 ``executed_by`` / ``lease_owner`` 列宽）。"""

    return f"{socket.gethostname()}:{os.getpid()}"[:128]


class WorkflowWorker:
    """轮询待处理命令 + 清扫超期审批。"""

    def __init__(
        self,
        settings: Settings,
        sessionFactory: async_sessionmaker[AsyncSession],
        executor: CommandExecutor,
        *,
        workerId: str | None = None,
        store: WorkflowStore | None = None,
        approval: WorkflowApprovalService | None = None,
        approvalSweepSeconds: float = DEFAULT_APPROVAL_SWEEP_SECONDS,
    ) -> None:
        self._settings = settings
        self._sessionFactory = sessionFactory
        self._executor = executor
        self._workerId = workerId or _defaultWorkerId()
        self._store = store or WorkflowStore()
        self._approval = approval or WorkflowApprovalService()
        self._approvalSweepSeconds = approvalSweepSeconds
        self._stopEvent = asyncio.Event()
        # 首次清扫必然执行（-inf ⇒ 距今无限久）⇒ 启动即扫一次，不等一个周期。
        self._lastSweepAt = float("-inf")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def requestStop(self) -> None:
        """请求优雅停机（当前这轮处理完后退出轮询）。"""

        self._stopEvent.set()

    async def run(self) -> None:
        """主循环；feature flag 关闭时**立即返回**（不建会话、不轮询）。"""

        if not self._settings.enableWorkflow:
            logger.info("工作流未启用（ENABLE_WORKFLOW=false），worker 不轮询")
            return

        registerSignalHandlers(self.requestStop)
        logger.info(
            "工作流 worker 启动（worker=%s poll=%.1fs maxAttempts=%s）",
            self._workerId,
            self._settings.workflowPollSeconds,
            self._settings.workflowMaxCommandAttempts,
        )
        try:
            while not self._stopEvent.is_set():
                await self._pollOnce()
                if self._stopEvent.is_set():
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopEvent.wait(),
                        timeout=self._settings.workflowPollSeconds,
                    )
        finally:
            logger.info("工作流 worker 已停止（worker=%s）", self._workerId)

    async def _pollOnce(self) -> None:
        """单轮异常隔离：DB/claim 故障记日志后由主循环等待重试。"""

        try:
            await self.sweepApprovals()
            await self.runOnce()
        except Exception:
            logger.exception("worker %s 轮询失败，下轮重试", self._workerId)

    # ------------------------------------------------------------------
    # 命令执行
    # ------------------------------------------------------------------

    async def runOnce(self) -> bool:
        """领取一条待处理命令并执行；没有可领取的命令时返回 ``False``。

        返回 ``True`` 表示「本轮领取成功并已交给执行器」——即使执行失败，
        领取本身已完成，调用方仍应立即进入下一轮。
        """

        async with self._sessionFactory() as session:
            command = await self._store.claimNextCommand(
                session,
                workerId=self._workerId,
                maxAttempts=self._settings.workflowMaxCommandAttempts,
            )
            if command is None:
                # 无候选 ⇒ 释放事务（别把空事务的快照挂到连接池里）。
                await session.rollback()
                return False

            commandId = command.id
            outcome = await self._executor.executeClaimed(session, command, workerId=self._workerId)
            logger.info(
                "worker %s 命令 %s 执行结果 %s",
                self._workerId,
                commandId,
                outcome.status,
            )
            return True

    # ------------------------------------------------------------------
    # 审批超时清扫
    # ------------------------------------------------------------------

    async def sweepApprovals(self, *, force: bool = False) -> int:
        """把超期未决的审批标记为 TIMEOUT，返回处理条数（节流）。

        节流：距上次清扫不足 ``approvalSweepSeconds`` ⇒ 直接返回 0
        （避免每轮都打一次 DB）。``force=True`` 忽略节流（测试用）。
        """

        now = time.monotonic()
        if not force and (now - self._lastSweepAt) < self._approvalSweepSeconds:
            return 0
        self._lastSweepAt = now

        async with self._sessionFactory() as session:
            swept = await self._approval.sweepTimedOutApprovals(session)
            await session.commit()
        if swept:
            logger.info("worker %s 清扫超期审批 %s 条", self._workerId, swept)
        return swept


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
    """进程入口：

    ``ENABLE_WORKFLOW=true uv run python -m app.workers.workflow_worker``

    不得随 API 启动。
    """

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # 函数级导入：本模块导入时不得依赖数据库引擎的初始化。
    from app.infrastructure.database import getSessionFactory
    from app.services.workflow_command_ports import (
        CommandHandlerRegistry,
        SimulatedCommandHandler,
    )

    registry = CommandHandlerRegistry()
    registry.register("po.write", SimulatedCommandHandler())

    settings = getSettings()
    worker = WorkflowWorker(
        settings,
        getSessionFactory(),
        CommandExecutor(
            registry,
            maxAttempts=settings.workflowMaxCommandAttempts,
        ),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
