"""P5 工作流实例生命周期：提交 / 详情 / 取消 / 审批决策后的推进。

**提交是幂等的**：同一 ``(definition_id, created_by, idempotency_key)`` 重复提交
只落一行，第二次返回既有实例（``created=False``）—— 幂等重放不是错误。

**幂等键按提交人私有**：别人用同一个 ``idempotencyKey`` ⇒ 各自独立实例（不是
409）⇒ ① 拿不到别人的实例（无越权读）；② 也不泄露「这个键存在」（无存在性
预言机）；③ 别人的键不会被占死（无 DoS）。

步骤来自定义的 ``definition_json.steps``（**数据驱动**）⇒ 改编排不改代码。
提交时把每一步的**编排快照**（``commandType`` / ``requiresApproval`` /
``payload``）写进 ``workflow_step.input_json`` ⇒ 定义事后被改也不影响在跑的
实例。

**提交即入队**：提交后交给编排器驱动 ⇒ 当前步骤的命令**立刻**入队
（否则 Worker 在生产永远领不到活儿 ⇒ G6「写操作全部经 Command」落空）。

**审批决策 = 两段事务**（``decideApprovalAndAdvance``）：决策先落库（**决策是
事实**），推进在独立事务里做（推进失败**不得**回滚已发生的审批）⇒ 推进失败时
实例**失败关闭**（``FAILED`` + 原因）。

**不 commit**：写操作由 API 层统一提交（⇒ 可与审计两阶段写同一事务）。
唯一例外是审批决策链（见上）：它自己分段提交。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

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
    WorkflowStep,
)
from app.models.rbac import ADMIN_ROLE_CODE
from app.services.actor_departments import DepartmentsInput
from app.services.messages_zh import (
    MSG_WORKFLOW_CANCEL_BUSY,
    MSG_WORKFLOW_CANCEL_NOT_ALLOWED,
    MSG_WORKFLOW_CANCEL_REAL_AUTH_REQUIRED,
    MSG_WORKFLOW_DEFINITION_AMBIGUOUS,
    MSG_WORKFLOW_DEFINITION_NOT_FOUND,
    MSG_WORKFLOW_DEFINITION_NOT_PUBLISHED,
    MSG_WORKFLOW_DEFINITION_VERSION_MISMATCH,
    MSG_WORKFLOW_DRIVE_FAILED,
)
from app.services.workflow_approval_service import WorkflowApprovalService
from app.services.workflow_audit_service import WorkflowAuditService
from app.services.workflow_definition import WorkflowStepSpec, parseWorkflowSteps
from app.services.workflow_orchestrator import WorkflowOrchestrator
from app.services.workflow_state_machine import STOPPED_INSTANCE_STATUSES
from app.services.workflow_store import (
    EVENT_APPROVAL_CANCELLED,
    WorkflowStore,
)

logger = logging.getLogger(__name__)

_STATUS_PUBLISHED = "PUBLISHED"
_STATUS_CANCELLED = "CANCELLED"
_STATUS_EXECUTING = "EXECUTING"
_STATUS_FAILED = "FAILED"

# 实例终态（不可再取消 / 不可再被失败关闭覆盖）。
# ⚠️ **单一出口**：终态集合只写在 ``workflow_state_machine``（状态机模块）里，
#    服务层 / 编排器 / 存储层都从这里取 ⇒ 不再各写一份、越写越歪。

# 错误细节码（前端可据此分支，不依赖文案）。
DETAIL_WORKFLOW_CANCEL_NOT_AUTHORIZED = "WORKFLOW_CANCEL_NOT_AUTHORIZED"
DETAIL_WORKFLOW_CANCEL_NOT_ALLOWED = "WORKFLOW_CANCEL_NOT_ALLOWED"
DETAIL_WORKFLOW_CANCEL_BUSY = "WORKFLOW_CANCEL_BUSY"
DETAIL_WORKFLOW_CANCEL_REAL_AUTH_REQUIRED = "WORKFLOW_CANCEL_REAL_AUTH_REQUIRED"
DETAIL_WORKFLOW_DEFINITION_NOT_PUBLISHED = "WORKFLOW_DEFINITION_NOT_PUBLISHED"
DETAIL_WORKFLOW_DEFINITION_VERSION_MISMATCH = "WORKFLOW_DEFINITION_VERSION_MISMATCH"
DETAIL_WORKFLOW_DEFINITION_AMBIGUOUS = "WORKFLOW_DEFINITION_AMBIGUOUS"


class WorkflowService:
    """工作流实例的提交、查询与取消。"""

    def __init__(
        self,
        store: WorkflowStore | None = None,
        audit: WorkflowAuditService | None = None,
        orchestrator: WorkflowOrchestrator | None = None,
        approval: WorkflowApprovalService | None = None,
    ) -> None:
        self._store = store or WorkflowStore()
        self._audit = audit or WorkflowAuditService()
        self._orchestrator = orchestrator or WorkflowOrchestrator(store=self._store)
        self._approval = approval or WorkflowApprovalService()

    async def submitInstance(
        self,
        session: AsyncSession,
        *,
        domainCode: str,
        capabilityCode: str,
        version: str,
        idempotencyKey: str,
        requestJson: dict[str, Any],
        actor: str,
        actorDepartments: DepartmentsInput = None,
    ) -> tuple[WorkflowInstance, bool]:
        """提交一个工作流实例。返回 ``(实例, 是否本次新建)``。

        定义不存在 ⇒ 404；定义未发布 ⇒ 409（**不发版就不能跑**）；
        定义的步骤编排非法 ⇒ 422（失败关闭，**零副作用**）。
        """

        definition = await self._resolveDefinition(
            session, domainCode=domainCode, capabilityCode=capabilityCode, version=version
        )

        # 先解析编排（定义写错 ⇒ 提交前就拒，不落任何行）
        specs = parseWorkflowSteps(definition.definition_json)

        instance, created = await self._createInstanceWithSteps(
            session,
            definition=definition,
            specs=specs,
            idempotencyKey=idempotencyKey,
            requestJson=requestJson,
            actor=actor,
            actorDepartments=actorDepartments,
        )
        if not created:
            # 幂等重放：既有实例原样返回（**不重建步骤、不重置状态**）
            return instance, False

        # 驱动：入队当前步骤的命令 / 发起审批 / 收尾（幂等，可反复调用）
        await self._orchestrator.drive(session, instanceId=instance.id)
        return instance, True

    async def _createInstanceWithSteps(
        self,
        session: AsyncSession,
        *,
        definition: WorkflowDefinition,
        specs: Sequence[WorkflowStepSpec],
        idempotencyKey: str,
        requestJson: dict[str, Any],
        actor: str,
        actorDepartments: DepartmentsInput = None,
    ) -> tuple[WorkflowInstance, bool]:
        """建实例 + 留提交审计 + 建步骤行（幂等重放**不重复留痕**）。"""

        instance, created = await self._store.createInstance(
            session,
            definitionId=definition.id,
            idempotencyKey=idempotencyKey,
            requestJson=requestJson,
            actor=actor,
        )
        if not created:
            # ⚠️ 幂等作用域**按提交人隔离**（库层约束含 ``created_by``）⇒ 别人用
            #    同一个键只会拿到**自己的**实例 ⇒ 不存在「拿到别人的实例」的路径，
            #    也不需要「键被他人占用」的 409（那等于泄露键空间的存在性）。
            return instance, False

        await self._audit.recordSubmit(
            session,
            instance=instance,
            actor=actor,
            actorDepartments=actorDepartments,
        )

        # ⚠️ 解析已保证**至少一步**（缺步骤 ⇒ 提交前就 422，零副作用）
        # ⇒ 这里无条件建步骤行（不再有「零步骤实例」这种假成功形态）。
        await self._store.createSteps(
            session,
            instanceId=instance.id,
            steps=[
                {
                    "code": spec.code,
                    "order": spec.order,
                    "input": _stepSnapshot(spec),
                }
                for spec in specs
            ],
        )
        return instance, True

    async def cancelInstance(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        actor: str,
        roles: Sequence[str] = (),
        dbUserId: int | None = None,
        isRealIdentity: bool = False,
        reason: str | None = None,
        actorDepartments: DepartmentsInput = None,
    ) -> WorkflowInstance:
        """取消工作流实例（**提交人或真实 admin**）。

        取消 = 之后**不再发起新的**外部写：待审批任务 → CANCELLED；待执行命令 →
        CANCELLED（**不再被 Worker 领取**）；未开始的步骤 → SKIPPED。

        ⚠️ **命令在飞 ⇒ 失败关闭（409）**：外部写可能已经发生 ⇒ 此时把实例判成
        CANCELLED 就是**治理面承诺与实际行为不符** ⇒ 让调用方稍后重试（幂等）。
        ⚠️ **已在飞的写无法撤回**（外部系统写入不可回滚）⇒ 只能靠补偿钩子。
        """

        instance, alreadyCancelled = await self._resolveCancellable(
            session,
            instanceId=instanceId,
            actor=actor,
            roles=roles,
            dbUserId=dbUserId,
            isRealIdentity=isRealIdentity,
        )
        if alreadyCancelled:
            # 幂等：重复取消不报错（也不重复留痕）。
            return instance

        beforeStatus = instance.status
        instance = await self._applyCancellation(session, instanceId=instanceId, reason=reason)
        await self._recordCancelAudit(
            session,
            instance=instance,
            actor=actor,
            beforeStatus=beforeStatus,
            reason=reason,
            actorDepartments=actorDepartments,
        )
        return instance

    async def _resolveCancellable(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        actor: str,
        roles: Sequence[str],
        dbUserId: int | None,
        isRealIdentity: bool,
    ) -> tuple[WorkflowInstance, bool]:
        """按「身份 → 归属 → 在飞命令 → 终态」顺序判定可否取消。

        返回 ``(实例, 是否已取消)``（已取消 ⇒ 幂等短路，**不重复留痕**）。
        """

        # 0) 身份闸：**先于存在性判定** ⇒ 未校验身份者对任何实例 id 都得同一个
        #    403（不构成存在性预言机）。
        self._assertRealIdentity(instanceId=instanceId, isRealIdentity=isRealIdentity)

        # 1) 无锁读：存在性 + 归属（``created_by`` 不会变 ⇒ 无需加锁）
        snapshot = await self._store.getInstance(session, instanceId)
        if snapshot is None:
            raise NotFoundError(f"工作流实例 {instanceId} 不存在")

        self._assertCanCancel(
            instance=snapshot, actor=actor, roles=roles, dbUserId=dbUserId
        )

        # 2) 锁命令（与 Worker 的 claim **互斥**）⇒ 看得到在飞的命令。
        #    ⚠️ 锁序：命令 → 实例（与执行器一致）⇒ 不会 40P01。
        await self._assertNoExecutingCommand(session, instanceId=instanceId)

        # 3) 锁实例（重读）⇒ 终态判定用**最新**值（等锁期间实例可能已推进）
        instance = await self._store.lockInstance(session, instanceId)
        if instance is None:
            raise NotFoundError(f"工作流实例 {instanceId} 不存在")

        if instance.status == _STATUS_CANCELLED:
            return instance, True
        self._assertCancellable(instance=instance, instanceId=instanceId)
        return instance, False

    @staticmethod
    def _assertCancellable(*, instance: WorkflowInstance, instanceId: int) -> None:
        """终态实例不可取消（409）—— 真实结局不被「取消」覆盖。"""

        if instance.status in STOPPED_INSTANCE_STATUSES:
            raise ConflictError(
                MSG_WORKFLOW_CANCEL_NOT_ALLOWED.format(status=instance.status),
                detail=DETAIL_WORKFLOW_CANCEL_NOT_ALLOWED,
                details={"instanceId": instanceId, "status": instance.status},
            )

    async def _applyCancellation(
        self, session: AsyncSession, *, instanceId: int, reason: str | None
    ) -> WorkflowInstance:
        """把待审批 / 待执行命令 / 未开始步骤一并收干净，再把实例判成 CANCELLED。"""

        cancelReason = reason or "实例被取消"

        # 待审批任务 → CANCELLED（不再等审批）
        await self._cancelPendingApprovals(session, instanceId=instanceId)
        # 待执行命令 → CANCELLED（**Worker 再也领不到它** ⇒ 外部写不再发生）
        cancelledStepIds = await self._store.cancelPendingCommands(
            session, instanceId=instanceId, reason=cancelReason
        )
        # 步骤：没开始的（PENDING）与「命令被取消」的（RUNNING）⇒ SKIPPED
        await self._skipPendingSteps(
            session, instanceId=instanceId, cancelledStepIds=cancelledStepIds
        )
        return await self._store.advanceInstance(
            session, instanceId, _STATUS_CANCELLED, errorMessage=reason
        )

    async def _recordCancelAudit(
        self,
        session: AsyncSession,
        *,
        instance: WorkflowInstance,
        actor: str,
        beforeStatus: str,
        reason: str | None,
        actorDepartments: DepartmentsInput = None,
    ) -> None:
        """留痕：取消是治理动作（谁取消的、为什么）。"""

        await self._audit.recordCancel(
            session,
            instance=instance,
            actor=actor,
            beforeStatus=beforeStatus,
            reason=reason,
            actorDepartments=actorDepartments,
        )

    @staticmethod
    def _assertRealIdentity(*, instanceId: int, isRealIdentity: bool) -> None:
        """身份闸（**先于存在性判定** ⇒ 不构成存在性预言机）。"""

        if isRealIdentity:
            return
        raise PermissionDeniedError(
            MSG_WORKFLOW_CANCEL_REAL_AUTH_REQUIRED,
            detail=DETAIL_WORKFLOW_CANCEL_REAL_AUTH_REQUIRED,
            details={"instanceId": instanceId},
        )

    async def _assertNoExecutingCommand(
        self, session: AsyncSession, *, instanceId: int
    ) -> None:
        """在飞的命令 ⇒ **失败关闭（409）**（取消 = 不再写外部系统）。

        ⚠️ Worker 正拿着命令跑 ⇒ 外部写可能已经发生 ⇒ 此时把实例判成
        CANCELLED 就是治理面承诺与实际行为不符 ⇒ 让调用方稍后重试。
        """

        lockedCommands = await self._store.lockInstanceCommands(
            session, instanceId=instanceId
        )
        executingIds = [row.id for row in lockedCommands if row.status == _STATUS_EXECUTING]
        if not executingIds:
            return
        raise ConflictError(
            MSG_WORKFLOW_CANCEL_BUSY,
            detail=DETAIL_WORKFLOW_CANCEL_BUSY,
            details={"instanceId": instanceId, "commandIds": executingIds},
        )

    async def _cancelPendingApprovals(
        self, session: AsyncSession, *, instanceId: int
    ) -> None:
        """待审批任务 → CANCELLED（不再等审批）。"""

        for task in await self._store.listApprovalTasks(session, instanceId=instanceId):
            if task.status == "PENDING":
                await self._store.markApprovalTask(
                    session,
                    task.id,
                    fromStatus=task.status,
                    toStatus="CANCELLED",
                    eventType=EVENT_APPROVAL_CANCELLED,
                )

    async def _skipPendingSteps(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        cancelledStepIds: Sequence[int],
    ) -> None:
        """没开始的步骤（PENDING）与「命令被取消」的步骤 ⇒ SKIPPED。

        （命令没跑 ⇒ 这一步其实没跑；不吊在半路。）
        """

        for step in await self._store.listSteps(session, instanceId=instanceId):
            if step.status == "PENDING" or step.id in cancelledStepIds:
                await self._store.advanceStep(session, step.id, "SKIPPED")

    # ------------------------------------------------------- 审批决策 + 推进

    async def decideApprovalAndAdvance(
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
        """审批决策 + 推进工作流（**两段事务**）。

        ① **决策先落库**：审批是**事实**（审批人确实批了）⇒ 不能被后续故障抹掉
           （否则：审批人要重批、审计行一起回滚查无实据、调用方重试撞 409）。
        ② **推进在独立事务**里做：推进失败 ⇒ 只回滚推进的部分 ⇒ **决策保留**，
           实例**失败关闭**（``FAILED`` + 原因）⇒ 不会「看起来还在跑」。

        ⚠️ 推进失败**不抛异常**：审批已经成功 ⇒ 报 500 会诱导调用方重试 ⇒ 重试
        撞「重复审批 409」⇒ 现场无法解释。失败只体现为实例的终态与日志。
        """

        task = await self._approval.decideApproval(
            session,
            taskId=taskId,
            decision=decision,
            actor=actor,
            roles=roles,
            note=note,
            actorDepartments=actorDepartments,
        )
        # ⚠️ rollback 会 expire 全部 ORM 实例 ⇒ 先取出标量（之后访问属性会触发
        #    同步懒加载 ⇒ MissingGreenlet）。
        instanceId = task.instance_id
        await session.commit()  # ① 决策落库（事实）

        try:
            await self._orchestrator.drive(session, instanceId=instanceId)
            await session.commit()
            return task
        except Exception:
            logger.exception(
                "审批后推进失败：实例 %s 失败关闭（决策已保留）", instanceId
            )
            await session.rollback()
            await self._failInstance(
                session, instanceId=instanceId, reason=MSG_WORKFLOW_DRIVE_FAILED
            )
            await session.commit()
            return await self._reloadTask(session, taskId)

    async def _failInstance(
        self, session: AsyncSession, *, instanceId: int, reason: str
    ) -> WorkflowInstance | None:
        """实例**失败关闭**（推进失败后的兜底）。

        只在实例还**活着**（非终态）时改 ⇒ 已终态的实例不动（不覆盖真实结局）。
        """

        instance = await self._store.lockInstance(session, instanceId)
        if instance is None or instance.status in STOPPED_INSTANCE_STATUSES:
            return instance
        return await self._store.advanceInstance(
            session, instanceId, _STATUS_FAILED, errorMessage=reason
        )

    async def _reloadTask(self, session: AsyncSession, taskId: int) -> ApprovalTask:
        """重读审批任务。

        ⚠️ rollback 之后 ORM 实例已被 expire ⇒ 直接返回旧对象会在调用方
        ``model_validate`` 时触发同步懒加载（MissingGreenlet）⇒ 必须重读。
        """

        row = (
            await session.execute(
                select(ApprovalTask)
                .where(ApprovalTask.id == taskId)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        return row

    async def getInstance(self, session: AsyncSession, instanceId: int) -> WorkflowInstance:
        instance = await self._store.getInstance(session, instanceId)
        if instance is None:
            raise NotFoundError(f"工作流实例 {instanceId} 不存在")
        return instance

    async def listSteps(self, session: AsyncSession, instanceId: int) -> list[WorkflowStep]:
        rows = (
            (
                await session.execute(
                    select(WorkflowStep)
                    .where(WorkflowStep.instance_id == instanceId)
                    .order_by(WorkflowStep.step_order, WorkflowStep.id)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def _resolveDefinition(
        self,
        session: AsyncSession,
        *,
        domainCode: str,
        capabilityCode: str,
        version: str,
    ) -> WorkflowDefinition:
        """解析**当前生效**的定义（服务端判定版本，客户端不得自选）。

        ⚠️ **为什么不能由客户端自选版本**：审批层级写在定义体的审批矩阵里 ⇒
        一个「宽松审批矩阵」的旧版本一旦可被自选，调用方就能**挑低审批层级**
        绕过高层审批 ⇒ 发布审批矩阵不是「发版动作」，而是**安全边界**。
        ⇒ 生效版本由服务端按 `(domain_code, capability_code)` 唯一确定
        （库层部分唯一索引 ``uq_workflow_definition_published`` 兜底），
        客户端指定的版本与之不符 ⇒ 409。

        错误口径：

        - 域能力下**没有任何生效版本**，且该版本号压根不存在 ⇒ 404；
        - 域能力下**没有任何生效版本**，但该版本号存在（草稿 / 已停用）⇒ 409；
        - 生效版本存在但与客户端指定的不一致 ⇒ 409。
        """

        published = await self._loadPublishedDefinition(
            session, domainCode=domainCode, capabilityCode=capabilityCode
        )
        if published is None:
            raise await self._definitionUnavailableError(
                session,
                domainCode=domainCode,
                capabilityCode=capabilityCode,
                version=version,
            )
        if published.version != version:
            raise ConflictError(
                MSG_WORKFLOW_DEFINITION_VERSION_MISMATCH.format(
                    requestedVersion=version,
                    publishedVersion=published.version,
                ),
                detail=DETAIL_WORKFLOW_DEFINITION_VERSION_MISMATCH,
                details={
                    "requestedVersion": version,
                    "publishedVersion": published.version,
                },
            )
        return published

    async def _loadPublishedDefinition(
        self,
        session: AsyncSession,
        *,
        domainCode: str,
        capabilityCode: str,
    ) -> WorkflowDefinition | None:
        """取该域能力下的**生效定义**（至多一条；多于一条 ⇒ 数据坏了 ⇒ 失败关闭）。"""

        rows = (
            (
                await session.execute(
                    select(WorkflowDefinition)
                    .where(
                        WorkflowDefinition.domain_code == domainCode,
                        WorkflowDefinition.capability_code == capabilityCode,
                        WorkflowDefinition.status == _STATUS_PUBLISHED,
                    )
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if len(rows) <= 1:
            return rows[0] if rows else None
        raise ConflictError(
            MSG_WORKFLOW_DEFINITION_AMBIGUOUS.format(
                domainCode=domainCode,
                capabilityCode=capabilityCode,
                versions="、".join(sorted(row.version for row in rows)),
            ),
            detail=DETAIL_WORKFLOW_DEFINITION_AMBIGUOUS,
            details={"domainCode": domainCode, "capabilityCode": capabilityCode},
        )

    async def _definitionUnavailableError(
        self,
        session: AsyncSession,
        *,
        domainCode: str,
        capabilityCode: str,
        version: str,
    ) -> Exception:
        """「没有生效版本」时的错误：不存在 ⇒ 404；存在但未生效 ⇒ 409。"""

        existing = (
            await session.execute(
                select(WorkflowDefinition.id).where(
                    WorkflowDefinition.domain_code == domainCode,
                    WorkflowDefinition.capability_code == capabilityCode,
                    WorkflowDefinition.version == version,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return NotFoundError(
                MSG_WORKFLOW_DEFINITION_NOT_FOUND.format(
                    domain_code=domainCode,
                    capability_code=capabilityCode,
                    version=version,
                )
            )
        return ConflictError(
            MSG_WORKFLOW_DEFINITION_NOT_PUBLISHED.format(version=version),
            detail=DETAIL_WORKFLOW_DEFINITION_NOT_PUBLISHED,
            details={"definitionId": existing, "version": version},
        )

    @staticmethod
    def _assertCanCancel(
        *,
        instance: WorkflowInstance,
        actor: str,
        roles: Sequence[str],
        dbUserId: int | None,
    ) -> None:
        """取消权限：**提交人**本人，或**真实** admin（否则 404）。

        为什么 admin 路径额外要求真实身份：stub 身份的缺省角色带 ``admin``
        （``DEFAULT_STUB_ROLES``）⇒ 只看角色的话，**任何**伪造请求头都能取消
        别人的实例 ⇒ 治理面被绕过。⇒ 跨用户的取消必须绑定 DB 用户 / JWT。
        （提交人取消**自己的**实例是自作用域。）

        ⚠️ 无权 ⇒ **404**（不是 403）：与「实例不存在」同码 ⇒ 调用方无法拿
        取消接口探测实例是否存在（与审批面的对象级授权同口径）。
        """

        if instance.created_by and instance.created_by == actor:
            return
        if ADMIN_ROLE_CODE in (roles or ()) and dbUserId is not None:
            return
        raise NotFoundError(f"工作流实例 {instance.id} 不存在")


def _stepSnapshot(spec: WorkflowStepSpec) -> dict[str, Any]:
    """步骤的**编排快照**（写进 ``workflow_step.input_json``）。

    为什么快照：定义是**可改的**（``workflow_definition`` 没有不可变触发器）
    ⇒ 若运行时回头读定义，定义一改，**在跑的实例**的行为会跟着变。
    """

    return {
        "commandType": spec.commandType,
        "requiresApproval": spec.requiresApproval,
        "payload": dict(spec.payload or {}),
    }
