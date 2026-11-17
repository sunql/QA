"""P5 存储层 · **实例与步骤**（幂等创建 / 行锁推进 / 步骤编排）。

职责边界：**只**管 ``workflow_instance`` 与 ``workflow_step`` 两张表。
命令在 ``workflow_command_store.py``、审批在 ``workflow_approval_store.py``、
共享底座（状态字面量 / 事件词汇表 / 条件 UPDATE）在 ``workflow_store_base.py``
⇒ 三个子存储 + 一个门面（``workflow_store.py``）⇒ 单文件都在行数上限内，
且「谁写哪张表」一眼可见。

**不 commit**：所有写操作与调用方业务写在同一事务里原子提交。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.workflow_models import WorkflowInstance, WorkflowStep
from app.services.workflow_state_machine import assertWorkflowTransition
from app.services.workflow_store_base import (
    EVENT_INSTANCE_CREATED,
    EVENT_INSTANCE_STATUS_CHANGED,
    EVENT_STEP_STATUS_CHANGED,
    EVENT_STEPS_CREATED,
    STATUS_PENDING,
    WorkflowStoreBase,
    utcnow,
)


class WorkflowInstanceStore(WorkflowStoreBase):
    """实例与步骤的持久化操作。"""

    async def getInstance(self, session: AsyncSession, instanceId: int) -> WorkflowInstance | None:
        """无锁读实例（返回 None 表示不存在）。"""

        return (
            await session.execute(select(WorkflowInstance).where(WorkflowInstance.id == instanceId))
        ).scalar_one_or_none()

    async def getInstanceStatus(
        self, session: AsyncSession, instanceId: int
    ) -> str | None:
        """无锁读实例状态（返回 None 表示实例不存在）。

        ⚠️ 刻意**不加行锁**：Worker 的锁序是「命令 → 实例」，这里再锁实例会
        与取消路径打架 ⇒ 40P01 死锁。
        """

        return (
            await session.execute(
                select(WorkflowInstance.status).where(WorkflowInstance.id == instanceId)
            )
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

        重复提交（同一 ``definitionId`` + **同一提交人** + ``idempotencyKey``）
        ⇒ 返回既有实例，``created=False``，**不抛错**（幂等重放是正常路径）。

        ⚠️ 幂等作用域**按提交人隔离**（库层唯一约束含 ``created_by``）：
        别人用同一个键 ⇒ 各自独立实例（不是 409）⇒ ① 不泄露「这个键存在」
        （存在性预言机）；② 别人的键不会被占死（DoS）。
        """

        insertedId = await self._insertInstance(
            session,
            definitionId=definitionId,
            idempotencyKey=idempotencyKey,
            requestJson=requestJson,
            actor=actor,
            businessKey=businessKey,
        )

        existing = await self._loadInstanceByIdempotency(
            session,
            definitionId=definitionId,
            idempotencyKey=idempotencyKey,
            actor=actor,
        )

        if insertedId is not None:
            # 只在**本次新建**时留事件（幂等重放不重复留痕）
            await self._recordCreatedEvent(
                session,
                instanceId=existing.id,
                definitionId=definitionId,
                idempotencyKey=idempotencyKey,
                actor=actor,
            )

        return existing, insertedId is not None

    async def _insertInstance(
        self,
        session: AsyncSession,
        *,
        definitionId: int,
        idempotencyKey: str,
        requestJson: dict[str, Any],
        actor: str,
        businessKey: str | None,
    ) -> int | None:
        """插入实例行（幂等键冲突则不插）⇒ 返回新行 id；冲突时返回 ``None``。"""

        return (
            await session.execute(
                pg_insert(WorkflowInstance)
                .values(
                    definition_id=definitionId,
                    idempotency_key=idempotencyKey,
                    business_key=businessKey,
                    status=STATUS_PENDING,
                    request_json=requestJson,
                    created_by=actor,
                )
                .on_conflict_do_nothing(
                    index_elements=["definition_id", "created_by", "idempotency_key"]
                )
                .returning(WorkflowInstance.id)
            )
        ).scalar_one_or_none()

    async def _loadInstanceByIdempotency(
        self,
        session: AsyncSession,
        *,
        definitionId: int,
        idempotencyKey: str,
        actor: str,
    ) -> WorkflowInstance:
        """按（定义, 提交人, 幂等键）读回实例行。"""

        return (
            await session.execute(
                select(WorkflowInstance)
                .where(
                    WorkflowInstance.definition_id == definitionId,
                    WorkflowInstance.created_by == actor,
                    WorkflowInstance.idempotency_key == idempotencyKey,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    async def _recordCreatedEvent(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        definitionId: int,
        idempotencyKey: str,
        actor: str,
    ) -> None:
        """落「实例已创建」事件（只在**本次新建**时写）。"""

        await self.enqueueEvent(
            session,
            instanceId=instanceId,
            eventType=EVENT_INSTANCE_CREATED,
            payload={
                "definitionId": definitionId,
                "idempotencyKey": idempotencyKey,
                "actor": actor,
            },
        )

    async def advanceInstance(
        self,
        session: AsyncSession,
        instanceId: int,
        toStatus: str,
        *,
        errorMessage: str | None = None,
        resultJson: dict[str, Any] | None = None,
    ) -> WorkflowInstance:
        """推进实例状态（行锁 + 状态机校验）。"""

        current = await self._lockInstance(session, instanceId)
        assertWorkflowTransition("instance", current.status, toStatus)

        values: dict[str, Any] = {"status": toStatus, "updated_time": utcnow()}
        if errorMessage is not None:
            values["error_message"] = errorMessage
        if resultJson is not None:
            values["result_json"] = resultJson

        updated = await self._writeStatus(
            session,
            WorkflowInstance,
            instanceId,
            fromStatus=current.status,
            values=values,
        )
        await self.enqueueEvent(
            session,
            instanceId=instanceId,
            eventType=EVENT_INSTANCE_STATUS_CHANGED,
            payload={"from": current.status, "to": toStatus},
        )
        return updated

    async def lockInstance(
        self, session: AsyncSession, instanceId: int
    ) -> WorkflowInstance | None:
        """在行锁内**重读**实例（`populate_existing` ⇒ 拿到最新状态）。

        不存在 ⇒ 返回 ``None``（调用方自己决定 404 还是别的语义）。
        编排器用它拿「当前状态」——**必须**重读，否则 identity map 里的旧值会
        让编排器按陈旧状态推进（并发下丢步）。
        """

        return (
            await session.execute(
                select(WorkflowInstance)
                .where(WorkflowInstance.id == instanceId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def _lockInstance(self, session: AsyncSession, instanceId: int) -> WorkflowInstance:
        """在行锁内**重读**实例（`populate_existing` ⇒ 拿到最新状态）。"""

        current = await self.lockInstance(session, instanceId)
        if current is None:
            raise NotFoundError(f"工作流实例 {instanceId} 不存在")
        return current


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

        await self._assertStepCodesUnique(session, instanceId=instanceId, steps=steps)

        rows = [
            WorkflowStep(
                instance_id=instanceId,
                step_code=step["code"],
                step_order=int(step.get("order", index)),
                status=STATUS_PENDING,
                input_json=step.get("input"),
            )
            for index, step in enumerate(steps)
        ]
        session.add_all(rows)
        await session.flush()
        await self.enqueueEvent(
            session,
            instanceId=instanceId,
            eventType=EVENT_STEPS_CREATED,
            payload={"stepCodes": [row.step_code for row in rows]},
        )
        return rows

    async def _assertStepCodesUnique(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        steps: Sequence[dict[str, Any]],
    ) -> None:
        """实例内步骤编码必须唯一（含与既有步骤的冲突）⇒ 重复直接拒。

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

    async def listSteps(self, session: AsyncSession, *, instanceId: int) -> list[WorkflowStep]:
        """列出实例的步骤（按 ``step_order`` 升序，id 次序兜底）。"""

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

        values: dict[str, Any] = {"status": toStatus, "updated_time": utcnow()}
        if outputJson is not None:
            values["output_json"] = outputJson
        if errorMessage is not None:
            values["error_message"] = errorMessage
        if toStatus == "RUNNING" and current.started_at is None:
            values["started_at"] = utcnow()
        if toStatus in {"SUCCEEDED", "FAILED", "SKIPPED", "COMPENSATED"}:
            values["finished_at"] = utcnow()

        updated = await self._writeStatus(
            session,
            WorkflowStep,
            stepId,
            fromStatus=current.status,
            values=values,
        )
        await self.enqueueEvent(
            session,
            instanceId=current.instance_id,
            eventType=EVENT_STEP_STATUS_CHANGED,
            payload={"stepId": stepId, "from": current.status, "to": toStatus},
        )
        return updated
