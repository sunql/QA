"""Agent 批量调度服务（Phase 7 G5 feat-agent-scheduler）。

cron 表达式的定时调度：CRUD + 到期执行。PG ``agent_schedule`` 表即事实源
（``next_run_at`` 落库），独立 worker 进程（``app.workers.agent_scheduler_worker``）
轮询到期行并调用 ``AgentRuntimeService.run``：

- ``computeNextRun``：croniter 解析，from_time 取严格下一个时刻；
- ``dueSchedules``：is_active 且 next_run_at <= now 的到期行；
- ``runSchedule``：claim（条件 UPDATE 前移 next_run_at，防并发双跑）→ 执行
  → 写 ``agent_run_log``（status/answer/error/tokens/cost/actor=scheduler:{id}）；
- 执行失败不抛异常：记录 status=error 日志，next_run_at 已前移，避免 crash-loop。

安全：写操作（create/toggle/delete）经 ``AclService.assertCanModify``
（admin 或 owner 部门）；读操作（list）任意登录用户。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.error_messages import (
    MSG_AGENT_RUN_BAD_INPUT,
    MSG_SCHEDULE_INVALID_CRON,
    MSG_SCHEDULE_NOT_FOUND,
)
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import (
    AgentDefinition,
    AgentRunLog,
    AgentSchedule,
)
from app.domain.schemas import AgentRunLogRead, AgentScheduleCreate, AgentScheduleRead
from app.services.acl_service import AclService
from app.services.agent_registry_service import AgentRegistryService
from app.services.agent_runtime_service import AgentRuntimeService

# 运行日志错误信息截断长度（避免把内部路径/堆栈细节暴露给 API）
_MAX_ERROR_LEN = 500


def _scheduleToRead(schedule: AgentSchedule, agent_code: str) -> AgentScheduleRead:
    """AgentSchedule ORM → 读模型（补 agent_code）。"""
    return AgentScheduleRead(
        id=schedule.id,
        agent_code=agent_code,
        cron_expression=schedule.cron_expression,
        params=schedule.params or {},
        is_active=schedule.is_active,
        last_run_at=schedule.last_run_at,
        next_run_at=schedule.next_run_at,
        created_time=schedule.created_time,
        updated_time=schedule.updated_time,
    )


def _logToRead(log: AgentRunLog) -> AgentRunLogRead:
    return AgentRunLogRead(
        id=log.id,
        schedule_id=log.schedule_id,
        agent_code=log.agent_code,
        status=log.status,
        answer=log.answer,
        error=log.error,
        tokens_used=log.tokens_used,
        cost=float(log.cost),
        actor=log.actor,
        started_at=log.started_at,
        finished_at=log.finished_at,
    )


class AgentSchedulerService:
    """cron 定时调度的 CRUD + 到期执行编排。"""

    def __init__(
        self,
        *,
        runtime: AgentRuntimeService | None = None,
        agents: AgentRegistryService | None = None,
        acl: AclService | None = None,
    ) -> None:
        self._runtime = runtime or AgentRuntimeService()
        self._agents = agents or AgentRegistryService()
        self._acl = acl or AclService()

    # ------------------------------------------------------------------
    # cron 解析（纯函数，可单测）
    # ------------------------------------------------------------------

    @staticmethod
    def is_valid_cron(expression: str) -> bool:
        """是否为合法 cron（5 位或 6 位含秒）。"""
        return bool(expression) and croniter.is_valid(expression)

    @staticmethod
    def computeNextRun(expression: str, from_time: datetime) -> datetime:
        """from_time 之后的下一个 cron 触发时刻（严格大于，UTC aware）。

        非法表达式 → ValidationError(422)。
        """
        if not AgentSchedulerService.is_valid_cron(expression):
            raise ValidationError(
                MSG_SCHEDULE_INVALID_CRON.format(expression=expression)
            )
        return croniter(expression, from_time).get_next(datetime)

    # ------------------------------------------------------------------
    # CRUD（写操作经 assertCanModify：admin 或 owner 部门）
    # ------------------------------------------------------------------

    async def createSchedule(
        self,
        session: AsyncSession,
        agent_code: str,
        dto: AgentScheduleCreate,
        actor: CurrentUser,
    ) -> AgentScheduleRead:
        """创建调度：校验 agent + cron + owner，计算 next_run_at，落库。"""
        entity = await self._agents.getAgent(session, agent_code)  # NotFoundError(404)
        self._acl.assertCanModify(actor, entity.owner, "Agent", agent_code)

        now = datetime.now(timezone.utc)
        next_run_at = self.computeNextRun(dto.cron_expression, now)

        schedule = AgentSchedule(
            agent_id=entity.id,
            cron_expression=dto.cron_expression,
            params=dict(dto.params or {}),
            is_active=True,
            next_run_at=next_run_at,
        )
        session.add(schedule)
        await session.commit()
        await session.refresh(schedule)
        return _scheduleToRead(schedule, agent_code)

    async def listSchedules(
        self,
        session: AsyncSession,
        agent_code: str,
        actor: CurrentUser,  # noqa: ARG001 - 读操作任意登录用户
    ) -> list[AgentScheduleRead]:
        entity = await self._agents.getAgent(session, agent_code)  # NotFoundError(404)
        rows = (
            (
                await session.execute(
                    select(AgentSchedule)
                    .where(AgentSchedule.agent_id == entity.id)
                    .order_by(AgentSchedule.next_run_at)
                )
            )
            .scalars()
            .all()
        )
        return [_scheduleToRead(s, agent_code) for s in rows]

    async def toggleSchedule(
        self,
        session: AsyncSession,
        agent_code: str,
        schedule_id: int,
        actor: CurrentUser,
    ) -> AgentScheduleRead:
        """启停调度。从停→启且 next_run_at 已过期时，重算 next_run_at（避免立即补跑）。"""
        entity = await self._agents.getAgent(session, agent_code)  # NotFoundError(404)
        self._acl.assertCanModify(actor, entity.owner, "Agent", agent_code)
        schedule = await self._getSchedule(session, entity.id, schedule_id)

        schedule.is_active = not schedule.is_active
        if schedule.is_active:
            now = datetime.now(timezone.utc)
            if schedule.next_run_at is None or schedule.next_run_at <= now:
                schedule.next_run_at = self.computeNextRun(
                    schedule.cron_expression, now
                )
        await session.commit()
        await session.refresh(schedule)
        return _scheduleToRead(schedule, agent_code)

    async def deleteSchedule(
        self,
        session: AsyncSession,
        agent_code: str,
        schedule_id: int,
        actor: CurrentUser,
    ) -> None:
        """删除调度（运行日志保留：FK ondelete SET NULL）。"""
        entity = await self._agents.getAgent(session, agent_code)  # NotFoundError(404)
        self._acl.assertCanModify(actor, entity.owner, "Agent", agent_code)
        schedule = await self._getSchedule(session, entity.id, schedule_id)
        await session.delete(schedule)
        await session.commit()

    async def _getSchedule(
        self, session: AsyncSession, agent_id: int, schedule_id: int
    ) -> AgentSchedule:
        row = (
            await session.execute(
                select(AgentSchedule).where(
                    AgentSchedule.id == schedule_id,
                    AgentSchedule.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(
                MSG_SCHEDULE_NOT_FOUND.format(schedule_id=schedule_id)
            )
        return row

    # ------------------------------------------------------------------
    # 到期执行（worker 调用）
    # ------------------------------------------------------------------

    async def dueSchedules(
        self, session: AsyncSession, now: datetime
    ) -> list[AgentSchedule]:
        """is_active 且 next_run_at <= now 的到期调度（按应跑时间升序）。"""
        rows = (
            (
                await session.execute(
                    select(AgentSchedule)
                    .where(
                        AgentSchedule.is_active.is_(True),
                        AgentSchedule.next_run_at.is_not(None),
                        AgentSchedule.next_run_at <= now,
                    )
                    .order_by(AgentSchedule.next_run_at)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def runSchedule(
        self,
        session: AsyncSession,
        schedule: AgentSchedule,
        now: datetime,
    ) -> AgentRunLogRead | None:
        """执行一个到期调度，返回运行日志；未到期 / 被并发抢占 / agent 已删 → None。

        执行失败不抛异常：写 status=error 日志（优雅记录，next_run_at 已前移）。
        """
        if (
            not schedule.is_active
            or schedule.next_run_at is None
            or schedule.next_run_at > now
        ):
            return None

        schedule_id = schedule.id
        agent_id = schedule.agent_id
        cron_expression = schedule.cron_expression
        old_next = schedule.next_run_at
        input_text = str((schedule.params or {}).get("input", ""))

        # claim：条件 UPDATE 前移 next_run_at —— 只有 next_run_at 仍等于旧值的
        # 会话能赢（并发 worker 双跑防护）。从应跑时间算下一次（保持节奏）。
        # 若应跑时间已过（late worker），从 now 算以保证 future（test 场景需此保障）。
        from_time = old_next if old_next > now else now
        new_next = self.computeNextRun(cron_expression, from_time)
        result = await session.execute(
            AgentSchedule.__table__.update()
            .where(
                AgentSchedule.id == schedule_id,
                AgentSchedule.next_run_at == old_next,
                AgentSchedule.is_active.is_(True),
            )
            .values(next_run_at=new_next)
        )
        await session.commit()
        if result.rowcount != 1:
            return None  # 已停用或被并发 worker 抢占

        # expire 强制 ORM 重新从 DB 加载（raw UPDATE 绕过了 identity-map）
        session.expire(schedule)
        schedule = await session.get(AgentSchedule, schedule_id)

        # agent_code 从 agent_id 解析（schedule 无冗余列；agent 删除会级联删 schedule）
        agent_code = (
            await session.execute(
                select(AgentDefinition.agent_code).where(
                    AgentDefinition.id == agent_id
                )
            )
        ).scalar_one_or_none()
        if agent_code is None:
            return None

        actor = f"scheduler:{schedule_id}"
        started_at = now
        try:
            run = await self._runtime.run(
                session, agent_code, input_text, actor=actor,
            )
            status = "success"
            answer = run.answer
            error = None
            tokens_used = run.tokens_used
            cost = run.cost
        except Exception as exc:  # noqa: BLE001 - 调度执行失败只记录 error 日志，不崩溃
            status = "error"
            answer = None
            err_msg = f"{type(exc).__name__}: {exc}"
            error = err_msg[: _MAX_ERROR_LEN] if len(err_msg) > _MAX_ERROR_LEN else err_msg
            tokens_used = 0
            cost = Decimal("0")

        log = AgentRunLog(
            schedule_id=schedule_id,
            agent_code=agent_code,
            status=status,
            answer=answer,
            error=error,
            tokens_used=tokens_used,
            cost=Decimal(str(cost)),
            actor=actor,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )
        session.add(log)
        schedule.last_run_at = started_at
        await session.commit()
        await session.refresh(log)
        return _logToRead(log)

    async def listRunLogs(
        self,
        session: AsyncSession,
        agent_code: str,
        actor: CurrentUser,
        *,
        schedule_id: int | None = None,
    ) -> list[AgentRunLogRead]:
        """调度执行历史（前端 schedule 面板数据源）。按 agent_code（+可选 schedule_id）。

        读权限：任意登录用户（owner-based ACL 不限制读）。
        """
        entity = await self._agents.getAgent(session, agent_code)  # NotFoundError(404)
        stmt = select(AgentRunLog).where(AgentRunLog.agent_code == agent_code)
        if schedule_id is not None:
            stmt = stmt.where(AgentRunLog.schedule_id == schedule_id)
        stmt = stmt.order_by(AgentRunLog.started_at.desc()).limit(100)
        rows = (await session.execute(stmt)).scalars().all()
        return [_logToRead(log) for log in rows]
