"""进程内持久任务台账（Task 10+11）：入队 / 认领 / 收尾 / 启动回收。

**职责边界**：本模块只碰 ``research_task`` 表与调用方传入的 session（与
``research_session_service`` 同口径 —— **全部不 commit，事务归调用方**），
唯一的例外是 ``reapOrphanedTasks`` 里对 ``sessionService.failSessionIfActive`` 的
调用（同样不 commit，由调用方决定）。

**形态裁定**（见 brief §0）：保留现有 ``BackgroundTasks`` 内联执行与进程内 SSE
总线不变，只加持久台账 + 启动回收。**不**新增独立进程 / compose 服务 / 自动重跑。

**已知限制**（如实登记，不实现）：``lease_until`` 只记录、reaper 不按它 gate ——
本应用是**单后端进程**，启动时上一进程的任何 run 都已死亡，与租约是否到期无关；
若只标「已过期」，则「崩溃后立刻重启」的窗口（租约未到期）根本回收不到，会话仍被
永久锁死。**多副本部署下**，A 副本启动会回收 B 副本正在跑的任务 ⇒ 需周期回收 +
租约 gate 的守护（后续任务）。

**覆盖面边界**（如实登记，不实现）：``reapOrphanedTasks`` 只回收 ``status='leased'``
（崩在**跑中**）。它**不**覆盖「入队台账 ``commit`` 成功、后台任务被 ``claimTask`` 认领
之前」这一窗口崩溃后留下的 ``pending`` 台账 —— 例如 ``answerCheckpoint`` 里 commit 成功、
``background.add_task`` 之前进程死掉：台账停在 ``pending``、会话停在 ``awaiting_user``
且仍持 ``active_run_id`` ⇒ 重启后 reaper 扫不到它，**不自愈**：用户需自行
``POST /abort``（``awaiting_user`` 正是 abort 的唯一白名单）或重答该 checkpoint 才能续跑
（**可恢复，非永久死路**）。**为何不顺手回收 ``pending``**：启动时 ``awaiting_user`` +
持 ``active_run_id`` 是**设计内的合法状态**（跨重启的暂停会话，运行只是暂停等用户决策），
naive 回收会把它误杀 ⇒ 修法需人类裁定（已登记的后续项）。
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchTask

logger = logging.getLogger(__name__)

TASK_KIND_TURN = "turn"
TASK_KIND_RESUME = "resume"
TASK_KINDS = frozenset({TASK_KIND_TURN, TASK_KIND_RESUME})
"""任务类型：``turn``（新轮次）/ ``resume``（checkpoint 决策续跑）。"""

TASK_STATUS_PENDING = "pending"
TASK_STATUS_LEASED = "leased"
TASK_STATUS_DONE = "done"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_ORPHANED = "orphaned"
TASK_STATUSES = frozenset(
    {
        TASK_STATUS_PENDING,
        TASK_STATUS_LEASED,
        TASK_STATUS_DONE,
        TASK_STATUS_FAILED,
        TASK_STATUS_ORPHANED,
    }
)
TERMINAL_TASK_STATUSES = frozenset(
    {TASK_STATUS_DONE, TASK_STATUS_FAILED, TASK_STATUS_ORPHANED}
)
"""台账终态：``done`` / ``failed`` 由执行体写，``orphaned`` **只**由 reaper 写。"""

_FINISHABLE_STATUSES = frozenset({TASK_STATUS_DONE, TASK_STATUS_FAILED})
"""``finishTask`` 允许写的状态（``orphaned`` 不在此列 —— 那是 reaper 的专属语义）。"""

DEFAULT_LEASE_SECONDS = 1800
"""默认租约 30min；env ``RESEARCH_TASK_LEASE_SECONDS`` 可覆盖（仅**记录**，见模块 docstring）。"""
_LEASE_SECONDS_ENV = "RESEARCH_TASK_LEASE_SECONDS"

ORPHAN_ERROR = "进程重启时任务仍处于执行中"
"""reaper 写入 ``error`` 的原因文案（单一字面量，测试与观测共用）。"""


def _now() -> datetime:
    """时区感知的当前 UTC 时间。"""
    return datetime.now(UTC)


def _resolveLeaseSeconds(override: int | None) -> int:
    """解析租约秒数：显式传入 > env > 默认。env 非法只告警并回落默认（不抛，认领不应因此失败）。"""
    if override is not None:
        return override
    raw = os.environ.get(_LEASE_SECONDS_ENV)
    if raw is None:
        return DEFAULT_LEASE_SECONDS
    try:
        return int(raw)
    except ValueError:
        logger.warning("非法 %s=%r，回落默认 %ds", _LEASE_SECONDS_ENV, raw, DEFAULT_LEASE_SECONDS)
        return DEFAULT_LEASE_SECONDS


async def enqueueTask(
    session: AsyncSession,
    *,
    sessionId: uuid.UUID,
    turnId: uuid.UUID | None,
    kind: str,
    payload: dict | None = None,
) -> ResearchTask:
    """插入一条 ``pending`` 台账并 ``flush``（返回带 id 的对象）。非法 ``kind`` 显式抛。"""
    if kind not in TASK_KINDS:
        logger.warning("非法任务类型: kind=%s", kind)
        raise ValueError(f"非法任务类型 {kind}，合法值: {sorted(TASK_KINDS)}")
    row = ResearchTask(
        session_id=sessionId,
        turn_id=turnId,
        kind=kind,
        status=TASK_STATUS_PENDING,
        payload=dict(payload or {}),
    )
    session.add(row)
    await session.flush()
    logger.info("入队研究任务: id=%s session=%s kind=%s", row.id, sessionId, kind)
    return row


async def claimTask(
    session: AsyncSession,
    *,
    taskId: uuid.UUID,
    workerId: str,
    leaseSeconds: int | None = None,
) -> bool:
    """原子认领：条件 UPDATE ``WHERE id=:id AND status='pending'`` ⇒ ``rowcount==1`` 为 True。

    已是 ``done/failed/orphaned/leased`` ⇒ False（**不重复执行**同一单元）。先读后写是
    TOCTOU：两条并发后台任务都读到 pending ⇒ 双双放行 ⇒ 同一单元跑两遍（同
    ``claimActiveRun`` / ``releaseActiveRun`` 的原子条件 UPDATE 口径）。**不 commit**。
    """
    lease = _resolveLeaseSeconds(leaseSeconds)
    result = await session.execute(
        update(ResearchTask)
        .where(ResearchTask.id == taskId, ResearchTask.status == TASK_STATUS_PENDING)
        .values(
            status=TASK_STATUS_LEASED,
            worker_id=workerId,
            lease_until=_now() + timedelta(seconds=lease),
            attempt=ResearchTask.attempt + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        logger.info("认领研究任务: task=%s worker=%s", taskId, workerId)
        return True
    status = await session.scalar(
        select(ResearchTask.status).where(ResearchTask.id == taskId)
    )
    logger.warning("研究任务认领未命中（已被处理或不存在）: task=%s status=%s", taskId, status)
    return False


async def finishTask(
    session: AsyncSession,
    *,
    taskId: uuid.UUID,
    status: str,
    error: str | None = None,
) -> None:
    """收尾：条件 UPDATE ``WHERE id=:id AND status='leased'``。**不 commit**。

    ``status`` 只允许 ``done`` / ``failed``（``orphaned`` 归 reaper）。未命中只记 WARNING
    （对账：谁改的？）。调用方负责把它挂进**同一次**提交或新事务（见 ``research.py`` 的
    ``_markTerminalFailure`` / ``_finishTaskQuietly``）。
    """
    if status not in _FINISHABLE_STATUSES:
        logger.warning("非法收尾状态: task=%s status=%s", taskId, status)
        raise ValueError(f"非法收尾状态 {status}，合法值: {sorted(_FINISHABLE_STATUSES)}")
    result = await session.execute(
        update(ResearchTask)
        .where(ResearchTask.id == taskId, ResearchTask.status == TASK_STATUS_LEASED)
        .values(status=status, error=error)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        logger.warning("研究任务收尾未命中（对账：谁改的？）: task=%s status=%s", taskId, status)


async def reapOrphanedTasks(
    session: AsyncSession, *, sessionService
) -> list[uuid.UUID]:
    """启动回收：标记**所有** ``leased`` 任务为 ``orphaned`` 并释放其会话执行权。

    返回被 orphan 的 ``session_id`` 列表（去重、保序）。**不 commit**（调用方提交）。

    **覆盖面（限定）**：只回收 ``leased``（崩在**跑中**）——「死路」这一说法只对 ``leased``
    成立。**不**覆盖「入队 commit 成功、后台任务被 ``claimTask`` 认领之前」崩溃留下的
    ``pending`` 台账（如 ``answerCheckpoint`` 的 commit → ``background.add_task`` 窗口）：
    该窗口重启后不自愈，用户需 ``POST /abort`` 或重答该 checkpoint 才能续跑（可恢复，非永久
    死路）。详见模块 docstring「覆盖面边界」。

    **为什么标所有 leased 而非只标过期**：单进程下「启动即全孤儿」才是正确且可测的
    判据（详见模块 docstring）。幂等：第二次没有 ``leased`` 行 ⇒ 返回空、无额外写入。
    """
    result = await session.execute(
        update(ResearchTask)
        .where(ResearchTask.status == TASK_STATUS_LEASED)
        .values(status=TASK_STATUS_ORPHANED, error=ORPHAN_ERROR)
        .returning(ResearchTask.session_id)
        .execution_options(synchronize_session=False)
    )
    orphanedSessionIds = list(result.scalars().all())
    sessionIds = list(dict.fromkeys(orphanedSessionIds))
    for sid in sessionIds:
        # **条件**失败（只用非终态会话）：无条件 failSession 会把已 done 的会话翻成
        # failed 丢报告。释放执行权就是这一步（清 active_run_id）。
        freed = await sessionService.failSessionIfActive(session, sid)
        if not freed:
            logger.info("孤儿任务的会话已终态，仅修正台账: session=%s", sid)
    if sessionIds:
        logger.warning(
            "启动回收孤儿任务：%d 条台账、%d 个会话",
            len(orphanedSessionIds),
            len(sessionIds),
        )
    return sessionIds
