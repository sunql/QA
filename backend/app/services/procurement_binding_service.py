"""采购源绑定服务（M1-1.1）。

两张表的业务层：**候选 → 确认** 的流转、复合键与过滤白名单校验、有效期软失效。

三条纪律（与 A8 §4 / 迁移 `0154` 一致）：

1. **候选 ≠ 结论**（决策 D-3）：新建一律落候选（`is_candidate = TRUE`），
   确认必须由人显式触发（`confirm*`），**确认人取当前登录用户**（不信任客户端）。
   读路径只认「已确认且当前生效」（`listEffective*`）。
2. **有效期是软版本化的唯一机制**：`valid_to IS NULL` = 当前生效；
   失效是**软失效**（写 `valid_to`），不删行 ⇒ 历史证据不丢。
3. **每次写入都留痕**：CREATE / UPDATE 走 `AuditService`（审计动作白名单已闭合，
   候选→确认复用 `UPDATE`，**不需要新迁移**）。

**状态机（确认 = 版本冻结点）**

    候选态（父未确认）──▶ 确认态（父已确认，子集合冻结）──▶ 失效态（只读）
       可增删子、可确认子        只能整体失效（级联子）        不可再写

为什么「确认后冻结子集合」：否则可以在**父已确认之后**追加并确认子绑定 ⇒
复合键序号连续性闸门（只在父确认那一刻跑一次）被绕过 ⇒ 生效映射出现缺位键
（`[1, 3]`）⇒ 下游把「第 2 键」当「第 1 键」用。
要改绑必须走「失效现役行 → 建新候选 → 重新确认」。

**锁序**：所有写路径**先锁父源绑定、再锁子属性绑定**（固定顺序 ⇒ 不会死锁）。

库层约束（迁移 `0154`）负责「不变量」，本模块负责「可用性」：
唯一索引冲突 → `ConflictError`（可读文案），规则不满足 → `ValidationError`。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import DataSource, OntologyClass, OntologyProperty
from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)
from app.schemas.procurement_binding import PropertyBindingCreate, SourceBindingCreate
from app.services.audit_service import AuditService
from app.services.procurement_binding_validation import (
    validateAllowlistItems,
    validateBindingRole,
    validateColumnName,
    validateFilterExpression,
    validateKeyOrdinals,
    validateTransformExpression,
)

logger = logging.getLogger(__name__)

#: 审计里的实体名（与表名一致，便于按表反查）。
SOURCE_ENTITY = "procurement_source_binding"
PROPERTY_ENTITY = "procurement_property_binding"

#: 失效时刻相对生效时刻的最小间隔（秒）。
#: 原因：`now()` 是**事务开始时刻**，与同事务新建行的 `valid_from` 完全相同 ⇒
#: `valid_to > valid_from` 会失败（迁移 0154 的 CHECK）。差 1 秒即可绕开。
_EXPIRE_GAP = timedelta(seconds=1)

#: 操作用户标识的最大长度（与库列 `created_by` / `confirmed_by` 的 `String(64)` 对齐）。
#: 超过 ⇒ PostgreSQL 抛 `StringDataRightTruncationError`（不是 `IntegrityError`）
#: ⇒ 未处理就是 500 ⇒ 必须在边界上先拒。
_MAX_ACTOR_LENGTH = 64


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sourceSnapshot(binding: ProcurementSourceBinding) -> dict[str, Any]:
    """源绑定的审计快照（before / after 共用）。"""
    return {
        "classId": binding.class_id,
        "datasourceId": binding.datasource_id,
        "schemaName": binding.schema_name,
        "sourceTable": binding.source_table,
        "bindingRole": binding.binding_role,
        "filterExpression": binding.filter_expression,
        "filterAllowlist": list(binding.filter_allowlist or []),
        "isCandidate": binding.is_candidate,
        "confirmedBy": binding.confirmed_by,
        "confirmedTime": binding.confirmed_time.isoformat() if binding.confirmed_time else None,
        "validFrom": binding.valid_from.isoformat() if binding.valid_from else None,
        "validTo": binding.valid_to.isoformat() if binding.valid_to else None,
    }


def _propertySnapshot(binding: ProcurementPropertyBinding) -> dict[str, Any]:
    """属性绑定的审计快照。"""
    return {
        "sourceBindingId": binding.source_binding_id,
        "propertyId": binding.property_id,
        "sourceColumn": binding.source_column,
        "keyOrdinal": binding.key_ordinal,
        "transformExpression": binding.transform_expression,
        "isCandidate": binding.is_candidate,
        "confirmedBy": binding.confirmed_by,
        "confirmedTime": binding.confirmed_time.isoformat() if binding.confirmed_time else None,
        "validFrom": binding.valid_from.isoformat() if binding.valid_from else None,
        "validTo": binding.valid_to.isoformat() if binding.valid_to else None,
    }


class ProcurementBindingService:
    """采购源绑定的查询与写入。"""

    def __init__(self, audit: AuditService | None = None) -> None:
        self._audit = audit or AuditService()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def listSourceBindings(
        self,
        session: AsyncSession,
        *,
        classId: int | None = None,
        datasourceId: int | None = None,
        effectiveOnly: bool = False,
    ) -> list[ProcurementSourceBinding]:
        """列出源绑定。

        `effectiveOnly=True` 是**唯一允许被读路径消费的视图**（D-3）：
        只返回「已确认且当前生效」的行 —— 候选与已失效行一律不出现。
        """
        stmt = select(ProcurementSourceBinding)
        if classId is not None:
            stmt = stmt.where(ProcurementSourceBinding.class_id == classId)
        if datasourceId is not None:
            stmt = stmt.where(ProcurementSourceBinding.datasource_id == datasourceId)
        if effectiveOnly:
            stmt = stmt.where(
                ProcurementSourceBinding.is_candidate.is_(False),
                ProcurementSourceBinding.valid_to.is_(None),
            )
        result = await session.execute(stmt.order_by(ProcurementSourceBinding.id))
        return list(result.scalars().all())

    async def listEffectivePropertyBindings(
        self, session: AsyncSession
    ) -> list[ProcurementPropertyBinding]:
        """列出**生效**的属性绑定：自身已确认且生效，且**父源绑定**已确认且生效。

        两条父状态谓词的可观测性**不同**：

        - 「父必须已确认」——**可观测**：子可以先于父被确认（属性确认只要求父仍是
          候选态）⇒ 此时父未确认 ⇒ 整组映射还不是结论 ⇒ 子不得进入生效视图。
        - 「父必须未失效」——**冗余防御**：父失效时子由**级联失效**一并作废 ⇒
          删掉这条谓词行为不变 ⇒ 变异测试对它必然 MISSED（预期，不是假绿）。
        """
        stmt = (
            select(ProcurementPropertyBinding)
            .join(
                ProcurementSourceBinding,
                ProcurementSourceBinding.id == ProcurementPropertyBinding.source_binding_id,
            )
            .where(
                ProcurementPropertyBinding.is_candidate.is_(False),
                ProcurementPropertyBinding.valid_to.is_(None),
                ProcurementSourceBinding.is_candidate.is_(False),
                ProcurementSourceBinding.valid_to.is_(None),
            )
            .order_by(ProcurementPropertyBinding.id)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def getSourceBinding(
        self, session: AsyncSession, id: int
    ) -> ProcurementSourceBinding:
        """按 id 取源绑定；不存在抛 NotFoundError。"""
        binding = await session.get(ProcurementSourceBinding, id)
        if binding is None:
            raise NotFoundError(f"采购源绑定 id={id} 不存在")
        return binding

    async def listPropertyBindings(
        self, session: AsyncSession, sourceBindingId: int
    ) -> list[ProcurementPropertyBinding]:
        """列出某源绑定下的全部属性绑定（含候选与已失效）。"""
        result = await session.execute(
            select(ProcurementPropertyBinding)
            .where(ProcurementPropertyBinding.source_binding_id == sourceBindingId)
            .order_by(ProcurementPropertyBinding.id)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # 视图辅助（批量取名 / 计数，避免 N+1）
    # ------------------------------------------------------------------
    async def classNameMap(
        self, session: AsyncSession, classIds: list[int]
    ) -> dict[int, str]:
        """本体类 id → 类名（批量）。"""
        if not classIds:
            return {}
        result = await session.execute(
            select(OntologyClass.id, OntologyClass.class_name).where(
                OntologyClass.id.in_(classIds)
            )
        )
        return {row[0]: row[1] for row in result.all()}

    async def propertyNameMap(
        self, session: AsyncSession, propertyIds: list[int]
    ) -> dict[int, str]:
        """本体属性 id → 属性名（批量）。"""
        if not propertyIds:
            return {}
        result = await session.execute(
            select(OntologyProperty.id, OntologyProperty.property_name).where(
                OntologyProperty.id.in_(propertyIds)
            )
        )
        return {row[0]: row[1] for row in result.all()}

    async def propertyCountMap(
        self,
        session: AsyncSession,
        bindingIds: list[int],
        *,
        onlyEffective: bool = False,
    ) -> dict[int, int]:
        """源绑定 id → 其属性绑定条数（批量）。

        `onlyEffective=True` 只数**已确认且未失效**的属性绑定。
        ⚠️ 登录即可访问的 `/effective` 视图**必须**用它 —— 否则候选与已失效的
        行会通过「条数」这个侧面泄漏给普通用户（D-3：未经确认的候选不得被
        读路径消费，含间接侧面）。
        """
        if not bindingIds:
            return {}
        stmt = select(
            ProcurementPropertyBinding.source_binding_id,
            func.count(ProcurementPropertyBinding.id),
        ).where(ProcurementPropertyBinding.source_binding_id.in_(bindingIds))
        if onlyEffective:
            stmt = stmt.where(
                ProcurementPropertyBinding.is_candidate.is_(False),
                ProcurementPropertyBinding.valid_to.is_(None),
            )
        result = await session.execute(
            stmt.group_by(ProcurementPropertyBinding.source_binding_id)
        )
        return {row[0]: row[1] for row in result.all()}

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def createSourceBinding(
        self, session: AsyncSession, dto: SourceBindingCreate, *, actor: str
    ) -> ProcurementSourceBinding:
        """新增**候选**源绑定。

        校验：类存在 / 数据源存在 / 角色合法 / 白名单条目是合法列标识符 /
        过滤表达式在白名单内。
        唯一索引冲突（同键位已有未失效行）⇒ `ConflictError`。
        """
        _assertActorLength(actor)
        validateBindingRole(dto.binding_role)
        validateAllowlistItems(dto.filter_allowlist)
        validateFilterExpression(dto.filter_expression, dto.filter_allowlist)
        await self._assertClassExists(session, dto.class_id)
        await self._assertDataSourceExists(session, dto.datasource_id)

        binding = ProcurementSourceBinding(
            class_id=dto.class_id,
            datasource_id=dto.datasource_id,
            schema_name=dto.schema_name,
            source_table=dto.source_table,
            binding_role=dto.binding_role,
            filter_expression=_normalizeExpression(dto.filter_expression),
            filter_allowlist=list(dto.filter_allowlist),
            is_candidate=True,
            created_by=actor,
        )
        session.add(binding)
        await self._flushOrConflict(session, "同一键位已有生效的源绑定")
        await self._audit.record(
            session,
            entity_type=SOURCE_ENTITY,
            entity_id=binding.id,
            action="CREATE",
            actor=actor,
            after=_sourceSnapshot(binding),
        )
        await self._commitOrTranslate(session, "写入源绑定失败")
        await session.refresh(binding)
        return binding

    async def createPropertyBinding(
        self,
        session: AsyncSession,
        sourceBindingId: int,
        dto: PropertyBindingCreate,
        *,
        actor: str,
    ) -> ProcurementPropertyBinding:
        """在源绑定下新增**候选**属性绑定。

        校验：① 父源绑定处于**候选态**（已确认 ⇒ 子集合已冻结；已失效 ⇒ 只能读）；
        ② 属性必须属于该源绑定对应的类（跨类绑定会让读路径错位）；
        ③ 复合键序号在该绑定内不重复（库层部分唯一索引兜底，这里给可读文案）；
        ④ 列级变换表达式必须过闸（与过滤表达式同级）。
        """
        _assertActorLength(actor)
        source = await self._lockSourceBinding(session, sourceBindingId)
        _assertParentEditable(source)
        validateColumnName(dto.source_column)
        validateTransformExpression(dto.transform_expression, dto.source_column)
        await self._assertPropertyBelongsToClass(session, dto.property_id, source.class_id)
        await self._assertKeyOrdinalFree(session, sourceBindingId, dto.key_ordinal)

        binding = ProcurementPropertyBinding(
            source_binding_id=sourceBindingId,
            property_id=dto.property_id,
            source_column=dto.source_column,
            key_ordinal=dto.key_ordinal,
            transform_expression=_normalizeExpression(dto.transform_expression),
            is_candidate=True,
            created_by=actor,
        )
        session.add(binding)
        await self._flushOrConflict(session, "同一属性或键序号已有生效的属性绑定")
        await self._audit.record(
            session,
            entity_type=PROPERTY_ENTITY,
            entity_id=binding.id,
            action="CREATE",
            actor=actor,
            after=_propertySnapshot(binding),
        )
        await self._commitOrTranslate(session, "写入属性绑定失败")
        await session.refresh(binding)
        return binding

    async def confirmSourceBinding(
        self, session: AsyncSession, id: int, *, actor: str
    ) -> ProcurementSourceBinding:
        """源绑定：候选 → 已确认（**版本冻结点**）。

        前置：① 未失效；② 尚未确认；③ 至少有一条**未失效**的属性绑定
        （`FILTER` 角色例外 —— 过滤源本来就不承载列映射）；④ **所有未失效的属性绑定
        都已确认**（否则生效视图会出现「父已确认、子还是候选」的空壳映射）；
        ⑤ 复合键序号从 1 起连续（**按未失效的绑定算** —— 已失效的序号不占位）。
        """
        _assertActorLength(actor)
        binding = await self._lockSourceBinding(session, id)
        if binding.valid_to is not None:
            raise ValidationError(f"源绑定 id={id} 已失效，不能确认")
        if not binding.is_candidate:
            raise ConflictError(f"源绑定 id={id} 已是确认态，不能重复确认")

        active = await self._activePropertyBindings(session, id)
        if binding.binding_role != "FILTER" and not active:
            raise ValidationError(f"源绑定 id={id} 尚无属性绑定，不能确认")
        validateKeyOrdinals([item.key_ordinal for item in active])
        pending = [item for item in active if item.is_candidate]
        if pending:
            raise ValidationError(
                f"源绑定 id={id} 的属性绑定尚未全部确认（待确认 id="
                f"{[item.id for item in pending]}），不能确认源绑定"
            )

        before = _sourceSnapshot(binding)
        binding.is_candidate = False
        binding.confirmed_by = actor
        binding.confirmed_time = _utcnow()
        await self._audit.record(
            session,
            entity_type=SOURCE_ENTITY,
            entity_id=binding.id,
            action="UPDATE",
            actor=actor,
            before=before,
            after=_sourceSnapshot(binding),
        )
        await self._commitOrTranslate(session, "确认源绑定失败")
        await session.refresh(binding)
        return binding

    async def confirmPropertyBinding(
        self,
        session: AsyncSession,
        sourceBindingId: int,
        id: int,
        *,
        actor: str,
    ) -> ProcurementPropertyBinding:
        """属性绑定：候选 → 已确认。

        前置：① 属于该源绑定（路径里的父 id 必须对得上 ⇒ 杜绝跨父确认）；
        ② 父源绑定仍是**候选态**且未失效（父已确认 ⇒ 子集合已冻结）；
        ③ 自身未失效、未确认；④ 确认后父绑定的复合键序号仍然连续。
        """
        _assertActorLength(actor)
        owner = await session.get(ProcurementPropertyBinding, id)
        if owner is None or owner.source_binding_id != sourceBindingId:
            raise NotFoundError(
                f"属性绑定 id={id} 不存在或不属于源绑定 id={sourceBindingId}"
            )

        source = await self._lockSourceBinding(session, sourceBindingId)
        _assertParentEditable(source)
        binding = await self._lockPropertyBinding(session, id)
        if binding.valid_to is not None:
            raise ValidationError(f"属性绑定 id={id} 已失效，不能确认")
        if not binding.is_candidate:
            raise ConflictError(f"属性绑定 id={id} 已是确认态，不能重复确认")

        active = await self._activePropertyBindings(session, sourceBindingId)
        validateKeyOrdinals([item.key_ordinal for item in active])

        before = _propertySnapshot(binding)
        binding.is_candidate = False
        binding.confirmed_by = actor
        binding.confirmed_time = _utcnow()
        await self._audit.record(
            session,
            entity_type=PROPERTY_ENTITY,
            entity_id=binding.id,
            action="UPDATE",
            actor=actor,
            before=before,
            after=_propertySnapshot(binding),
        )
        await self._commitOrTranslate(session, "确认属性绑定失败")
        await session.refresh(binding)
        return binding

    async def expireSourceBinding(
        self, session: AsyncSession, id: int, *, actor: str
    ) -> ProcurementSourceBinding:
        """软失效：写 `valid_to`（不删行 ⇒ 历史证据保留）。

        其属性绑定**一并失效** —— 否则读路径会看到「父已过期、子还生效」的孤儿映射；
        每个被级联失效的子行**也写审计**（改了行就要留痕）。
        """
        _assertActorLength(actor)
        binding = await self._lockSourceBinding(session, id)
        if binding.valid_to is not None:
            raise ConflictError(f"源绑定 id={id} 已失效，不能重复失效")
        before = _sourceSnapshot(binding)
        validTo = _expireAt(binding.valid_from)
        # 子行的有效期必须嵌套在父的之内 ⇒ 父的失效时刻可能被后移（见 `_expireProperties`）。
        validTo = await self._expireProperties(session, binding.id, validTo, actor)
        binding.valid_to = validTo
        await self._audit.record(
            session,
            entity_type=SOURCE_ENTITY,
            entity_id=binding.id,
            action="UPDATE",
            actor=actor,
            before=before,
            after=_sourceSnapshot(binding),
        )
        await self._commitOrTranslate(session, "失效源绑定失败")
        await session.refresh(binding)
        return binding

    async def expirePropertyBinding(
        self,
        session: AsyncSession,
        sourceBindingId: int,
        id: int,
        *,
        actor: str,
    ) -> ProcurementPropertyBinding:
        """属性绑定：软失效（写 `valid_to`）。

        前置与「确认属性绑定」同构：属于该源绑定、父仍是候选态且未失效、自身未失效。
        用途：绑错了列时**只失效这一条**，不必把整个源绑定作废重来。
        """
        _assertActorLength(actor)
        owner = await session.get(ProcurementPropertyBinding, id)
        if owner is None or owner.source_binding_id != sourceBindingId:
            raise NotFoundError(
                f"属性绑定 id={id} 不存在或不属于源绑定 id={sourceBindingId}"
            )

        source = await self._lockSourceBinding(session, sourceBindingId)
        _assertParentEditable(source)
        binding = await self._lockPropertyBinding(session, id)
        if binding.valid_to is not None:
            raise ConflictError(f"属性绑定 id={id} 已失效，不能重复失效")

        before = _propertySnapshot(binding)
        binding.valid_to = _expireAt(binding.valid_from)
        await self._audit.record(
            session,
            entity_type=PROPERTY_ENTITY,
            entity_id=binding.id,
            action="UPDATE",
            actor=actor,
            before=before,
            after=_propertySnapshot(binding),
        )
        await self._commitOrTranslate(session, "失效属性绑定失败")
        await session.refresh(binding)
        return binding

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _lockSourceBinding(
        self, session: AsyncSession, id: int
    ) -> ProcurementSourceBinding:
        """取源绑定并**加行锁**（`FOR UPDATE`）。

        为什么必须加锁：确认/失效是「读-判-写」，两个并发请求在 READ COMMITTED
        下会**同时**读到 `is_candidate = TRUE` ⇒ 双双通过「不能重复确认」的判定 ⇒
        后写者覆盖先写者的确认人（审计两条都在，行上只剩一个）。

        `populate_existing` ⇒ 强制用库里的最新值覆盖 identity map 里的旧值
        （否则拿到的是本会话早先读到的陈旧对象）。
        """
        stmt = (
            select(ProcurementSourceBinding)
            .where(ProcurementSourceBinding.id == id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        binding = (await session.execute(stmt)).scalar_one_or_none()
        if binding is None:
            raise NotFoundError(f"采购源绑定 id={id} 不存在")
        return binding

    async def _lockPropertyBinding(
        self, session: AsyncSession, id: int
    ) -> ProcurementPropertyBinding:
        """取属性绑定并**加行锁**（同 `_lockSourceBinding`）。"""
        stmt = (
            select(ProcurementPropertyBinding)
            .where(ProcurementPropertyBinding.id == id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        binding = (await session.execute(stmt)).scalar_one_or_none()
        if binding is None:
            raise NotFoundError(f"采购属性绑定 id={id} 不存在")
        return binding

    async def _activePropertyBindings(
        self, session: AsyncSession, sourceBindingId: int
    ) -> list[ProcurementPropertyBinding]:
        """源绑定下**未失效**的属性绑定（含候选）。

        键序号的连续性按这个集合算：已失效的绑定不再占位（否则「已失效的序号 1
        + 生效的序号 2」会被误判成连续）。
        """
        properties = await self.listPropertyBindings(session, sourceBindingId)
        return [item for item in properties if item.valid_to is None]

    async def _expireProperties(
        self,
        session: AsyncSession,
        sourceBindingId: int,
        validTo: datetime,
        actor: str,
    ) -> datetime:
        """把源绑定下的**生效**属性绑定一并软失效（每个被改的行都写审计）。

        返回**父绑定应当采用的失效时刻**：子行的有效期必须**嵌套在**父行的
        有效期之内（子不能比父活得久）。子行可能比父晚创建 ⇒ 它的最早合法失效
        时刻（`valid_from + 1s`）可能晚于父的失效时刻 ⇒ 此时要把父的失效时刻
        一起后移，否则管理台会出现「子比父活得久」。

        口径：父的失效时刻 = max(父原失效时刻, 所有生效子行的最早合法失效时刻)。
        """
        properties = await self.listPropertyBindings(session, sourceBindingId)
        effectiveValidTo = validTo
        for item in properties:
            if item.valid_to is not None:
                continue
            effectiveValidTo = max(effectiveValidTo, _expireAt(item.valid_from))
        for item in properties:
            if item.valid_to is not None:
                continue
            before = _propertySnapshot(item)
            item.valid_to = max(validTo, _expireAt(item.valid_from))
            await self._audit.record(
                session,
                entity_type=PROPERTY_ENTITY,
                entity_id=item.id,
                action="UPDATE",
                actor=actor,
                before=before,
                after=_propertySnapshot(item),
            )
        return effectiveValidTo

    async def _assertClassExists(self, session: AsyncSession, classId: int) -> None:
        found = await session.get(OntologyClass, classId)
        if found is None:
            raise ValidationError(f"本体类 id={classId} 不存在")

    async def _assertDataSourceExists(self, session: AsyncSession, datasourceId: int) -> None:
        found = await session.get(DataSource, datasourceId)
        if found is None:
            raise ValidationError(f"数据源 id={datasourceId} 不存在")

    async def _assertPropertyBelongsToClass(
        self, session: AsyncSession, propertyId: int, classId: int
    ) -> None:
        """属性必须挂在该源绑定对应的类上（跨类绑定会让读路径错位）。"""
        found = await session.get(OntologyProperty, propertyId)
        if found is None:
            raise ValidationError(f"本体属性 id={propertyId} 不存在")
        if found.class_id != classId:
            raise ValidationError(
                f"本体属性 id={propertyId} 不属于类 id={classId}，不能绑到该源绑定"
            )

    async def _assertKeyOrdinalFree(
        self, session: AsyncSession, sourceBindingId: int, keyOrdinal: int | None
    ) -> None:
        """键序号在该绑定内不得重复，且**前缀必须齐全**。

        为什么还要查前缀：库层只保证「不重复」（部分唯一索引）⇒ 可以先建序号 3
        再建序号 1 ⇒ 中间缺 2 ⇒ 这个源绑定**永远无法被确认**（确认那一刻的连续性
        闸门必然拒）⇒ 用户在「建绑定」时得不到任何提示，要等到「确认」才撞墙。
        建的时候就把前缀查清楚 ⇒ 错在当场。

        口径与连续性闸门一致：**按未失效的绑定算**（已失效的序号不占位）。
        """
        if keyOrdinal is None:
            return
        stmt = select(ProcurementPropertyBinding.key_ordinal).where(
            ProcurementPropertyBinding.source_binding_id == sourceBindingId,
            ProcurementPropertyBinding.valid_to.is_(None),
            ProcurementPropertyBinding.key_ordinal.is_not(None),
        )
        present = {row[0] for row in (await session.execute(stmt)).all()}
        if keyOrdinal in present:
            raise ConflictError(f"键序号 {keyOrdinal} 在该绑定内已被占用")
        missing = [number for number in range(1, keyOrdinal) if number not in present]
        if missing:
            raise ValidationError(
                f"键序号 {keyOrdinal} 要求序号 {missing} 先存在"
                "（复合键序号必须从 1 起连续）"
            )

    async def _flushOrConflict(self, session: AsyncSession, message: str) -> None:
        """flush 并把数据库错误翻译成领域错误。

        - `IntegrityError` ⇒ 按**约束类型**分流（唯一 ⇒ 409；CHECK / 外键 / 非空 ⇒ 422）；
        - 其它 `DBAPIError`（值越界 / 长度超限 / 类型不符）⇒ `ValidationError`（422）。

        ⚠️ 冲突后必须 rollback —— 否则 session 处于「事务已中止」状态，
        后续任何查询都会抛 `PendingRollbackError`。
        """
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            raise _classifyIntegrityError(error, message) from error
        except DBAPIError as error:
            await session.rollback()
            logger.warning("采购绑定写入被数据库拒绝：%s", error, exc_info=True)
            raise ValidationError(f"{message}（{_dbErrorHint(error)}）") from error

    async def _commitOrTranslate(self, session: AsyncSession, message: str) -> None:
        """commit 并把数据库错误翻译成领域错误。

        为什么不能裸 `session.commit()`：**延迟约束**（外键、部分唯一索引、
        CHECK）在 flush 时可能还不触发，而是拖到 commit ⇒ 裸 commit 抛出的
        `IntegrityError` 会一路冒到 FastAPI 的兜底处理 ⇒ 用户看到 **500**
        而不是可读的 409 / 422。
        """
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise _classifyIntegrityError(error, message) from error
        except DBAPIError as error:
            await session.rollback()
            logger.warning("采购绑定提交被数据库拒绝：%s", error, exc_info=True)
            raise ValidationError(f"{message}（{_dbErrorHint(error)}）") from error


def _classifyIntegrityError(error: IntegrityError, message: str) -> Exception:
    """把 `IntegrityError` 按**约束类型**翻译成领域错误。

    - **唯一约束**（`uq_*` / SQLSTATE `23505`）⇒ `ConflictError`（409）——
      「同键位已有生效行」是**可预期的冲突**，客户端换版本或稍后重试即可；
    - **CHECK 约束**（`ck_*` / `23514`）、**外键**（`23503`）、**非空**（`23502`）
      ⇒ `ValidationError`（422）—— 是**这次写入的值不合法**，重试无用。

    ⚠️ 不能一律当 409：库层 CHECK（候选 ⇔ 无确认人、有效期方向等）触发时，
    用户改数据就能解决 ⇒ 报 409 会让客户端以为「等一会儿再试」。

    ⚠️ 约束名**不发给用户**（那会泄漏库表结构）⇒ 只进服务端日志。
    """
    orig = getattr(error, "orig", error)
    constraintName = (getattr(orig, "constraint_name", "") or "").lower()
    sqlstate = (getattr(orig, "sqlstate", "") or "").strip()
    isUnique = constraintName.startswith("uq_") or sqlstate == "23505"
    if isUnique:
        logger.info(
            "采购绑定唯一约束冲突：constraint=%s sqlstate=%s", constraintName or "?", sqlstate or "?"
        )
        return ConflictError(f"{message}（唯一约束冲突）")
    logger.warning(
        "采购绑定完整性约束被触发：constraint=%s sqlstate=%s",
        constraintName or "?",
        sqlstate or "?",
    )
    return ValidationError(f"{message}（{_dbErrorHint(error)}）")


def _dbErrorHint(error: DBAPIError) -> str:
    """把数据库异常翻译成**不给用户看内部结构**的可读提示。

    细节（异常原文）只进服务端日志 ⇒ 用户看不到 SQL / 表名 / 参数。
    """
    text = str(getattr(error, "orig", error)).lower()
    if "too long" in text or "character varying" in text:
        return "字段长度超限"
    if "out of range" in text or "invalid input" in text:
        return "数值超出取值范围"
    if "not null" in text or "not-null" in text or "null value" in text:
        return "缺少必填字段"
    if "check constraint" in text:
        return "数据不满足库层约束"
    if "foreign key" in text:
        return "关联对象不存在"
    return "数据库拒绝了该值"


def _normalizeExpression(expression: str | None) -> str | None:
    """纯空白表达式一律归一成 `NULL`。

    为什么：库里 `filter_expression` 是可空列 ⇒ 「空表达式」与「没有表达式」
    应当是同一个状态；存成空串会让读路径在拼 WHERE 时多出一个恒真条件，
    也让「这条绑定到底有没有过滤」变得不可判定。
    """
    if expression is None:
        return None
    return expression if expression.strip() else None


def _assertActorLength(actor: str) -> None:
    """操作用户标识长度必须能落库（`created_by` / `confirmed_by` 是 `String(64)`）。

    超长 ⇒ PostgreSQL 抛 `StringDataRightTruncationError`（**不是** `IntegrityError`）
    ⇒ 未处理就是 500 ⇒ 必须在边界上先拒（422）。
    """
    if len(actor or "") > _MAX_ACTOR_LENGTH:
        raise ValidationError(
            f"操作用户标识过长（{len(actor)} > {_MAX_ACTOR_LENGTH} 字符）"
        )


def _assertParentEditable(source: ProcurementSourceBinding) -> None:
    """父源绑定必须处于**候选态且未失效**才能改它的子绑定。

    - 已失效 ⇒ 只能读（否则会出现「父已过期、子还在生效」的孤儿映射）；
    - 已确认 ⇒ 子集合**已冻结**（版本冻结点）⇒ 要改绑必须先失效源绑定再建新版本。
    """
    if source.valid_to is not None:
        raise ValidationError(f"源绑定 id={source.id} 已失效，不能修改其属性绑定")
    if not source.is_candidate:
        raise ConflictError(
            f"源绑定 id={source.id} 已确认，其属性绑定已冻结；"
            "要改绑请先失效该源绑定，再建新版本"
        )


def _expireAt(validFrom: datetime | None) -> datetime:
    """失效时刻：至少比生效时刻晚 1 秒（否则撞 `valid_to > valid_from` 的 CHECK）。"""
    now = _utcnow()
    if validFrom is None:
        return now
    return max(now, validFrom + _EXPIRE_GAP)
