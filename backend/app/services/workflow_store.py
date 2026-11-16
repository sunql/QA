"""P5 工作流存储层：幂等创建 / 行锁推进 / 条件 UPDATE claim。

三条纪律（对应实施计划 D-2 / D-3 / D-4）：

1. **幂等**（D-2）：创建实例 / 入队命令都用
   ``INSERT ... ON CONFLICT DO NOTHING RETURNING id`` —— 一条语句原子完成
   「不存在才插入」⇒ 并发下重复提交也只落一行；命中冲突时返回既有行并
   ``created=False``（**幂等重放不是错误**）。
2. **行锁 + 重读**（D-3）：推进前在锁内 ``SELECT ... FOR UPDATE`` 且带
   ``execution_options(populate_existing=True)``。**`populate_existing` 不可省**：
   调用方通常早已把这一行进 identity map，SQLAlchemy 默认不刷新既有实例属性
   ⇒ 拿到锁却按陈旧值判定流转 ⇒ 并发下**丢步**（两个推进都落地，实例跳过中间态）。
3. **条件 UPDATE claim**（D-3）：``WHERE id=? AND status='PENDING'`` ⇒ 同一命令
   只能被 claim 一次；候选选取用 ``SKIP LOCKED`` ⇒ 多 worker 各拿各的。

状态流转一律先过**纯函数矩阵**（`app/services/workflow_state_machine.py`）⇒
非法流转在写库前就被拒（拒绝即零副作用）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.workflow_models import (
    BusinessCommand,
    WorkflowInstance,
    WorkflowStep,
)
from app.services.workflow_state_machine import assertWorkflowTransition

# 命令 / 步骤的初始状态字面量（与状态机矩阵、库层 CHECK 一致）。
_STATUS_PENDING = "PENDING"
_STATUS_EXECUTING = "EXECUTING"

# 幂等冲突的错误信息（ConflictError ⇒ 409）。
_MSG_STEP_INSTANCE_MISMATCH = "步骤 {stepId} 不属于实例 {instanceId}（拒绝跨实例挂载）"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WorkflowStore:
    """工作流实例 / 步骤 / 命令的持久化操作。

    **不 commit**：所有写操作与调用方业务写在同一事务里原子提交（照
    `app/services/outbox_service.py` 的同事务语义）。
    """

    # ---------------------------------------------------------------- 实例

    async def getInstance(self, session: AsyncSession, instanceId: int) -> WorkflowInstance | None:
        """无锁读实例（返回 None 表示不存在）。"""

        return (
            await session.execute(select(WorkflowInstance).where(WorkflowInstance.id == instanceId))
        ).scalar_one_or_none()

    async def createInstance(
        self,
        session: AsyncSession,
        *,
        definitionId: int,
        idempotencyKey: str,
        requestJson: dict[str, Any],
        actor: str,
        businessKey: str | None = None,
    ) -> tuple[WorkflowInstance, bool]:
        """幂等创建实例。返回 ``(实例, 是否本次新建)``。

        重复提交（同一 ``definitionId`` + ``idempotencyKey``）⇒ 返回既有实例，
        ``created=False``，**不抛错**（幂等重放是正常路径）。
        """

        insertedId = (
            await session.execute(
                pg_insert(WorkflowInstance)
                .values(
                    definition_id=definitionId,
                    idempotency_key=idempotencyKey,
                    business_key=businessKey,
                    status=_STATUS_PENDING,
                    request_json=requestJson,
                    created_by=actor,
                )
                .on_conflict_do_nothing(index_elements=["definition_id", "idempotency_key"])
                .returning(WorkflowInstance.id)
            )
        ).scalar_one_or_none()

        existing = (
            await session.execute(
                select(WorkflowInstance)
                .where(
                    WorkflowInstance.definition_id == definitionId,
                    WorkflowInstance.idempotency_key == idempotencyKey,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

        return existing, insertedId is not None

    async def advanceInstance(
        self,
        session: AsyncSession,
        instanceId: int,
        toStatus: str,
        *,
        currentStep: str | None = None,
        errorMessage: str | None = None,
        resultJson: dict[str, Any] | None = None,
    ) -> WorkflowInstance:
        """推进实例状态（行锁 + 状态机校验）。"""

        current = await self._lockInstance(session, instanceId)
        assertWorkflowTransition("instance", current.status, toStatus)

        values: dict[str, Any] = {"status": toStatus, "updated_time": _utcnow()}
        if currentStep is not None:
            values["current_step"] = currentStep
        if errorMessage is not None:
            values["error_message"] = errorMessage
        if resultJson is not None:
            values["result_json"] = resultJson

        return await self._writeStatus(
            session,
            WorkflowInstance,
            instanceId,
            fromStatus=current.status,
            values=values,
        )

    async def _lockInstance(self, session: AsyncSession, instanceId: int) -> WorkflowInstance:
        """在行锁内**重读**实例（`populate_existing` ⇒ 拿到最新状态）。"""

        current = (
            await session.execute(
                select(WorkflowInstance)
                .where(WorkflowInstance.id == instanceId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise NotFoundError(f"工作流实例 {instanceId} 不存在")
        return current

    # ---------------------------------------------------------------- 步骤

    async def createSteps(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        steps: Sequence[dict[str, Any]],
    ) -> list[WorkflowStep]:
        """按编排创建实例的步骤（初始状态 PENDING）。

        同一实例内步骤编码必须唯一 ⇒ 重复直接拒（ConflictError），
        **不靠库层唯一约束报错**（那会变成 500 而不是可读的 409）。
        库层唯一约束仍是并发下的兜底。
        """

        existingCodes = set(
            (
                await session.execute(
                    select(WorkflowStep.step_code).where(
                        WorkflowStep.instance_id == instanceId
                    )
                )
            )
            .scalars()
            .all()
        )
        codes = [str(step["code"]) for step in steps]
        seen: set[str] = set()
        duplicates: set[str] = set()
        for code in codes:
            if code in existingCodes or code in seen:
                duplicates.add(code)
            seen.add(code)
        if duplicates:
            raise ConflictError(
                f"步骤编码重复：{', '.join(sorted(duplicates))}（实例内必须唯一）"
            )

        rows = [
            WorkflowStep(
                instance_id=instanceId,
                step_code=step["code"],
                step_order=int(step.get("order", index)),
                status=_STATUS_PENDING,
                input_json=step.get("input"),
            )
            for index, step in enumerate(steps)
        ]
        session.add_all(rows)
        await session.flush()
        return rows

    async def advanceStep(
        self,
        session: AsyncSession,
        stepId: int,
        toStatus: str,
        *,
        outputJson: dict[str, Any] | None = None,
        errorMessage: str | None = None,
    ) -> WorkflowStep:
        """推进步骤状态（行锁 + 状态机校验）。"""

        current = (
            await session.execute(
                select(WorkflowStep)
                .where(WorkflowStep.id == stepId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise NotFoundError(f"工作流步骤 {stepId} 不存在")

        assertWorkflowTransition("step", current.status, toStatus)

        values: dict[str, Any] = {"status": toStatus, "updated_time": _utcnow()}
        if outputJson is not None:
            values["output_json"] = outputJson
        if errorMessage is not None:
            values["error_message"] = errorMessage
        if toStatus == "RUNNING" and current.started_at is None:
            values["started_at"] = _utcnow()
        if toStatus in {"SUCCEEDED", "FAILED", "SKIPPED", "COMPENSATED"}:
            values["finished_at"] = _utcnow()

        return await self._writeStatus(
            session,
            WorkflowStep,
            stepId,
            fromStatus=current.status,
            values=values,
        )

    # ---------------------------------------------------------------- 命令

    async def enqueueCommand(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        stepId: int | None,
        commandType: str,
        idempotencyKey: str,
        payload: dict[str, Any],
        executedBy: str | None = None,
    ) -> tuple[BusinessCommand, bool]:
        """幂等入队业务命令。返回 ``(命令, 是否本次新建)``。

        步骤必须属于同一实例 ⇒ 拒绝跨实例挂载（写错实例会污染审计链）。
        """

        if stepId is not None:
            stepInstanceId = (
                await session.execute(select(WorkflowStep.instance_id).where(WorkflowStep.id == stepId))
            ).scalar_one_or_none()
            if stepInstanceId is None or stepInstanceId != instanceId:
                raise ConflictError(_MSG_STEP_INSTANCE_MISMATCH.format(stepId=stepId, instanceId=instanceId))

        insertedId = (
            await session.execute(
                pg_insert(BusinessCommand)
                .values(
                    instance_id=instanceId,
                    step_id=stepId,
                    command_type=commandType,
                    idempotency_key=idempotencyKey,
                    status=_STATUS_PENDING,
                    payload=payload,
                    executed_by=executedBy,
                )
                .on_conflict_do_nothing(index_elements=["command_type", "idempotency_key"])
                .returning(BusinessCommand.id)
            )
        ).scalar_one_or_none()

        existing = (
            await session.execute(
                select(BusinessCommand)
                .where(
                    BusinessCommand.command_type == commandType,
                    BusinessCommand.idempotency_key == idempotencyKey,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

        return existing, insertedId is not None

    async def claimCommand(
        self, session: AsyncSession, commandId: int, *, workerId: str
    ) -> BusinessCommand | None:
        """条件 UPDATE claim：``PENDING → EXECUTING``。已被别人领走 ⇒ None。"""

        row = (
            await session.execute(
                update(BusinessCommand)
                .where(
                    BusinessCommand.id == commandId,
                    BusinessCommand.status == _STATUS_PENDING,
                )
                .values(
                    status=_STATUS_EXECUTING,
                    executed_by=workerId,
                    attempts=BusinessCommand.attempts + 1,
                    updated_time=_utcnow(),
                )
                .returning(BusinessCommand)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return row

    async def claimNextCommand(self, session: AsyncSession, *, workerId: str) -> BusinessCommand | None:
        """领取一条待执行命令（``SKIP LOCKED``：跳过别人锁住的行）。"""

        candidateId = (
            await session.execute(
                select(BusinessCommand.id)
                .where(BusinessCommand.status == _STATUS_PENDING)
                .order_by(BusinessCommand.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if candidateId is None:
            return None
        return await self.claimCommand(session, candidateId, workerId=workerId)

    async def completeCommand(
        self,
        session: AsyncSession,
        commandId: int,
        toStatus: str,
        *,
        result: dict[str, Any] | None = None,
        errorMessage: str | None = None,
    ) -> BusinessCommand:
        """结束命令（行锁 + 状态机校验）。"""

        current = (
            await session.execute(
                select(BusinessCommand)
                .where(BusinessCommand.id == commandId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise NotFoundError(f"业务命令 {commandId} 不存在")

        assertWorkflowTransition("command", current.status, toStatus)

        values: dict[str, Any] = {"status": toStatus, "updated_time": _utcnow()}
        if result is not None:
            values["result"] = result
        if errorMessage is not None:
            values["error_message"] = errorMessage

        return await self._writeStatus(
            session,
            BusinessCommand,
            commandId,
            fromStatus=current.status,
            values=values,
        )

    # ---------------------------------------------------------------- 内部

    @staticmethod
    async def _writeStatus(
        session: AsyncSession,
        model: type,
        rowId: int,
        *,
        fromStatus: str,
        values: dict[str, Any],
    ) -> Any:
        """按「当前状态」条件 UPDATE（双保险：行锁之外再加一道状态判定）。

        条件 UPDATE 命中 0 行 ⇒ 状态已被并发改动 ⇒ 拒绝（不静默覆盖）。
        """

        row = (
            await session.execute(
                update(model)
                .where(model.id == rowId, model.status == fromStatus)
                .values(**values)
                .returning(model)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ConflictError(f"状态已被并发改动，拒绝覆盖（期望当前状态 {fromStatus}）")
        return row
