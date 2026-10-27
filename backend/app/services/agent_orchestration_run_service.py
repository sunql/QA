"""编排运行的 run 生命周期服务（Task 7）。

职责：
- ``createRun``：校验 Agent（ACTIVE）/ profile（启用）/ 工具绑定（至少一个当前有效）/
  模型（存在且 active），冻结 instructions + 工具快照 + 预算，落库为 QUEUED；
- ``listRuns`` / ``getRun`` / ``listSteps``：按所有权读取（创建者或 admin），
  他人的 run 一律 ``NotFoundError("agent run not found")``；
- ``resumeRun``：仅从 PAUSED 恢复，按 pause_code 施加输入/预算规则，复查当前
  Agent/profile/模型/绑定（PERMISSION_DENIED 时按**当前** ACL 复查快照工具），
  重新排队；
- ``cancelRun``：QUEUED/PAUSED 立即 CANCELLED；RUNNING 只置 cancel_requested。

设计约束：
- 快照条目只含 ``toolName`` / ``description`` / ``inputSchema`` / ``sortOrder``，
  绝不包含 handler_ref、凭据或其它工具配置内部信息；
- 历史 run 不依赖 Agent 存活（``run.agent_id`` 为 ON DELETE SET NULL），
  读路径只查 run 表；
- 预算上限与「当前有效工具」判定复用
  ``app.services.agent_orchestration_validation`` 的共享助手（不复制粘贴）；
- 审计经 ``OutboxService`` 与业务写入同事务提交，payload 只含 run 自身公开字段；
- 本任务不实现 Worker claim（lease 恒为 NULL），也不做计量（成本恒为 UNKNOWN）。
"""

from __future__ import annotations

import copy
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import CurrentUser
from app.domain.agent_orchestration_models import (
    AgentOrchestrationBinding,
    AgentOrchestrationProfile,
    AgentOrchestrationRun,
    AgentOrchestrationStep,
)
from app.domain.agent_orchestration_schemas import (
    AgentRunCreate,
    AgentRunCreated,
    AgentRunLimits,
    AgentRunRead,
    AgentRunResume,
    AgentRunStepRead,
)
from app.domain.enums import AgentStatus
from app.domain.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.domain.models import AgentDefinition, AgentToolConfig, LlmConfig
from app.services.acl_service import ADMIN_ROLE
from app.services.agent_orchestration_tool_executor import (
    MSG_ORCH_PERMISSION_DENIED,
)
from app.services.agent_orchestration_types import (
    CostSource,
    PauseCode,
    RunStatus,
    StepKind,
    StepStatus,
)
from app.services.agent_orchestration_validation import (
    assertWithinSystemCeilings,
    isOrchestrationToolValid,
    systemCeilings,
)
from app.services.agent_registry_service import AgentRegistryService
from app.services.agent_runtime_service import AgentRuntimeService
from app.services.agent_tool_config_registry import (
    AgentToolConfigRegistry,
    agent_tool_config_registry,
)
from app.services.outbox_service import OutboxService

logger = logging.getLogger(__name__)

MSG_RUN_NOT_FOUND = "agent run not found"

_AUDIT_EVENT_PREFIX = "agent_orchestration_run"
_AUDIT_ENTITY_TYPE = "agent_orchestration_run"

MAX_PAGE_LIMIT = 200
DEFAULT_RUN_LIMIT = 50
DEFAULT_STEP_LIMIT = 100

# 补充输入追加到原问题时的分隔标记（只追加，不覆盖）。
SUPPLEMENT_SEPARATOR = "\n\n--- 补充输入 ---\n"

_TERMINAL_STATUSES = frozenset({
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
})

# pause_detail.budget → 必须提供的增量字段。
_BUDGET_INCREMENT_BY_BUDGET = {
    "iteration": "additional_iterations",
    "iterations": "additional_iterations",
    "tool": "additional_tool_calls",
    "tool_calls": "additional_tool_calls",
    "cost": "additional_cost_usd",
    "time": "additional_timeout_seconds",
    "timeout": "additional_timeout_seconds",
}

# 增量字段 → 受影响的预算字段。
_LIMIT_FIELD_BY_INCREMENT = {
    "additional_iterations": "max_iterations",
    "additional_tool_calls": "max_tool_calls",
    "additional_cost_usd": "max_cost_usd",
    "additional_timeout_seconds": "timeout_seconds",
}


