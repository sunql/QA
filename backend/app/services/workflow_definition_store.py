"""P5 存储层 · **工作流定义**（定义的增 / 改 / 发布 / 停用）。

职责边界：**只**管 ``workflow_definition`` 表。实例与步骤在
``workflow_instance_store.py``、命令在 ``workflow_command_store.py``、
审批在 ``workflow_approval_store.py``。

为什么定义也需要独立存储层：定义体决定「谁有资格批」⇒ 写它是**安全边界**
操作 ⇒ 必须与实例 / 命令 / 审批一样收口到唯一写入口（G6 守卫
``app/tests/unit/test_workflow_write_gate.py`` 逐条扫源码：任何模块直接
构造 / 改属性 P5 ORM 实体都会红）。

写形状统一为**条件 UPDATE**（``WHERE id=? AND status=<期望前态>``）：
命中 0 行 ⇒ 状态已被并发改动 ⇒ 409（**不静默覆盖**：已发布定义被并发改
= 审批层级被静默改动）。

**不 commit**：与调用方（服务层）的审计写落在同一事务。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError
from app.domain.workflow_models import WorkflowDefinition
from app.services.workflow_store_base import WorkflowStoreBase, utcnow


class WorkflowDefinitionStore(WorkflowStoreBase):
    """工作流定义的持久化操作。"""

    # ------------------------------------------------------------------ 读

    async def listDefinitions(
        self,
        session: AsyncSession,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WorkflowDefinition], int]:
        """管理列表（全部状态，**含定义体**——编辑要用）。返回 ``(行, 总数)``。"""

        conditions = [] if status is None else [WorkflowDefinition.status == status]
        total = (
            await session.execute(
                select(func.count()).select_from(WorkflowDefinition).where(*conditions)
            )
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(WorkflowDefinition)
                    .where(*conditions)
                    .order_by(
                        WorkflowDefinition.domain_code,
                        WorkflowDefinition.capability_code,
                        WorkflowDefinition.version,
                    )
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    async def getDefinition(
        self, session: AsyncSession, definitionId: int
    ) -> WorkflowDefinition | None:
        """读单条定义（不存在返回 ``None`` ⇒ 由服务层转 404）。"""

        return await session.get(WorkflowDefinition, definitionId)

    async def findPublishedDefinition(
        self,
        session: AsyncSession,
        *,
        domainCode: str,
        capabilityCode: str,
        excludeId: int,
    ) -> WorkflowDefinition | None:
        """查同一域能力下**已生效**的其它版本（发布前的冲突预检）。

        ⚠️ 这是**预检**不是保证：并发发布由库层部分唯一索引
        ``uq_workflow_definition_published`` 兜底（撞上 ⇒ IntegrityError ⇒ 409）。
        """

        return (
            await session.execute(
                select(WorkflowDefinition).where(
                    WorkflowDefinition.domain_code == domainCode,
                    WorkflowDefinition.capability_code == capabilityCode,
                    WorkflowDefinition.status == "PUBLISHED",
                    WorkflowDefinition.id != excludeId,
                )
            )
        ).scalar_one_or_none()

    # ------------------------------------------------------------------ 写

    async def insertDefinition(
        self,
        session: AsyncSession,
        *,
        domainCode: str,
        capabilityCode: str,
        version: str,
        definitionJson: dict[str, Any],
        status: str,
        actor: str,
    ) -> WorkflowDefinition:
        """插入定义行（**只 add + flush，不 commit**：审计写要同事务）。"""

        row = WorkflowDefinition(
            domain_code=domainCode,
            capability_code=capabilityCode,
            version=version,
            definition_json=definitionJson,
            status=status,
            created_by=actor,
        )
        session.add(row)
        await session.flush()
        return row

    async def updateDefinitionFields(
        self,
        session: AsyncSession,
        *,
        definitionId: int,
        expectedStatus: str,
        domainCode: str,
        capabilityCode: str,
        version: str,
        definitionJson: dict[str, Any],
    ) -> WorkflowDefinition:
        """改定义体（**条件 UPDATE**：仅当当前状态 = ``expectedStatus``）。"""

        return await self._conditionalUpdate(
            session,
            definitionId=definitionId,
            expectedStatus=expectedStatus,
            values={
                "domain_code": domainCode,
                "capability_code": capabilityCode,
                "version": version,
                "definition_json": definitionJson,
                "updated_time": utcnow(),
            },
            action="编辑",
        )

    async def setStatus(
        self,
        session: AsyncSession,
        *,
        definitionId: int,
        fromStatus: str,
        toStatus: str,
    ) -> WorkflowDefinition:
        """推进定义状态（DRAFT→PUBLISHED / PUBLISHED→DISABLED）。"""

        return await self._conditionalUpdate(
            session,
            definitionId=definitionId,
            expectedStatus=fromStatus,
            values={"status": toStatus, "updated_time": utcnow()},
            action="状态推进",
        )

    async def _conditionalUpdate(
        self,
        session: AsyncSession,
        *,
        definitionId: int,
        expectedStatus: str,
        values: dict[str, Any],
        action: str,
    ) -> WorkflowDefinition:
        """按「当前状态」条件 UPDATE（命中 0 行 ⇒ 并发改动 ⇒ 拒绝覆盖）。

        ``populate_existing=True`` 不可省：调用方此前已把这一行进 identity map，
        SQLAlchemy 默认不刷新既有实例 ⇒ 会按陈旧值继续判定。
        """

        row = (
            await session.execute(
                update(WorkflowDefinition)
                .where(
                    WorkflowDefinition.id == definitionId,
                    WorkflowDefinition.status == expectedStatus,
                )
                .values(**values)
                .returning(WorkflowDefinition)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ConflictError(
                f"定义 {definitionId} 状态已被并发改动（{action}要求当前状态 "
                f"{expectedStatus}），拒绝覆盖"
            )
        return row
