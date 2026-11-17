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
from app.services.workflow_command_ports import CommandHandlerRegistry
from app.services.workflow_store import WorkflowStore

logger = logging.getLogger(__name__)

# 审批超时清扫的默认节流间隔（秒）：太短 ⇒ 每轮都打一次 DB；太长 ⇒ 超时响应慢。
DEFAULT_APPROVAL_SWEEP_SECONDS = 60.0

# 单轮「排空」的批量上限：一条命令一次外部写 ⇒ 一轮最多做这么多外部写，
# 剩下的下一轮再来（防止一条链式工作流把一轮拉得没边）。
DEFAULT_DRAIN_BATCH_SIZE = 50

# workerId 的长度上限。
# ⚠️ 取 **64**（``business_command.executed_by`` 的列宽），不是 128
#    （``workflow_outbox.lease_owner`` 的列宽）⇒ 取宽的那个会让超长主机名
#    **写不进** executed_by ⇒ claim 直接报错 ⇒ 命令永远执行不了。
WORKER_ID_MAX_LEN = 64


def _truncateWorkerId(value: str) -> str:
    """把 workerId 截到列宽内（``executed_by String(64)``）。"""

    return value[:WORKER_ID_MAX_LEN]


def _defaultWorkerId() -> str:
    """进程级稳定标识（长度 <= ``executed_by`` 列宽）。"""

    return _truncateWorkerId(f"{socket.gethostname()}:{os.getpid()}")


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
        drainBatchSize: int = DEFAULT_DRAIN_BATCH_SIZE,
    ) -> None:
        self._settings = settings
        self._sessionFactory = sessionFactory
        self._executor = executor
        self._workerId = _truncateWorkerId(workerId or _defaultWorkerId())
        self._store = store or WorkflowStore()
        self._approval = approval or WorkflowApprovalService()
        self._approvalSweepSeconds = approvalSweepSeconds
        self._drainBatchSize = max(1, drainBatchSize)
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
        """单轮异常隔离：**清扫**与**执行**各自独立。

        ⚠️ 不能放在同一个 try 里：清扫（审批超时）是**次要**职责，执行命令是
        **主要**职责 ⇒ 清扫一失败（DB 抖动 / 审批表被锁），命令就再也领不到 ⇒
        外部写永久停摆，而现场只看到一条「轮询失败」。
        ⇒ 两条职责各自 try/except：谁炸谁降级，另一条照常。
        """

        try:
            await self.sweepApprovals()
        except Exception:
            logger.exception("worker %s 清扫超期审批失败（不影响命令执行）", self._workerId)

        try:
            await self.runOnce()
        except Exception:
            logger.exception("worker %s 本轮命令执行失败，下轮重试", self._workerId)

    # ------------------------------------------------------------------
    # 命令执行
    # ------------------------------------------------------------------

    async def runOnce(self) -> bool:
        """领取并执行待处理命令，直到本轮**排空**（批量上限保护）。

        返回 ``True`` 表示「本轮至少领取成功一条」——即使执行失败，
        领取本身已完成，调用方仍应立即进入下一轮。

        ⚠️ **三条纪律**：

        1. **排空**：一轮只领一条 ⇒ 吞吐被 ``workflowPollSeconds``（默认 2s）
           压死 ⇒ 约 30 条/分钟 ⇒ 实例越多越追不上 ⇒ 本轮把「未领取」的命令
           全部处理掉（上限 ``drainBatchSize``）。
        2. **重试有节奏**：可重试失败（``FAILED``）**每轮只给一次机会**
           ⇒ 不会在同一轮里被反复领取（热转）⇒ 毒丸命令不烧 CPU；
           同时它也不会饿死「未领取」的命令（排空只领 ``PENDING``）。
        3. **预算含第一次领取**：本轮**总共**最多领 ``drainBatchSize`` 条
           ⇒ 早期实现是「第一次 + 排空 N 条」= N+1 条 ⇒ 批量上限形同虚设
           （宣称的 50 实际是 51 ⇒ 上限失真 ⇒ 批量大时一轮跑得比预期久）。
        """

        claimedAny = False
        async with self._sessionFactory() as session:
            budget = self._drainBatchSize
            # 第一次领取**允许**可重试失败（给上一轮的失败一次机会）。
            if await self._executeOne(session, includeRetryable=True):
                claimedAny = True
                budget -= 1
            while budget > 0:
                if not await self._executeOne(session, includeRetryable=False):
                    break
                claimedAny = True
                budget -= 1
        return claimedAny

    async def _executeOne(self, session: AsyncSession, *, includeRetryable: bool) -> bool:
        """领取并执行一条命令；没有可领取的命令时返回 ``False``。"""

        command = await self._store.claimNextCommand(
            session,
            workerId=self._workerId,
            # ⚠️ **取自执行器**（唯一出口）⇒ 领取过滤条件与失败终态判定
            #   用的是**同一个**上限 ⇒ 不会出现「领得到却永远不判终态」。
            maxAttempts=self._executor.maxAttempts,
            includeRetryable=includeRetryable,
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


def buildCommandExecutor(
    settings: Settings,
    registry: CommandHandlerRegistry,
) -> CommandExecutor:
    """**组装根**：按设置构造执行器。

    ⚠️ **为什么必须有这个函数**：最大尝试次数与适配器超时都从**设置**来，
    而这两项在执行器里会被用到**两处**（领取过滤条件 / 失败终态判定）
    ⇒ 必须**一处装配** ⇒ 否则改了设置忘了另一处 ⇒ 领取口径与终态口径打架
    （命令领得到却永远不判终态 ⇒ 无限重试）。
    """

    return CommandExecutor(
        registry,
        maxAttempts=settings.workflowMaxCommandAttempts,
        commandTimeoutSeconds=settings.workflowCommandTimeoutSeconds,
    )


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
        buildCommandExecutor(settings, registry),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
