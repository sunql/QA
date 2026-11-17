"""P5 审批面 · **只读查询**（我的待办 / 审批持有人可见的实例）。

从 ``workflow_approval_service`` 拆出（单文件 800 行硬上限）：写路径（发起 /
决策 / 超时清扫）与读路径（待办 / 可见性）职责不同，读路径**不加锁、不写库**。

⚠️ **授权口径必须与详情面 ``_assertInstanceVisible`` 逐条一致**：
「列表里看得到」但「详情 404」= 列表泄露存在性；反过来 = 页面永远点不进去。
⇒ 持有人判定只有这一份（``matchesHolder``），写路径的 ``assertInstanceVisible``
也调 ``isApprovalHolder`` 走同一口径。

⚠️ **矩阵在 JSONB 里**：``approver_user`` 列只在决策那一刻写签署人
⇒ 待审批时任务行为空，「谁有资格批」只能查定义体审批矩阵 ⇒ SQL 无法只凭
任务行判定 ⇒ 有界扫描后在内存过滤（``HOLDER_SCAN_LIMIT``，命中上限记 warning）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError, PermissionDeniedError
from app.domain.workflow_models import (
    ApprovalTask,
    WorkflowDefinition,
    WorkflowInstance,
)
from app.services.messages_zh import MSG_WORKFLOW_APPROVAL_REAL_AUTH_REQUIRED
from app.services.workflow_definition import ApprovalMatrixEntry, parseApprovalMatrix
from app.services.workflow_store import WorkflowStore

logger = logging.getLogger(__name__)

DETAIL_APPROVAL_REAL_AUTH_REQUIRED = "APPROVAL_REAL_AUTH_REQUIRED"

STATUS_PENDING = "PENDING"

# 审批持有人扫描上限：待办 / 实例列表要按定义体审批矩阵判定持有人，
# 而矩阵在 JSONB 里、SQL 无法只凭任务行判定 ⇒ 只能有界扫描后在内存过滤。
# 命中上限会记 warning（**不静默截断**）；超过此规模需要把指定审批人落到列上
# （A4-0 扩展 ``approval_task.subject_id`` 时一并做，见 WP-ONT-AI 排期）。
HOLDER_SCAN_LIMIT = 2000


def matchesHolder(
    *,
    task: ApprovalTask,
    entry: ApprovalMatrixEntry | None,
    actor: str,
    roles: Sequence[str],
) -> bool:
    """该 actor 是否被这条任务 / 矩阵条目认作审批人。

    三条来源：

    1. 任务行 ``approver_user``：**实际签署人**（决策那一刻写入 ⇒ 已批的任务）；
    2. 矩阵条目 ``approverUser``：指定人（待审批时任务行为空，只有这里知道）；
    3. 矩阵条目 ``approverRole`` / 任务行 ``approver_role``：角色匹配。

    ⚠️ 矩阵条目**指定了人**时不再看角色（``elif``）—— 与既有判定逐字一致。
    矩阵条目已不在定义体里（定义被改过）⇒ 只认任务行的签署人。
    """

    if task.approver_user and task.approver_user == actor:
        return True
    if entry is None:
        return False
    if entry.approverUser:
        if entry.approverUser == actor:
            return True
    elif entry.approverRole and entry.approverRole in roles:
        return True
    return bool(task.approver_role and task.approver_role in roles)


class WorkflowApprovalQueryService:
    """审批面的只读查询（不加锁、不写库、不 commit）。"""

    def __init__(self, store: WorkflowStore | None = None) -> None:
        self._store = store or WorkflowStore()

    async def isApprovalHolder(
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
        matrixByCode = _matrixByCode(definition)
        return any(
            matchesHolder(
                task=task,
                entry=matrixByCode.get(task.matrix_code),
                actor=actor,
                roles=roles,
            )
            for task in tasks
        )

    async def listMyApprovals(
        self,
        session: AsyncSession,
        *,
        actor: str,
        roles: Sequence[str] = (),
        isRealIdentity: bool = False,
        status: str | None = STATUS_PENDING,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[
        list[tuple[ApprovalTask, WorkflowInstance, WorkflowDefinition]], int
    ]:
        """「我的待办」：该 actor 可审批的任务 + 实例 / 定义上下文。

        ⚠️ **要求已校验身份**（``isRealIdentity``）：待办是**跨用户读**
        （别人的实例）⇒ stub 头（``X-User-Id: lead``）可冒充任意账号 ⇒
        不验签就等于把整个审批面读穿。自作用域读（自己的实例）不在此列。
        """

        if not isRealIdentity:
            raise PermissionDeniedError(
                MSG_WORKFLOW_APPROVAL_REAL_AUTH_REQUIRED,
                detail=DETAIL_APPROVAL_REAL_AUTH_REQUIRED,
            )

        matched = await self._scanHolderRows(
            session,
            actor=actor,
            roles=roles,
            statuses=[status] if status else None,
        )
        total = len(matched)
        return matched[offset : offset + limit], total

    async def listVisibleInstanceIds(
        self,
        session: AsyncSession,
        *,
        actor: str,
        roles: Sequence[str] = (),
        isRealIdentity: bool = False,
    ) -> list[int]:
        """该 actor 以**审批持有人**身份可见的实例 id（跨用户读 ⇒ 同样要求验签）。

        返回空列表（而不是报错）：未验签的非 admin 只能看到**自作用域**实例，
        列表接口据此把「自己的 + 这批」合并 ⇒ 不泄露任何存在性。
        """

        if not isRealIdentity:
            return []

        matched = await self._scanHolderRows(
            session, actor=actor, roles=roles, statuses=None
        )
        return sorted({instance.id for _, instance, _ in matched})

    # ---------------------------------------------------------------- 内部

    async def _loadDefinition(
        self, session: AsyncSession, definitionId: int
    ) -> WorkflowDefinition:
        definition = await session.get(WorkflowDefinition, definitionId)
        if definition is None:
            # 外键 RESTRICT + 实例一定有定义 ⇒ 走到这里说明数据被绕过约束改过。
            raise NotFoundError(f"工作流定义 {definitionId} 不存在")
        return definition

    async def _scanHolderRows(
        self,
        session: AsyncSession,
        *,
        actor: str,
        roles: Sequence[str],
        statuses: Sequence[str] | None,
    ) -> list[tuple[ApprovalTask, WorkflowInstance, WorkflowDefinition]]:
        """有界扫描审批任务，按定义体审批矩阵过滤出该 actor 持有的行。"""

        rows = await self._store.listTasksWithContext(
            session, statuses=statuses, limit=HOLDER_SCAN_LIMIT
        )
        if len(rows) >= HOLDER_SCAN_LIMIT:
            logger.warning(
                "审批持有人扫描命中上限 %s 条，列表结果可能不完整"
                "（需把指定审批人落到任务行后改回 SQL 过滤）",
                HOLDER_SCAN_LIMIT,
            )

        matrices: dict[int, dict[str, ApprovalMatrixEntry]] = {}
        matched: list[tuple[ApprovalTask, WorkflowInstance, WorkflowDefinition]] = []
        for task, instance, definition in rows:
            matrix = matrices.get(definition.id)
            if matrix is None:
                matrix = _matrixByCode(definition)
                matrices[definition.id] = matrix
            if matchesHolder(
                task=task,
                entry=matrix.get(task.matrix_code),
                actor=actor,
                roles=roles,
            ):
                matched.append((task, instance, definition))
        return matched


def _matrixByCode(definition: WorkflowDefinition) -> dict[str, ApprovalMatrixEntry]:
    return {
        entry.matrixCode: entry
        for entry in parseApprovalMatrix(definition.definition_json)
    }
