"""P5 审批运行时：任务生成 / 审批决策 / 超时清扫。

七道守卫（全部在**写库前**判定 ⇒ 拒绝即零副作用）：

| # | 守卫 | 判定依据 | 失败 |
|---|---|---|---|
| ① | 对象级可见 | 实例创建人 / admin / 该实例审批任务的持有人 | 404（无存在性预言机） |
| ② | 越权 | **审批矩阵条目**（指定人优先，其次角色） | 403 |
| ③ | 四眼 | 决策人 ≠ 实例创建人 **且** ≠ 本轮发起人 | 403 |
| ④ | 重复审批 | 任务状态必须是 ``PENDING`` | 409 |
| ⑤ | 超时 | ``now > due_at`` ⇒ 置 TIMEOUT + 实例 FAILED | 409 |
| ⑥ | 单轮 | 一个实例只允许一轮审批（不叠加） | 409 |
| ⑦ | 审批人可解析 | 角色 / 指定人在库中存在（且启用） | 409 |

守卫顺序 = **先鉴权后状态**：未授权者不该从错误码里读出「这条审批批过没有」。

**风险级别由服务端绑定**：来自定义体顶层 ``riskLevel``（**客户端不得自选** ——
自选风险级别 = 自选审批层级 = 挑低风险绕过高层审批）⇒ 矩阵未覆盖 ⇒
**失败关闭**（拒绝发起审批），绝不「匹配不到就放行」。

**「谁有资格批」的权威 = 审批矩阵**（定义体）⇒ 任务行只快照角色；
任务行的 ``approver_user`` 是**实际审批人（签署人）**，待审批时为空。

本服务**不 commit**（写操作由 API 层提交 ⇒ 可与审计两阶段写同一事务）；
唯一例外是**超时**：超时置 TIMEOUT 是**状态推进**，必须在抛 409 之前落库
（否则实例永远卡在「等待审批」，而清扫器并不总在跑）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.domain.workflow_models import (
    ApprovalTask,
    WorkflowDefinition,
    WorkflowInstance,
)
from app.models.rbac import ADMIN_ROLE_CODE, Role, User
from app.services.actor_departments import DepartmentsInput
from app.services.messages_zh import (
    MSG_WORKFLOW_APPROVAL_ALREADY_DECIDED,
    MSG_WORKFLOW_APPROVAL_APPROVER_UNKNOWN,
    MSG_WORKFLOW_APPROVAL_MATRIX_NOT_MATCHED,
    MSG_WORKFLOW_APPROVAL_MATRIX_UNAVAILABLE,
    MSG_WORKFLOW_APPROVAL_NOT_AUTHORIZED,
    MSG_WORKFLOW_APPROVAL_RISK_LEVEL_NOT_DEFINED,
    MSG_WORKFLOW_APPROVAL_ROUND_EXISTS,
    MSG_WORKFLOW_APPROVAL_TIMEOUT,
)
from app.services.workflow_audit_service import WorkflowAuditService
from app.services.workflow_definition import (
    ApprovalMatrixEntry,
    matrixEntryByCode,
    parseApprovalMatrix,
    parseDefinitionRiskLevel,
    selectMatrixEntries,
)
from app.services.workflow_state_machine import assertWorkflowTransition
from app.services.workflow_store import WorkflowStore

DETAIL_APPROVAL_ALREADY_DECIDED = "APPROVAL_ALREADY_DECIDED"
DETAIL_APPROVAL_TIMEOUT = "APPROVAL_TIMEOUT"
DETAIL_APPROVAL_NOT_AUTHORIZED = "APPROVAL_NOT_AUTHORIZED"
DETAIL_APPROVAL_SELF_FORBIDDEN = "APPROVAL_SELF_FORBIDDEN"
DETAIL_APPROVAL_MATRIX_NOT_MATCHED = "APPROVAL_MATRIX_NOT_MATCHED"
DETAIL_APPROVAL_MATRIX_UNAVAILABLE = "APPROVAL_MATRIX_UNAVAILABLE"
DETAIL_APPROVAL_ROUND_EXISTS = "APPROVAL_ROUND_ALREADY_EXISTS"
DETAIL_APPROVAL_APPROVER_UNKNOWN = "APPROVAL_APPROVER_UNKNOWN"
DETAIL_APPROVAL_RISK_LEVEL_NOT_DEFINED = "APPROVAL_RISK_LEVEL_NOT_DEFINED"

# 审批对象类型（P5 内部用工作流实例；WP-ONT-AI A4 复用同一张表时换这里的值）。
SUBJECT_TYPE_WORKFLOW_INSTANCE = "workflow_instance"

# 决策枚举。
DECISION_APPROVE = "APPROVE"
DECISION_REJECT = "REJECT"

_STATUS_PENDING = "PENDING"
_STATUS_APPROVED = "APPROVED"
_STATUS_REJECTED = "REJECTED"
_STATUS_TIMED_OUT = "TIMEOUT"

# 实例在审批全部通过 / 有拒绝 / 有超时后的目标状态。
_INSTANCE_APPROVED = "APPROVED"
_INSTANCE_REJECTED = "REJECTED"
_INSTANCE_FAILED = "FAILED"
_INSTANCE_WAITING_APPROVAL = "WAITING_APPROVAL"

# 超时是**系统事件**（时钟 / 清扫器），不是某个人的决定 ⇒ 审计 actor 用系统标识。
SYSTEM_ACTOR_TIMEOUT = "system:approval-timeout"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WorkflowApprovalService:
    """审批面运行时。"""

    def __init__(
        self,
        store: WorkflowStore | None = None,
        audit: WorkflowAuditService | None = None,
    ) -> None:
        self._store = store or WorkflowStore()
        self._audit = audit or WorkflowAuditService()

    # ---------------------------------------------------------------- 生成

    async def requestApproval(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        actor: str,
        roles: Sequence[str] = (),
        subjectType: str = SUBJECT_TYPE_WORKFLOW_INSTANCE,
        actorDepartments: DepartmentsInput = None,
    ) -> list[ApprovalTask]:
        """为实例发起一轮审批（**失败关闭**：矩阵未覆盖 ⇒ 拒绝，不落任何行）。

        幂等：同一 ``(instance_id, matrix_code)`` 只落一条任务
        （``uq_approval_task_matrix``）⇒ 重复发起不产生第二份审批。

        **风险级别不由调用方传入** —— 由定义体顶层 ``riskLevel`` 决定
        （服务端绑定 ⇒ 客户端不能挑低风险绕过高层审批）。
        """

        instance, definition = await self._loadInstanceAndDefinition(session, instanceId)
        await self._assertInstanceVisible(session, instance=instance, actor=actor, roles=roles)

        entries = self._selectEntries(definition=definition, subjectType=subjectType)
        await self._assertApproversResolvable(session, entries)

        existing = await self._store.listApprovalTasks(session, instanceId=instance.id)
        replay = self._matchReplay(existing, entries)
        if replay is not None:
            return replay
        if existing:
            raise ConflictError(
                MSG_WORKFLOW_APPROVAL_ROUND_EXISTS.format(instanceId=instance.id),
                detail=DETAIL_APPROVAL_ROUND_EXISTS,
                details={"instanceId": instance.id, "taskCount": len(existing)},
            )

        # 实例转入「等待审批」；同态是 no-op（幂等重放）
        await self._store.advanceInstance(
            session,
            instance.id,
            _INSTANCE_WAITING_APPROVAL,
        )

        now = _utcnow()
        tasks: list[ApprovalTask] = []
        for entry in entries:
            task, _created = await self._store.createApprovalTask(
                session,
                instanceId=instance.id,
                subjectType=entry.subjectType,
                subjectId=str(instance.id),
                matrixCode=entry.matrixCode,
                approverRole=entry.approverRole,
                approverUser=None,
                dueAt=now + timedelta(hours=entry.timeoutHours),
                createdBy=actor,
            )
            await self._audit.recordApprovalRequested(
                session,
                task=task,
                actor=actor,
                actorDepartments=actorDepartments,
            )
            tasks.append(task)
        return tasks

    # ---------------------------------------------------------------- 决策

    async def decideApproval(
        self,
        session: AsyncSession,
        *,
        taskId: int,
        decision: str,
        actor: str,
        roles: Sequence[str] = (),
        note: str | None = None,
        actorDepartments: DepartmentsInput = None,
    ) -> ApprovalTask:
        """审批决策（APPROVE / REJECT）。七守卫见模块文档。"""

        snapshot = await self._store.getApprovalTask(session, taskId)
        if snapshot is None:
            raise NotFoundError(f"审批任务 {taskId} 不存在")

        # 锁顺序恒为 instance → task（多处并发 ⇒ 固定顺序杜绝死锁）
        instance = await self._lockInstance(session, snapshot.instance_id)
        task = await self._lockTask(session, taskId)

        definition = await self._loadDefinition(session, instance.definition_id)
        entry = matrixEntryByCode(
            parseApprovalMatrix(definition.definition_json),
            task.matrix_code,
        )

        # ① 越权（含矩阵失效 ⇒ 失败关闭）
        self._assertAuthorized(task=task, entry=entry, instance=instance, actor=actor, roles=roles)

        # ② 重复审批
        if task.status != _STATUS_PENDING:
            raise self._alreadyDecidedError(task)

        # ③ 超时（顺带把任务落成 TIMEOUT ⇒ 实例不再死等）
        now = _utcnow()
        if task.due_at is not None and now > task.due_at:
            await self._markTimedOut(session, task=task, instance=instance, now=now)
            await session.commit()
            raise ConflictError(
                MSG_WORKFLOW_APPROVAL_TIMEOUT.format(taskId=task.id),
                detail=DETAIL_APPROVAL_TIMEOUT,
                details={"taskId": task.id},
            )

        targetStatus = self._targetStatus(decision)
        beforeStatus = task.status
        updated = await self._store.decideApprovalTask(
            session,
            task.id,
            fromStatus=task.status,
            toStatus=targetStatus,
            actor=actor,
            note=note,
            decidedAt=now,
        )

        await self._audit.recordDecision(
            session,
            task=updated,
            decision=decision,
            actor=actor,
            beforeStatus=beforeStatus,
            note=note,
            actorDepartments=actorDepartments,
        )
        await self._syncInstanceStatus(session, instance)
        return updated

    # ---------------------------------------------------------------- 清扫

    async def sweepTimedOutApprovals(self, session: AsyncSession, *, now: datetime | None = None) -> int:
        """把超期未决的审批标记为 TIMEOUT，返回处理条数。

        过期任务留在 ``PENDING`` 会让实例永远卡在「等待审批」⇒ 必须有清扫。
        **不 commit** —— 由调用方（Worker）统一提交。
        """

        current = now or _utcnow()
        overdue = (
            (
                await session.execute(
                    select(ApprovalTask).where(
                        ApprovalTask.status == _STATUS_PENDING,
                        ApprovalTask.due_at.is_not(None),
                        ApprovalTask.due_at < current,
                    )
                )
            )
            .scalars()
            .all()
        )
        touched = 0
        for task in overdue:
            instance = await self._lockInstance(session, task.instance_id)
            await self._markTimedOut(session, task=task, instance=instance, now=current)
            touched += 1
        return touched

    # ---------------------------------------------------------------- 查询

    async def getInstance(self, session: AsyncSession, instanceId: int) -> WorkflowInstance | None:
        return await self._store.getInstance(session, instanceId)

    async def assertInstanceVisible(
        self,
        session: AsyncSession,
        instanceId: int,
        *,
        actor: str,
        roles: Sequence[str] = (),
    ) -> WorkflowInstance:
        """按**对象级授权**取实例；无权 ⇒ 404（不泄露「存在但无权」）。"""

        instance = await self._store.getInstance(session, instanceId)
        if instance is None:
            raise NotFoundError(f"工作流实例 {instanceId} 不存在")
        await self._assertInstanceVisible(session, instance=instance, actor=actor, roles=roles)
        return instance

    async def listApprovals(self, session: AsyncSession, *, instanceId: int) -> list[ApprovalTask]:
        rows = await self._store.listApprovalTasks(session, instanceId=instanceId)
        return rows

    async def listVisibleApprovals(
        self,
        session: AsyncSession,
        instanceId: int,
        *,
        actor: str,
        roles: Sequence[str] = (),
    ) -> list[ApprovalTask]:
        """对象级授权后的审批任务列表；实例不存在 / 无权 ⇒ 404。"""

        await self.assertInstanceVisible(session, instanceId, actor=actor, roles=roles)
        return await self._store.listApprovalTasks(session, instanceId=instanceId)

    # ---------------------------------------------------------------- 内部

    async def _loadInstanceAndDefinition(
        self, session: AsyncSession, instanceId: int
    ) -> tuple[WorkflowInstance, WorkflowDefinition]:
        instance = await self._store.getInstance(session, instanceId)
        if instance is None:
            raise NotFoundError(f"工作流实例 {instanceId} 不存在")
        definition = await self._loadDefinition(session, instance.definition_id)
        return instance, definition

    async def _loadDefinition(
        self, session: AsyncSession, definitionId: int
    ) -> WorkflowDefinition:
        definition = await session.get(WorkflowDefinition, definitionId)
        if definition is None:
            raise NotFoundError(f"工作流定义 {definitionId} 不存在")
        return definition

    def _selectEntries(
        self, *, definition: WorkflowDefinition, subjectType: str
    ) -> tuple[ApprovalMatrixEntry, ...]:
        """按（服务端绑定的风险级别）选矩阵条目；选不中 ⇒ 失败关闭。"""

        riskLevel = parseDefinitionRiskLevel(definition.definition_json)
        if riskLevel is None:
            raise ConflictError(
                MSG_WORKFLOW_APPROVAL_RISK_LEVEL_NOT_DEFINED.format(
                    definitionId=definition.id
                ),
                detail=DETAIL_APPROVAL_RISK_LEVEL_NOT_DEFINED,
                details={"definitionId": definition.id},
            )
        entries = selectMatrixEntries(
            parseApprovalMatrix(definition.definition_json),
            subjectType=subjectType,
            capabilityCode=definition.capability_code,
            riskLevel=riskLevel,
        )
        if not entries:
            raise ConflictError(
                MSG_WORKFLOW_APPROVAL_MATRIX_NOT_MATCHED.format(
                    subjectType=subjectType,
                    capabilityCode=definition.capability_code,
                    riskLevel=riskLevel,
                ),
                detail=DETAIL_APPROVAL_MATRIX_NOT_MATCHED,
                details={
                    "subjectType": subjectType,
                    "capabilityCode": definition.capability_code,
                    "riskLevel": riskLevel,
                },
            )
        return entries

    @staticmethod
    def _matchReplay(
        existing: Sequence[ApprovalTask], entries: Sequence[ApprovalMatrixEntry]
    ) -> list[ApprovalTask] | None:
        """同一轮重复发起 ⇒ 返回既有任务（幂等）；否则 None（交由调用方判冲突）。"""

        if not existing:
            return None
        existingCodes = {task.matrix_code for task in existing}
        wantedCodes = {entry.matrixCode for entry in entries}
        if existingCodes != wantedCodes:
            return None
        if any(task.status != _STATUS_PENDING for task in existing):
            return None
        return list(existing)

    async def _assertApproversResolvable(
        self, session: AsyncSession, entries: Sequence[ApprovalMatrixEntry]
    ) -> None:
        """审批人必须在库中存在（否则生成**谁也批不了**的任务 = 死等）。

        角色：``roles.code`` 存在；指定人：``users.username`` 存在且启用。
        """

        for entry in entries:
            if entry.approverRole:
                roleId = (
                    await session.execute(select(Role.id).where(Role.code == entry.approverRole))
                ).scalar_one_or_none()
                if roleId is None:
                    raise ConflictError(
                        MSG_WORKFLOW_APPROVAL_APPROVER_UNKNOWN.format(
                            matrixCode=entry.matrixCode,
                            approver=entry.approverRole,
                            kind="角色",
                        ),
                        detail=DETAIL_APPROVAL_APPROVER_UNKNOWN,
                        details={
                            "matrixCode": entry.matrixCode,
                            "approver": entry.approverRole,
                            "kind": "role",
                        },
                    )
            if entry.approverUser:
                userId = (
                    await session.execute(
                        select(User.id).where(
                            User.username == entry.approverUser,
                            User.enabled.is_(True),
                        )
                    )
                ).scalar_one_or_none()
                if userId is None:
                    raise ConflictError(
                        MSG_WORKFLOW_APPROVAL_APPROVER_UNKNOWN.format(
                            matrixCode=entry.matrixCode,
                            approver=entry.approverUser,
                            kind="用户",
                        ),
                        detail=DETAIL_APPROVAL_APPROVER_UNKNOWN,
                        details={
                            "matrixCode": entry.matrixCode,
                            "approver": entry.approverUser,
                            "kind": "user",
                        },
                    )

    async def _assertInstanceVisible(
        self,
        session: AsyncSession,
        *,
        instance: WorkflowInstance,
        actor: str,
        roles: Sequence[str],
    ) -> None:
        """对象级授权：无权 ⇒ 404（**不泄露存在性**）。"""

        if instance.created_by and instance.created_by == actor:
            return
        if ADMIN_ROLE_CODE in (roles or ()):
            return
        if await self._isApprovalHolder(session, instance=instance, actor=actor, roles=roles):
            return
        raise NotFoundError(f"工作流实例 {instance.id} 不存在")

    async def _isApprovalHolder(
        self,
        session: AsyncSession,
        *,
        instance: WorkflowInstance,
        actor: str,
        roles: Sequence[str],
    ) -> bool:
        """该 actor 是否持有本实例的审批任务（指定人 / 角色 / 已签署）。"""

        tasks = await self._store.listApprovalTasks(session, instanceId=instance.id)
        if not tasks:
            return False
        definition = await self._loadDefinition(session, instance.definition_id)
        matrixByCode = {
            entry.matrixCode: entry
            for entry in parseApprovalMatrix(definition.definition_json)
        }
        for task in tasks:
            if task.approver_user == actor:
                return True
            entry = matrixByCode.get(task.matrix_code)
            if entry is None:
                continue
            if entry.approverUser:
                if entry.approverUser == actor:
                    return True
            elif entry.approverRole and entry.approverRole in (roles or ()):
                return True
            if task.approver_role and task.approver_role in (roles or ()):
                return True
        return False

    async def _lockTask(self, session: AsyncSession, taskId: int) -> ApprovalTask:
        task = (
            await session.execute(
                select(ApprovalTask)
                .where(ApprovalTask.id == taskId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is None:
            raise NotFoundError(f"审批任务 {taskId} 不存在")
        return task

    async def _lockInstance(self, session: AsyncSession, instanceId: int) -> WorkflowInstance:
        instance = (
            await session.execute(
                select(WorkflowInstance)
                .where(WorkflowInstance.id == instanceId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if instance is None:
            raise NotFoundError(f"工作流实例 {instanceId} 不存在")
        return instance

    @staticmethod
    def _assertAuthorized(
        *,
        task: ApprovalTask,
        entry: ApprovalMatrixEntry | None,
        instance: WorkflowInstance,
        actor: str,
        roles: Sequence[str],
    ) -> None:
        """越权判定（403）。判据 = **审批矩阵条目**（不是任务行的可变列）。"""

        if entry is None:
            # 矩阵条目查不到 ⇒ 失败关闭（不猜、不放行）
            raise ConflictError(
                MSG_WORKFLOW_APPROVAL_MATRIX_UNAVAILABLE.format(
                    matrixCode=task.matrix_code
                ),
                detail=DETAIL_APPROVAL_MATRIX_UNAVAILABLE,
                details={"taskId": task.id, "matrixCode": task.matrix_code},
            )

        if entry.approverUser:
            allowed = entry.approverUser == actor
        else:
            allowed = bool(entry.approverRole) and entry.approverRole in (roles or ())

        if not allowed:
            # ⚠️ details 不带 approverRole / approverUser —— 不泄露审批规则
            raise PermissionDeniedError(
                MSG_WORKFLOW_APPROVAL_NOT_AUTHORIZED.format(taskId=task.id),
                detail=DETAIL_APPROVAL_NOT_AUTHORIZED,
                details={"taskId": task.id},
            )

        # 四眼原则：决策人不得是「实例提交人」或「本轮审批发起人」
        if (instance.created_by and instance.created_by == actor) or (
            task.created_by and task.created_by == actor
        ):
            raise PermissionDeniedError(
                MSG_WORKFLOW_APPROVAL_NOT_AUTHORIZED.format(taskId=task.id),
                detail=DETAIL_APPROVAL_SELF_FORBIDDEN,
                details={"taskId": task.id, "reason": "SELF_APPROVAL_FORBIDDEN"},
            )

    @staticmethod
    def _alreadyDecidedError(task: ApprovalTask) -> ConflictError:
        if task.status == _STATUS_TIMED_OUT:
            return ConflictError(
                MSG_WORKFLOW_APPROVAL_TIMEOUT.format(taskId=task.id),
                detail=DETAIL_APPROVAL_TIMEOUT,
                details={"taskId": task.id},
            )
        return ConflictError(
            MSG_WORKFLOW_APPROVAL_ALREADY_DECIDED.format(taskId=task.id, status=task.status),
            detail=DETAIL_APPROVAL_ALREADY_DECIDED,
            details={"taskId": task.id, "status": task.status},
        )

    @staticmethod
    def _targetStatus(decision: str) -> str:
        if decision == DECISION_APPROVE:
            return _STATUS_APPROVED
        if decision == DECISION_REJECT:
            return _STATUS_REJECTED
        raise ConflictError(
            f"未知的审批决策：{decision}（允许 APPROVE / REJECT）",
            detail="APPROVAL_DECISION_INVALID",
        )

    async def _markTimedOut(
        self,
        session: AsyncSession,
        *,
        task: ApprovalTask,
        instance: WorkflowInstance,
        now: datetime,
    ) -> None:
        """超时 ⇒ 任务 TIMEOUT，实例不再死等（FAILED）。"""

        assertWorkflowTransition("approval", task.status, _STATUS_TIMED_OUT)
        updated = await self._store.markApprovalTask(
            session,
            task.id,
            fromStatus=task.status,
            toStatus=_STATUS_TIMED_OUT,
            decidedAt=now,
        )
        await self._audit.recordTimeout(
            session,
            task=updated,
            actor=SYSTEM_ACTOR_TIMEOUT,
        )
        await self._syncInstanceStatus(session, instance)

    async def _syncInstanceStatus(self, session: AsyncSession, instance: WorkflowInstance) -> None:
        """把实例状态与本轮审批结果对齐（只处理「等待审批」态）。"""

        if instance.status != _INSTANCE_WAITING_APPROVAL:
            return
        tasks = await self._store.listApprovalTasks(session, instanceId=instance.id)
        if not tasks:
            return
        statuses = {task.status for task in tasks}
        if _STATUS_REJECTED in statuses:
            target = _INSTANCE_REJECTED
        elif _STATUS_TIMED_OUT in statuses:
            target = _INSTANCE_FAILED
        elif statuses == {_STATUS_APPROVED}:
            target = _INSTANCE_APPROVED
        else:
            return
        await self._store.advanceInstance(session, instance.id, target)
