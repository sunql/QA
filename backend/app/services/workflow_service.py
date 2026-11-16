"""P5 工作流实例生命周期：提交 / 详情。

**提交是幂等的**：同一 ``(definition_id, idempotency_key)`` 重复提交只落一行，
第二次返回既有实例（``created=False``）—— 幂等重放不是错误。

步骤来自定义的 ``definition_json.steps``（**数据驱动**）⇒ 改编排不改代码。

**不 commit**：写操作由 API 层统一提交（⇒ 可与审计两阶段写同一事务）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.workflow_models import (
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowStep,
)
from app.services.messages_zh import (
    MSG_WORKFLOW_DEFINITION_NOT_FOUND,
    MSG_WORKFLOW_DEFINITION_NOT_PUBLISHED,
)
from app.services.workflow_definition import parseWorkflowSteps
from app.services.workflow_store import WorkflowStore

_STATUS_PUBLISHED = "PUBLISHED"
_STATUS_RUNNING = "RUNNING"


class WorkflowService:
    """工作流实例的提交与查询。"""

    def __init__(self, store: WorkflowStore | None = None) -> None:
        self._store = store or WorkflowStore()

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
    ) -> tuple[WorkflowInstance, bool]:
        """提交一个工作流实例。返回 ``(实例, 是否本次新建)``。

        定义不存在 ⇒ 404；定义未发布 ⇒ 409（**不发版就不能跑**）。
        """

        definition = await self._resolveDefinition(
            session, domainCode=domainCode, capabilityCode=capabilityCode, version=version
        )

        instance, created = await self._store.createInstance(
            session,
            definitionId=definition.id,
            idempotencyKey=idempotencyKey,
            requestJson=requestJson,
            actor=actor,
        )
        if not created:
            # 幂等重放：既有实例原样返回（**不重建步骤、不重置状态**）
            return instance, False

        specs = parseWorkflowSteps(definition.definition_json)
        if specs:
            await self._store.createSteps(
                session,
                instanceId=instance.id,
                steps=[{"code": spec.code, "order": spec.order} for spec in specs],
            )
        await self._store.advanceInstance(session, instance.id, _STATUS_RUNNING)
        return instance, True

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
        definition = (
            await session.execute(
                select(WorkflowDefinition).where(
                    WorkflowDefinition.domain_code == domainCode,
                    WorkflowDefinition.capability_code == capabilityCode,
                    WorkflowDefinition.version == version,
                )
            )
        ).scalar_one_or_none()
        if definition is None:
            raise NotFoundError(
                MSG_WORKFLOW_DEFINITION_NOT_FOUND.format(
                    domainCode=domainCode,
                    capabilityCode=capabilityCode,
                    version=version,
                )
            )
        if definition.status != _STATUS_PUBLISHED:
            raise ConflictError(
                MSG_WORKFLOW_DEFINITION_NOT_PUBLISHED.format(status=definition.status),
                detail="WORKFLOW_DEFINITION_NOT_PUBLISHED",
                details={"definitionId": definition.id, "status": definition.status},
            )
        return definition