class AgentOrchestrationRunService:
    """编排运行的创建、读取、恢复与取消。"""

    def __init__(
        self,
        *,
        agents: AgentRegistryService | None = None,
        outbox: OutboxService | None = None,
        registry: AgentToolConfigRegistry | None = None,
        runtime: AgentRuntimeService | None = None,
    ) -> None:
        self._agents = agents or AgentRegistryService()
        self._outbox = outbox or OutboxService()
        self._registry = registry or agent_tool_config_registry
        self._runtime = runtime or AgentRuntimeService()

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    async def createRun(
        self,
        session: AsyncSession,
        dto: AgentRunCreate,
        actor: CurrentUser,
    ) -> AgentRunCreated:
        """创建一次编排运行（QUEUED）。任一门槛不满足都不落库。"""
        agent = await self._agents.getAgent(session, dto.agent_code)
        self._assertAgentActive(agent)
        profile = await self._loadEnabledProfile(session, agent)
        modelConfig = await self._loadActiveModel(session, dto.model_config_id)
        snapshot = await self._buildToolSnapshot(session, profile)
        limits = self._mergeLimits(profile, dto.limits)
        assertWithinSystemCeilings(limits)

        run = AgentOrchestrationRun(
            run_id=str(uuid.uuid4()),
            agent_id=agent.id,
            agent_code=agent.agent_code,
            profile_revision=profile.revision,
            instructions_snapshot=profile.instructions,
            tool_bindings_snapshot=snapshot,
            max_observation_chars=profile.max_observation_chars,
            question=dto.input,
            created_by=actor.userId,
            model_config_id=modelConfig.id,
            status=RunStatus.QUEUED.value,
            max_iterations=int(limits["max_iterations"]),
            max_tool_calls=int(limits["max_tool_calls"]),
            max_cost_usd=Decimal(str(limits["max_cost_usd"])),
            timeout_seconds=int(limits["timeout_seconds"]),
            iteration_count=0,
            tool_call_count=0,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cost=Decimal("0"),
            cost_source=CostSource.UNKNOWN.value,
            cost_complete=False,
            cancel_requested=False,
            revision=1,
            queued_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        await self._enqueueAudit(
            session,
            run,
            actor,
            eventAction="created",
            before=None,
        )
        await session.commit()
        return AgentRunCreated(run_id=run.run_id, status=RunStatus.QUEUED)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def listRuns(
        self,
        session: AsyncSession,
        actor: CurrentUser,
        *,
        agentCode: str | None = None,
        status: RunStatus | None = None,
        limit: int = DEFAULT_RUN_LIMIT,
        offset: int = 0,
    ) -> list[AgentRunRead]:
        """列出**当前用户自己**的 run（按 id 倒序，最新在前）。

        admin 也只在这里看到自己的 run；他人的 run 由详情接口按 id 单独读取。
        """
        stmt = select(AgentOrchestrationRun).where(
            AgentOrchestrationRun.created_by == actor.userId
        )
        if agentCode:
            stmt = stmt.where(AgentOrchestrationRun.agent_code == agentCode)
        if status is not None:
            stmt = stmt.where(AgentOrchestrationRun.status == status.value)
        stmt = (
            stmt.order_by(AgentOrchestrationRun.id.desc())
            .limit(_capLimit(limit))
            .offset(max(0, offset))
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [runToRead(row) for row in rows]

    async def getRun(
        self,
        session: AsyncSession,
        runId: str,
        actor: CurrentUser,
    ) -> AgentRunRead:
        """读取单个 run（创建者或 admin；否则统一 404）。"""
        run = await self._loadReadableRun(session, runId, actor)
        return runToRead(run)

    async def listSteps(
        self,
        session: AsyncSession,
        runId: str,
        actor: CurrentUser,
        *,
        afterStepNo: int = 0,
        limit: int = DEFAULT_STEP_LIMIT,
    ) -> list[AgentRunStepRead]:
        """按 step_no 升序返回轨迹；``afterStepNo`` 为**闭区间下界**。"""
        run = await self._loadReadableRun(session, runId, actor)
        stmt = (
            select(AgentOrchestrationStep)
            .where(
                AgentOrchestrationStep.run_id == run.id,
                AgentOrchestrationStep.step_no >= afterStepNo,
            )
            .order_by(AgentOrchestrationStep.step_no)
            .limit(_capLimit(limit))
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [stepToRead(row) for row in rows]

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------

    async def resumeRun(
        self,
        session: AsyncSession,
        runId: str,
        dto: AgentRunResume,
        actor: CurrentUser,
    ) -> AgentRunRead:
        """从 PAUSED 恢复运行：重新排队（QUEUED）。"""
        run = await self._loadReadableRun(session, runId, actor, forUpdate=True)
        if run.status != RunStatus.PAUSED.value:
            raise ConflictError(
                f"只有暂停中的编排运行可以恢复（当前状态 {run.status}）"
            )
        before = _runToAuditDict(run)
        self._assertPauseRequirements(run, dto)

        agent = await self._loadAgentForRun(session, run)
        self._assertAgentActive(agent)
        profile = await self._loadEnabledProfile(session, agent)
        await self._loadActiveModel(session, run.model_config_id)
        # 至少一个当前有效的绑定（快照本身保持冻结，不被新配置覆盖）。
        await self._buildToolSnapshot(session, profile)
        if run.pause_code == PauseCode.PERMISSION_DENIED.value:
            await self._assertSnapshotToolsAllowed(
                session, agent, run.tool_bindings_snapshot
            )

        limits = self._applyIncrements(run, dto)

        if dto.input:
            run.question = f"{run.question}{SUPPLEMENT_SEPARATOR}{dto.input}"
        run.pause_code = None
        run.pause_detail = None
        run.error_code = None
        run.error_message = None
        run.cancel_requested = False
        run.lease_owner = None
        run.lease_until = None
        run.started_at = None
        run.status = RunStatus.QUEUED.value
        run.revision = run.revision + 1
        run.queued_at = datetime.now(UTC)
        run.max_iterations = int(limits["max_iterations"])
        run.max_tool_calls = int(limits["max_tool_calls"])
        run.max_cost_usd = Decimal(str(limits["max_cost_usd"]))
        run.timeout_seconds = int(limits["timeout_seconds"])
        await session.flush()
        await self._enqueueAudit(
            session,
            run,
            actor,
            eventAction="updated",
            before=before,
        )
        await session.commit()
        return runToRead(run)

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    async def cancelRun(
        self,
        session: AsyncSession,
        runId: str,
        actor: CurrentUser,
    ) -> AgentRunRead:
        """取消运行：QUEUED/PAUSED 立即终止；RUNNING 只标记取消请求。"""
        run = await self._loadReadableRun(session, runId, actor, forUpdate=True)
        if run.status in _TERMINAL_STATUSES:
            raise ConflictError(
                f"已结束的编排运行无法取消（当前状态 {run.status}）"
            )
        before = _runToAuditDict(run)
        if run.status == RunStatus.RUNNING.value:
            # 运行中不伪装成已终止：只置标志，由 Worker 在安全点收尾。
            run.cancel_requested = True
        else:
            run.status = RunStatus.CANCELLED.value
            run.finished_at = datetime.now(UTC)
        run.revision = run.revision + 1
        await session.flush()
        await self._enqueueAudit(
            session,
            run,
            actor,
            eventAction="updated",
            before=before,
        )
        await session.commit()
        return runToRead(run)

    # ------------------------------------------------------------------
    # 当前状态加载
    # ------------------------------------------------------------------

    async def _loadReadableRun(
        self,
        session: AsyncSession,
        runId: str,
        actor: CurrentUser,
        *,
        forUpdate: bool = False,
    ) -> AgentOrchestrationRun:
        """按 run_id 取 run；不存在或无权访问一律同一 NotFoundError。"""
        stmt = select(AgentOrchestrationRun).where(
            AgentOrchestrationRun.run_id == runId
        )
        if forUpdate:
            stmt = stmt.with_for_update()
        run = (await session.execute(stmt)).scalar_one_or_none()
        if run is None or not _isReadable(run, actor):
            raise NotFoundError(MSG_RUN_NOT_FOUND)
        return run

    async def _loadAgentForRun(
        self,
        session: AsyncSession,
        run: AgentOrchestrationRun,
    ) -> AgentDefinition:
        """按 run 解析当前 Agent（agent_id 优先，回落 agent_code）。"""
        stmt = select(AgentDefinition).options(
            selectinload(AgentDefinition.policies)
        )
        if run.agent_id is not None:
            stmt = stmt.where(AgentDefinition.id == run.agent_id)
        else:
            stmt = stmt.where(AgentDefinition.agent_code == run.agent_code)
        agent = (await session.execute(stmt)).scalar_one_or_none()
        if agent is None:
            raise ConflictError("编排运行对应的 Agent 当前不可用")
        return agent

    def _assertAgentActive(self, agent: AgentDefinition) -> None:
        if agent.status != AgentStatus.ACTIVE.value:
            raise ConflictError(f"Agent {agent.agent_code} 当前不可用于编排")

    async def _loadEnabledProfile(
        self,
        session: AsyncSession,
        agent: AgentDefinition,
    ) -> AgentOrchestrationProfile:
        profile = (
            await session.execute(
                select(AgentOrchestrationProfile).where(
                    AgentOrchestrationProfile.agent_id == agent.id
                )
            )
        ).scalar_one_or_none()
        if profile is None:
            raise ConflictError(f"Agent {agent.agent_code} 尚无编排配置")
        if not profile.enabled:
            raise ConflictError(f"Agent {agent.agent_code} 的编排配置已禁用")
        return profile

    async def _loadActiveModel(
        self,
        session: AsyncSession,
        modelConfigId: int,
    ) -> LlmConfig:
        config = (
            await session.execute(
                select(LlmConfig).where(LlmConfig.id == modelConfigId)
            )
        ).scalar_one_or_none()
        if config is None:
            raise NotFoundError(f"模型配置不存在：{modelConfigId}")
        if not config.is_active:
            raise ConflictError(f"模型配置已停用：{modelConfigId}")
        return config

    # ------------------------------------------------------------------
    # 快照与预算
    # ------------------------------------------------------------------

    async def _buildToolSnapshot(
        self,
        session: AsyncSession,
        profile: AgentOrchestrationProfile,
    ) -> list[dict[str, Any]]:
        """冻结当前有效的绑定工具：按 sort_order 排序的**新** dict。

        条目只含 toolName / description / inputSchema / sortOrder，
        绝不携带 handler_ref、凭据或其它工具配置内部信息。
        """
        bindings = await self._loadBindings(session, profile.id)
        enabled = [binding for binding in bindings if binding.enabled]
        if not enabled:
            raise ConflictError("编排配置没有可用的工具绑定")
        rows = (
            (
                await session.execute(
                    select(AgentToolConfig).where(
                        AgentToolConfig.name.in_(
                            [binding.tool_name for binding in enabled]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        byName = {row.name: row for row in rows}

        snapshot: list[dict[str, Any]] = []
        for binding in sorted(enabled, key=lambda b: (b.sort_order, b.tool_name)):
            row = byName.get(binding.tool_name)
            if row is None or not isOrchestrationToolValid(row):
                continue
            snapshot.append(
                {
                    "toolName": row.name,
                    "description": row.description or "",
                    "inputSchema": copy.deepcopy(row.input_schema or {}),
                    "sortOrder": binding.sort_order,
                }
            )
        if not snapshot:
            raise ConflictError("编排配置没有可用的工具绑定")
        return snapshot

    async def _loadBindings(
        self,
        session: AsyncSession,
        profileId: int,
    ) -> list[AgentOrchestrationBinding]:
        rows = (
            (
                await session.execute(
                    select(AgentOrchestrationBinding)
                    .where(AgentOrchestrationBinding.profile_id == profileId)
                    .order_by(
                        AgentOrchestrationBinding.sort_order,
                        AgentOrchestrationBinding.tool_name,
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    def _mergeLimits(
        self,
        profile: AgentOrchestrationProfile,
        limits: AgentRunLimits | None,
    ) -> dict[str, float]:
        """profile 默认预算 + 本次请求覆盖（未提供的字段回落默认值）。"""
        merged: dict[str, float] = {
            "max_iterations": float(profile.default_max_iterations),
            "max_tool_calls": float(profile.default_max_tool_calls),
            "max_cost_usd": float(profile.default_max_cost_usd),
            "timeout_seconds": float(profile.default_timeout_seconds),
            "max_observation_chars": float(profile.max_observation_chars),
        }
        if limits is None:
            return merged
        overrides = {
            "max_iterations": limits.max_iterations,
            "max_tool_calls": limits.max_tool_calls,
            "max_cost_usd": limits.max_cost_usd,
            "timeout_seconds": limits.timeout_seconds,
        }
        for field, value in overrides.items():
            if value is not None:
                merged[field] = float(value)
        return merged

    def _applyIncrements(
        self,
        run: AgentOrchestrationRun,
        dto: AgentRunResume,
    ) -> dict[str, float]:
        """预算增量：仅在不超过系统硬上限时生效（超限 422，绝不静默截断）。"""
        ceilings = systemCeilings()
        current = {
            "max_iterations": float(run.max_iterations),
            "max_tool_calls": float(run.max_tool_calls),
            "max_cost_usd": float(run.max_cost_usd),
            "timeout_seconds": float(run.timeout_seconds),
        }
        additions = {
            "max_iterations": dto.additional_iterations,
            "max_tool_calls": dto.additional_tool_calls,
            "max_cost_usd": dto.additional_cost_usd,
            "timeout_seconds": dto.additional_timeout_seconds,
        }
        updated = dict(current)
        for field, addition in additions.items():
            if addition is None:
                continue
            newValue = current[field] + float(addition)
            ceiling = ceilings[field]
            if newValue > ceiling:
                raise ValidationError(f"{field} 超出系统上限 {ceiling}")
            updated[field] = newValue
        return updated

    # ------------------------------------------------------------------
    # pause_code 规则与 ACL 复查
    # ------------------------------------------------------------------

    def _assertPauseRequirements(
        self,
        run: AgentOrchestrationRun,
        dto: AgentRunResume,
    ) -> None:
        """按 pause_code 施加输入/预算门槛（缺输入 → 422）。"""
        pauseCode = run.pause_code
        if pauseCode in (PauseCode.NEEDS_INPUT.value, PauseCode.AMBIGUOUS.value) and not dto.input:
            raise ValidationError("恢复运行需要提供补充输入")
        if pauseCode == PauseCode.BUDGET_EXHAUSTED.value:
            required = _requiredIncrementField(run.pause_detail)
            if required is None:
                if not _hasAnyIncrement(dto):
                    raise ValidationError("恢复运行需要提供对应的预算增量")
            elif getattr(dto, required) is None:
                raise ValidationError("恢复运行需要提供对应的预算增量")

    async def _assertSnapshotToolsAllowed(
        self,
        session: AsyncSession,
        agent: AgentDefinition,
        snapshot: list[dict[str, Any]] | None,
    ) -> None:
        """按**当前** ACL 复查快照里的每个工具；仍被拒 → 409。

        复查只做策略判定，**绝不调用任何工具 handler**；错误文案保持通用，
        不泄露 data object / policy 等内部信息。
        """
        for entry in snapshot or []:
            if not isinstance(entry, dict):
                continue
            toolName = entry.get("toolName")
            if not isinstance(toolName, str) or not toolName:
                continue
            tool = await self._resolveCurrentTool(session, toolName)
            if tool is None:
                raise ConflictError("快照中的工具当前不可用")
            try:
                self._runtime.enforcePolicies(agent, tool)
            except PermissionDeniedError as exc:
                raise ConflictError(MSG_ORCH_PERMISSION_DENIED) from exc

    async def _resolveCurrentTool(self, session: AsyncSession, toolName: str):
        """按当前 DB 行重新解析工具（registry 缓存可能过期）。"""
        try:
            await self._registry.reload_one(session, toolName)
            return self._registry.get(toolName)
        except (RuntimeError, ValueError) as exc:
            logger.warning(
                "orchestration run tool unavailable (tool=%s): %s",
                toolName,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    async def _enqueueAudit(
        self,
        session: AsyncSession,
        run: AgentOrchestrationRun,
        actor: CurrentUser,
        *,
        eventAction: str,
        before: dict | None,
    ) -> None:
        await self._outbox.enqueue(
            session,
            event_type=f"{_AUDIT_EVENT_PREFIX}_{eventAction}",
            entity_type=_AUDIT_ENTITY_TYPE,
            entity_id=run.id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={
                "before": before,
                "after": _runToAuditDict(run),
            },
        )


# ---------------------------------------------------------------------------
# 模块级公开映射（前端与 Worker 集成测试共用）
# ---------------------------------------------------------------------------


def runToRead(run: AgentOrchestrationRun) -> AgentRunRead:
    """ORM run → 读模型。"""
    return AgentRunRead(
        run_id=run.run_id,
        agent_code=run.agent_code,
        question=run.question,
        created_by=run.created_by,
        model_config_id=run.model_config_id,
        status=RunStatus(run.status),
        instructions_snapshot=run.instructions_snapshot,
        tool_bindings_snapshot=list(run.tool_bindings_snapshot or []),
        max_iterations=run.max_iterations,
        max_tool_calls=run.max_tool_calls,
        max_cost_usd=float(run.max_cost_usd),
        timeout_seconds=run.timeout_seconds,
        iteration_count=run.iteration_count,
        tool_call_count=run.tool_call_count,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
        total_tokens=run.total_tokens,
        cost=float(run.cost),
        cost_source=CostSource(run.cost_source),
        cost_complete=run.cost_complete,
        final_answer=run.final_answer,
        pause_code=PauseCode(run.pause_code) if run.pause_code else None,
        pause_detail=run.pause_detail,
        error_code=run.error_code,
        error_message=run.error_message,
        cancel_requested=run.cancel_requested,
        created_time=run.created_time,
        updated_time=run.updated_time,
        queued_at=run.queued_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        revision=run.revision,
    )


def stepToRead(step: AgentOrchestrationStep) -> AgentRunStepRead:
    """ORM step → 读模型。"""
    return AgentRunStepRead(
        step_no=step.step_no,
        iteration_no=step.iteration_no,
        kind=StepKind(step.kind),
        status=StepStatus(step.status),
        action=step.action,
        plan_summary=step.plan_summary,
        tool_name=step.tool_name,
        input_json=step.input_json,
        output_json=step.output_json,
        answer=step.answer,
        error_code=step.error_code,
        error_message=step.error_message,
        prompt_tokens=step.prompt_tokens,
        completion_tokens=step.completion_tokens,
        cost=float(step.cost),
        started_at=step.started_at,
        finished_at=step.finished_at,
    )


def _runToAuditDict(run: AgentOrchestrationRun) -> dict[str, Any]:
    """审计 payload：只含 run 自身公开字段。

    绝不含模型 api key / provider 凭据 / handler_ref 等工具配置内部信息。
    """
    return {
        "runId": run.run_id,
        "agentCode": run.agent_code,
        "status": run.status,
        "modelConfigId": run.model_config_id,
        "question": run.question,
        "instructionsSnapshot": run.instructions_snapshot,
        "toolBindingsSnapshot": copy.deepcopy(run.tool_bindings_snapshot or []),
        "maxIterations": run.max_iterations,
        "maxToolCalls": run.max_tool_calls,
        "maxCostUsd": float(run.max_cost_usd),
        "timeoutSeconds": run.timeout_seconds,
        "iterationCount": run.iteration_count,
        "toolCallCount": run.tool_call_count,
        "promptTokens": run.prompt_tokens,
        "completionTokens": run.completion_tokens,
        "totalTokens": run.total_tokens,
        "cost": float(run.cost),
        "costSource": run.cost_source,
        "costComplete": run.cost_complete,
        "finalAnswer": run.final_answer,
        "pauseCode": run.pause_code,
        "pauseDetail": copy.deepcopy(run.pause_detail),
        "errorCode": run.error_code,
        "errorMessage": run.error_message,
        "cancelRequested": run.cancel_requested,
        "revision": run.revision,
    }


def _isReadable(run: AgentOrchestrationRun, actor: CurrentUser) -> bool:
    """读 ACL：创建者本人或 admin。"""
    if run.created_by == actor.userId:
        return True
    return ADMIN_ROLE in tuple(actor.roles or ())


def _capLimit(limit: int) -> int:
    """分页上限（同时兜住负数与超限值）。"""
    return max(0, min(int(limit), MAX_PAGE_LIMIT))


def _requiredIncrementField(pauseDetail: dict | None) -> str | None:
    """BUDGET_EXHAUSTED 的 pause_detail → 必须提供的增量字段。"""
    if not isinstance(pauseDetail, dict):
        return None
    budget = pauseDetail.get("budget")
    if not isinstance(budget, str):
        return None
    return _BUDGET_INCREMENT_BY_BUDGET.get(budget.strip().lower())


def _hasAnyIncrement(dto: AgentRunResume) -> bool:
    return any(
        getattr(dto, field) is not None
        for field in _LIMIT_FIELD_BY_INCREMENT
    )
