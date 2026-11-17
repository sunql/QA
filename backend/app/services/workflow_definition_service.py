"""P5 工作流**定义管理面**服务（admin-only 写入口）。

为什么要有单独的写面
--------------------

在 UI 出现之前，定义只能「直接 INSERT 一行 JSON」——等于**绕过所有校验**把
审批矩阵写进库。而定义体决定了「谁有资格批」：

- 矩阵条目没写 ``approverUser`` / ``approverRole`` ⇒ 生成一条谁也批不了的任务；
- 步骤既没有 ``commandType`` 也没有 ``requiresApproval`` ⇒ 实例假成功；
- 同一域能力存在两条 ``PUBLISHED`` ⇒ 客户端挑「宽松矩阵」绕过高层审批。

⇒ 写入口必须把 ``workflow_definition.py`` 的**纯函数校验**接上，并且：

1. **只有 DRAFT 可编辑**：PUBLISHED 定义在发起审批时会被读取（审批矩阵），
   改已生效定义 = 改「谁批」⇒ 已发布的只能另建版本；
2. **发布 = 可运行性校验**：草稿允许写一半，发布时必须能被真实跑起来
   （步骤可解析 + 有审批门时矩阵必须覆盖该域能力/风险级别）；
3. **发布不自动替换旧版本**：生效版本唯一是库层部分唯一索引兜底的**安全
   边界**（见 ``workflow_models.WorkflowDefinition``），遇到冲突显式 409，
   由人先停用旧版本——自动替换会静默改审批层级。

治理审计走通用动作 ``CREATE`` / ``UPDATE``（``entity_type`` =
``workflow_definition``），与其余 admin CRUD 同口径，不需要新增迁移。

**数据访问一律走存储层**（``workflow_definition_store.py``）：本服务只做
校验 / 审计 / 事务，**不直接构造或改属性** P5 ORM 实体（G6 守卫
``test_workflow_write_gate.py`` 逐条扫源码）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.workflow_models import WorkflowDefinition
from app.services.audit_service import AuditService
from app.services.workflow_definition import (
    WorkflowStepSpec,
    parseApprovalMatrix,
    parseDefinitionRiskLevel,
    parseWorkflowSteps,
    selectMatrixEntries,
)
from app.services.workflow_store import WorkflowStore

STATUS_DRAFT = "DRAFT"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_DISABLED = "DISABLED"

# 矩阵的 subject 维：P5 只支持实例型；A4 会扩展（见 P5 排期裁决记录）。
SUBJECT_TYPE_INSTANCE = "workflow_instance"

ENTITY_DEFINITION = "workflow_definition"

# 单列长度上限（与库层列宽一致；DTO 已挡一层，服务层再挡一层防直接调用）。
_DOMAIN_MAX_LENGTH = 64
_CAPABILITY_MAX_LENGTH = 64
_VERSION_MAX_LENGTH = 32


class WorkflowDefinitionService:
    """定义的增 / 改 / 发布 / 停用（全部 admin 写入口）。"""

    def __init__(
        self,
        audit: AuditService | None = None,
        store: WorkflowStore | None = None,
    ) -> None:
        self._audit = audit or AuditService()
        self._store = store or WorkflowStore()

    # ------------------------------------------------------------------ 读

    async def listDefinitions(
        self,
        session: AsyncSession,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WorkflowDefinition], int]:
        """管理列表（全部状态，**含定义体**——编辑要用）。"""

        return await self._store.listDefinitions(
            session, status=status, limit=limit, offset=offset
        )

    async def getDefinition(
        self, session: AsyncSession, definitionId: int
    ) -> WorkflowDefinition:
        row = await self._store.getDefinition(session, definitionId)
        if row is None:
            raise NotFoundError(f"工作流定义 {definitionId} 不存在")
        return row

    # ------------------------------------------------------------------ 写

    async def createDefinition(
        self,
        session: AsyncSession,
        *,
        domainCode: str,
        capabilityCode: str,
        version: str,
        definitionJson: dict[str, Any],
        actor: str,
    ) -> WorkflowDefinition:
        """新建定义（**一律 DRAFT**：不发版就不能跑）。"""

        domainCode = _requireCode(domainCode, "domainCode", _DOMAIN_MAX_LENGTH)
        capabilityCode = _requireCode(capabilityCode, "capabilityCode", _CAPABILITY_MAX_LENGTH)
        version = _requireCode(version, "version", _VERSION_MAX_LENGTH)
        _validateDefinitionBody(definitionJson)

        try:
            row = await self._store.insertDefinition(
                session,
                domainCode=domainCode,
                capabilityCode=capabilityCode,
                version=version,
                definitionJson=definitionJson,
                status=STATUS_DRAFT,
                actor=actor,
            )
            await self._audit.record(
                session,
                entity_type=ENTITY_DEFINITION,
                entity_id=row.id,
                action="CREATE",
                actor=actor,
                after=_auditSnapshot(row),
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ConflictError(
                f"定义已存在：{domainCode}/{capabilityCode}@{version}"
            ) from exc
        await session.refresh(row)
        return row

    async def updateDefinition(
        self,
        session: AsyncSession,
        *,
        definitionId: int,
        domainCode: str,
        capabilityCode: str,
        version: str,
        definitionJson: dict[str, Any],
        actor: str,
    ) -> WorkflowDefinition:
        """编辑定义（**仅 DRAFT**；已发布的请另建版本）。"""

        row = await self.getDefinition(session, definitionId)
        if row.status != STATUS_DRAFT:
            raise ConflictError(
                f"定义 {definitionId} 状态为 {row.status}，只有 DRAFT 可以编辑"
                "（已发布的定义请另建新版本）"
            )

        domainCode = _requireCode(domainCode, "domainCode", _DOMAIN_MAX_LENGTH)
        capabilityCode = _requireCode(capabilityCode, "capabilityCode", _CAPABILITY_MAX_LENGTH)
        version = _requireCode(version, "version", _VERSION_MAX_LENGTH)
        _validateDefinitionBody(definitionJson)

        # 审计前像必须在写库**之前**取（写库会刷新 identity map 里的同一实例）。
        before = _auditSnapshot(row)
        try:
            updated = await self._store.updateDefinitionFields(
                session,
                definitionId=definitionId,
                expectedStatus=STATUS_DRAFT,
                domainCode=domainCode,
                capabilityCode=capabilityCode,
                version=version,
                definitionJson=definitionJson,
            )
            await self._audit.record(
                session,
                entity_type=ENTITY_DEFINITION,
                entity_id=updated.id,
                action="UPDATE",
                actor=actor,
                before=before,
                after=_auditSnapshot(updated),
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ConflictError(
                f"定义已存在：{domainCode}/{capabilityCode}@{version}"
            ) from exc
        await session.refresh(updated)
        return updated

    async def publishDefinition(
        self, session: AsyncSession, *, definitionId: int, actor: str
    ) -> WorkflowDefinition:
        """发布（DRAFT → PUBLISHED），发布前做**可运行性**校验。"""

        row = await self.getDefinition(session, definitionId)
        if row.status != STATUS_DRAFT:
            raise ConflictError(
                f"定义 {definitionId} 状态为 {row.status}，只有 DRAFT 可以发布"
            )

        _assertRunnable(row)

        existing = await self._store.findPublishedDefinition(
            session,
            domainCode=row.domain_code,
            capabilityCode=row.capability_code,
            excludeId=row.id,
        )
        if existing is not None:
            raise ConflictError(
                f"{row.domain_code}/{row.capability_code} 已有生效版本 "
                f"{existing.version}（id={existing.id}），请先停用后再发布",
                detail="WORKFLOW_DEFINITION_ALREADY_PUBLISHED",
            )

        # ⚠️ rollback 会 expire 全部 ORM 实例 ⇒ 错误信息里用**提前取好的标量**，
        #    不在 except 块里读 row.*（那会触发隐式 IO ⇒ MissingGreenlet）。
        scope = f"{row.domain_code}/{row.capability_code}"
        before = _auditSnapshot(row)
        try:
            updated = await self._store.setStatus(
                session,
                definitionId=definitionId,
                fromStatus=STATUS_DRAFT,
                toStatus=STATUS_PUBLISHED,
            )
            await self._audit.record(
                session,
                entity_type=ENTITY_DEFINITION,
                entity_id=updated.id,
                action="UPDATE",
                actor=actor,
                before=before,
                after=_auditSnapshot(updated),
            )
            await session.commit()
        except IntegrityError as exc:  # 并发发布 ⇒ 库层部分唯一索引兜底
            await session.rollback()
            raise ConflictError(f"{scope} 已有生效版本（并发发布）") from exc
        await session.refresh(updated)
        return updated

    async def disableDefinition(
        self, session: AsyncSession, *, definitionId: int, actor: str
    ) -> WorkflowDefinition:
        """停用（PUBLISHED → DISABLED）。停用后可发布新版本接替。"""

        row = await self.getDefinition(session, definitionId)
        if row.status != STATUS_PUBLISHED:
            raise ConflictError(
                f"定义 {definitionId} 状态为 {row.status}，只有 PUBLISHED 可以停用"
            )

        before = _auditSnapshot(row)
        updated = await self._store.setStatus(
            session,
            definitionId=definitionId,
            fromStatus=STATUS_PUBLISHED,
            toStatus=STATUS_DISABLED,
        )
        await self._audit.record(
            session,
            entity_type=ENTITY_DEFINITION,
            entity_id=updated.id,
            action="UPDATE",
            actor=actor,
            before=before,
            after=_auditSnapshot(updated),
        )
        await session.commit()
        await session.refresh(updated)
        return updated


def _requireCode(value: str, field: str, maxLength: int) -> str:
    """去空白 + 非空 + 长度（DTO 挡一层，服务层挡直接调用方）。"""

    text = (value or "").strip()
    if not text:
        raise ValidationError(f"{field} 不能为空")
    if len(text) > maxLength:
        raise ValidationError(f"{field} 超长（{len(text)} > {maxLength}）")
    return text


def _validateDefinitionBody(definitionJson: dict[str, Any]) -> tuple[WorkflowStepSpec, ...]:
    """保存期的**结构**校验（与运行时同一套纯函数 ⇒ 不会漂移）。"""

    specs = parseWorkflowSteps(definitionJson)
    parseApprovalMatrix(definitionJson)          # 条目非法 ⇒ 422
    parseDefinitionRiskLevel(definitionJson)     # 风险级别非法 ⇒ 422
    return specs


def _assertRunnable(row: WorkflowDefinition) -> None:
    """发布期的**可运行性**校验：草稿可以写一半，发布必须跑得起来。

    有审批门 ⇒ 必须能选中一条矩阵（三段键精确匹配），否则实例运行到审批门
    必然 409 ``APPROVAL_MATRIX_NOT_MATCHED`` —— 那种定义不该被发布。
    """

    specs = _validateDefinitionBody(row.definition_json)
    if not any(spec.requiresApproval for spec in specs):
        return

    riskLevel = parseDefinitionRiskLevel(row.definition_json)
    if riskLevel is None:
        raise ValidationError(
            "定义含审批环节但缺少顶层 riskLevel：无法决定走哪条审批矩阵"
        )
    matched = selectMatrixEntries(
        parseApprovalMatrix(row.definition_json),
        subjectType=SUBJECT_TYPE_INSTANCE,
        capabilityCode=row.capability_code,
        riskLevel=riskLevel,
    )
    if not matched:
        raise ValidationError(
            "定义含审批环节，但审批矩阵没有覆盖"
            f"（{SUBJECT_TYPE_INSTANCE}, {row.capability_code}, {riskLevel}）"
        )


def _auditSnapshot(row: WorkflowDefinition) -> dict[str, Any]:
    """审计快照只留判定性字段（定义体可能很大，不入 before/after）。"""

    return {
        "domainCode": row.domain_code,
        "capabilityCode": row.capability_code,
        "version": row.version,
        "status": row.status,
    }
