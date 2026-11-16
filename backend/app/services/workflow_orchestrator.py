"""P5 编排器：把「当前步骤」翻译成业务命令（**写操作的来路**）。

W5 评审发现的 CRITICAL 缺口
---------------------------
``WorkflowStore.enqueueCommand`` 在**生产路径上没有任何调用方**（只被测试
调用）⇒ Worker 在生产永远领不到活儿 ⇒ G6 第一条「写操作全部经 Command」
名存实亡。

根因有两处（本模块一次修好）：

1. **步骤↔命令没有链接**：``WorkflowStepSpec`` 只有 code / order / requiresApproval
   ⇒ 步骤不知道自己要执行什么命令；
2. **没有驱动者**：没有任何东西把「当前步骤」推进成「待执行命令」
   ⇒ 提交后实例永远停在 RUNNING。

本模块就是那个缺失的**驱动者**（链接由 ``workflow_definition`` 补齐）。

四条纪律
--------

1. **一次只推进一个步骤**：按 ``step_order`` 找**第一个未完成**的步骤；它还在
   跑（RUNNING）就停下 ⇒ 步骤之间**串行**（并行编排不在契约内，也不在本期）。
2. **审批先行**：步骤要求审批 ⇒ 先有审批轮次；未决就停（等审批），通过后才
   入队命令；被拒 / 超时 ⇒ **绝不入队命令**（实例已被审批面收敛成
   REJECTED / FAILED）。
3. **快照为准**：运行时只认步骤行上的 ``input_json`` 编排快照，**不回头读定义**
   ——``workflow_definition`` 没有不可变触发器 ⇒ 定义一改，**在跑的实例**的行为
   不该跟着变。
4. **幂等**：命令幂等键 = ``wf-{instanceId}-step-{stepId}`` ⇒ 反复驱动只落
   一条命令（``uq_business_command_idempotency`` 是库级保证）。
5. **载荷命名空间化**：命令载荷固定是 ``{"request": 用户输入, "step": 步骤载荷}``
   ⇒ 用户输入**顶不掉**流程字段；用户输入里的**保留键**（适配器控制面）被剔除
   ⇒ 用户不能左右适配器行为（详见 ``workflow_command_ports``）。

**不 commit**：写操作由调用方（API 层 / Worker / 执行器）统一提交 ⇒ 可与
治理审计两阶段写同一事务。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError
from app.domain.workflow_models import WorkflowInstance, WorkflowStep
from app.services.workflow_approval_service import WorkflowApprovalService
from app.services.workflow_command_ports import buildCommandPayload
from app.services.workflow_definition import parseStepRuntimeSpec
from app.services.workflow_store import WorkflowStore

# 实例状态字面量（与 `workflow_state_machine` 的矩阵、库层 CHECK 一致）。
_STATUS_PENDING = "PENDING"
_STATUS_RUNNING = "RUNNING"
_STATUS_APPROVED = "APPROVED"
_STATUS_COMPLETED = "COMPLETED"
_STATUS_SUCCEEDED = "SUCCEEDED"
_STATUS_SKIPPED = "SKIPPED"

# 实例进入终态 ⇒ 编排器不再驱动（已取消 / 已拒绝 / 已补偿 / 已失败 / 已完成）。
_TERMINAL_INSTANCE_STATUSES = frozenset(
    {
        _STATUS_COMPLETED,
        "CANCELLED",
        "REJECTED",
        "COMPENSATED",
        "FAILED",
    }
)

# 步骤已完成（⇒ 轮到下一步）。
_DONE_STEP_STATUSES = frozenset({_STATUS_SUCCEEDED, _STATUS_SKIPPED})

# 实例允许置 COMPLETED 的来源状态（其它状态说明还在等审批 / 已终态）。
_INSTANCE_COMPLETABLE_STATUSES = frozenset({_STATUS_RUNNING, _STATUS_APPROVED})

# 审批轮次结论。
_APPROVAL_WAITING = "WAITING"
_APPROVAL_APPROVED = "APPROVED"
_APPROVAL_REJECTED = "REJECTED"
_APPROVAL_FAILED = "FAILED"


def commandIdempotencyKey(instanceId: int, stepId: int) -> str:
    """命令幂等键 = 实例 + 步骤（同一实例同一步只可能有一条命令）。

    库层唯一约束作用域是 ``(command_type, idempotency_key)``；键里带上实例与
    步骤 ⇒ 不同实例 / 不同步骤的同名命令互不干扰。
    """

    return f"wf-{instanceId}-step-{stepId}"


class WorkflowOrchestrator:
    """工作流编排器（驱动实例前进）。"""

    def __init__(
        self,
        store: WorkflowStore | None = None,
        approval: WorkflowApprovalService | None = None,
    ) -> None:
        self._store = store or WorkflowStore()
        self._approval = approval or WorkflowApprovalService()

    # ---------------------------------------------------------------- 驱动

    async def drive(self, session: AsyncSession, *, instanceId: int) -> WorkflowInstance:
        """把实例往前推一步（幂等：反复调用不会重复入队命令）。

        返回推进后的实例行。

        - 没有步骤 ⇒ 实例直接 COMPLETED（一步即完成，不吊在 RUNNING）；
        - 当前步骤的命令还在飞（RUNNING）⇒ 等 Worker 回话（**不抢跑**）；
        - 当前步骤要审批且未决 ⇒ 发起审批轮次后停下（等审批）；
        - 审批被拒 / 超时 ⇒ 不推进（实例已被审批面收敛）。
        """

        instance = await self._store.lockInstance(session, instanceId)
        if instance is None:
            raise NotFoundError(f"工作流实例 {instanceId} 不存在")
        if instance.status in _TERMINAL_INSTANCE_STATUSES:
            return instance

        if instance.status == _STATUS_PENDING:
            instance = await self._store.advanceInstance(
                session, instanceId, _STATUS_RUNNING
            )

        steps = await self._store.listSteps(session, instanceId=instanceId)
        if not steps:
            return await self._completeInstance(session, instance)

        for step in steps:
            if step.status in _DONE_STEP_STATUSES:
                continue
            if step.status != _STATUS_PENDING:
                # 当前步骤还在跑（命令在飞）或已失败 ⇒ 等 Worker / 等显式重试，
                # **不抢跑下一步**（步骤之间串行）。
                return instance

            instance, shouldContinue = await self._advanceStep(
                session, instance=instance, step=step
            )
            if not shouldContinue:
                return instance

        return await self._completeInstance(session, instance)

    # ---------------------------------------------------------------- 步骤

    async def _advanceStep(
        self,
        session: AsyncSession,
        *,
        instance: WorkflowInstance,
        step: WorkflowStep,
    ) -> tuple[WorkflowInstance, bool]:
        """推进当前步骤。返回 ``(实例, 是否继续往下走)``。"""

        spec = parseStepRuntimeSpec(step.input_json)

        if spec.requiresApproval:
            verdict = await self._approvalVerdict(session, instance=instance)
            if verdict != _APPROVAL_APPROVED:
                # 等审批 / 被拒 / 超时 ⇒ 都不入队命令（失败关闭）。
                return instance, False

        if spec.commandType is None:
            # 纯审批门：审批通过即该步完成 ⇒ 继续下一步。
            await self._completeStep(session, step)
            return instance, True

        instance = await self._enqueueStepCommand(
            session, instance=instance, step=step, commandType=spec.commandType, payload=spec.payload
        )
        # 命令已入队 ⇒ 等 Worker 执行完再推进下一步。
        return instance, False

    async def _completeStep(self, session: AsyncSession, step: WorkflowStep) -> None:
        """步骤 → SUCCEEDED（PENDING 先补一跳 RUNNING：状态机不允许多跳）。"""

        await self._store.advanceStep(session, step.id, _STATUS_RUNNING)
        await self._store.advanceStep(session, step.id, _STATUS_SUCCEEDED)

    async def _enqueueStepCommand(
        self,
        session: AsyncSession,
        *,
        instance: WorkflowInstance,
        step: WorkflowStep,
        commandType: str,
        payload: dict | None,
    ) -> WorkflowInstance:
        """为当前步骤入队业务命令（幂等），并把步骤 / 实例推进到「运行中」。

        ⚠️ 载荷**命名空间化**（``buildCommandPayload``）：用户输入进 ``request``、
        步骤载荷进 ``step`` ⇒ 用户输入**顶不掉**流程字段；用户输入里的**保留键**
        （适配器控制面）被剔除 ⇒ 用户不能左右适配器行为。
        """

        commandPayload = buildCommandPayload(
            requestJson=instance.request_json or {},
            stepPayload=payload or {},
        )
        await self._store.enqueueCommand(
            session,
            instanceId=instance.id,
            stepId=step.id,
            commandType=commandType,
            idempotencyKey=commandIdempotencyKey(instance.id, step.id),
            payload=commandPayload,
        )
        await self._store.advanceStep(session, step.id, _STATUS_RUNNING)

        if instance.status == _STATUS_APPROVED:
            # 审批通过 ⇒ 实例从「已批准」回到「运行中」（命令在飞）。
            instance = await self._store.advanceInstance(
                session, instance.id, _STATUS_RUNNING
            )
        return instance

    # ---------------------------------------------------------------- 审批

    async def _approvalVerdict(
        self, session: AsyncSession, *, instance: WorkflowInstance
    ) -> str:
        """本步骤的审批结论（``APPROVED`` / ``WAITING`` / ``REJECTED`` / ``FAILED``）。

        没有审批轮次 ⇒ **发起一轮**（失败关闭：矩阵未覆盖 ⇒ 抛 409，不放行）。
        """

        tasks = await self._store.listApprovalTasks(session, instanceId=instance.id)
        if not tasks:
            await self._approval.requestApproval(
                session,
                instanceId=instance.id,
                actor=instance.created_by or "",
                roles=(),
            )
            return _APPROVAL_WAITING

        statuses = {task.status for task in tasks}
        if _STATUS_PENDING in statuses:
            return _APPROVAL_WAITING
        if "REJECTED" in statuses:
            return _APPROVAL_REJECTED
        if "TIMEOUT" in statuses:
            return _APPROVAL_FAILED
        if statuses == {_STATUS_APPROVED}:
            return _APPROVAL_APPROVED
        # 其它组合（如 CANCELLED）⇒ 失败关闭：不推进、不入队命令。
        return _APPROVAL_WAITING

    # ---------------------------------------------------------------- 收尾

    async def _completeInstance(
        self, session: AsyncSession, instance: WorkflowInstance
    ) -> WorkflowInstance:
        """全部步骤完成 ⇒ 实例 COMPLETED。"""

        if instance.status in _INSTANCE_COMPLETABLE_STATUSES:
            return await self._store.advanceInstance(
                session, instance.id, _STATUS_COMPLETED
            )
        return instance
