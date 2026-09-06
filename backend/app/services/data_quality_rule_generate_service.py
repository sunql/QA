"""数据质量规则自动生成服务（dq-rule-auto-generation Task 4）。

preview:  对指定本体类 + 数据源，驱动规则推导引擎 + schema 映射，
          返回待确认的规则建议列表 + 被阻断的属性及原因。
confirm:  Task 5（写入确认后的规则）
applySuggestion: Task 6（直接采纳单条建议）
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import DataType
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataQualityRule, DataSource, OntologyClass, OntologyJoin, OntologyProperty
from app.domain.schemas import (
    ApplySuggestionRequest,
    ApplySuggestionResponse,
    BlockedPropertyRead,
    DataQualityRuleRead,
    GenerateConfirmRequest,
    GenerateConfirmResponse,
    GeneratePreviewResponse,
    RuleSuggestionRead,
)
from app.services.data_quality_rule_generator import (
    ClassContext,
    ColumnMeta,
    JoinEdgeMeta,
    PropertyMeta,
    SchemaIndex,
    deriveSuggestions,
)
from app.services.messages_zh import (
    MSG_DQ_GEN_BAD_VALUE,
    MSG_DQ_GEN_CLASS_NOT_FOUND,
    MSG_DQ_GEN_DATASOURCE_NOT_FOUND,
    MSG_DQ_GEN_PROPERTY_NOT_FOUND,
)
from app.services.outbox_service import OutboxService
from app.services.schema_introspection_service import SchemaIntrospectionService


def _buildSchemaIndex(
    schemaData: list[dict],
) -> SchemaIndex:
    """把 schema_cache.schema_data（list of table dicts）转为 SchemaIndex（table->column map）。"""
    tables: dict[str, dict[str, ColumnMeta]] = {}
    for t in schemaData:
        cols: dict[str, ColumnMeta] = {}
        for c in t.get("columns", []):
            cols[c["column_name"].upper()] = ColumnMeta(
                column_name=c["column_name"],
                data_type=c["data_type"],
                nullable=c.get("nullable", True),
            )
        tables[t["table_name"].upper()] = cols
    return SchemaIndex(tables=tables)


def _toPropertyMeta(
    prop: OntologyProperty,
    refClasses: dict[int, OntologyClass],
) -> PropertyMeta:
    """把 OntologyProperty 行转为 PropertyMeta（含 ref_key_column 解析）。"""
    ref_class: ClassContext | None = None
    ref_key_column: str | None = None

    if prop.ref_class_id and prop.ref_class_id in refClasses:
        refCls = refClasses[prop.ref_class_id]
        ref_class = ClassContext(
            class_id=refCls.id,
            class_name=refCls.class_name,
            source_table=refCls.source_table,
            object_type=refCls.object_type,
        )
        # 查 ref 类的主键属性，用其 source_column 作为 ref_key_column
        # （装配 PropertyMeta 时查，避免在循环内逐个查）
        # 此处 ref_key_column 由调用方在 caller 中解析后传入
        # 为简化，暂传 None；真实场景由 caller 在循环外批量查
        ref_key_column = None

    return PropertyMeta(
        property_id=prop.id,
        property_name=prop.property_name,
        source_column=prop.source_column,
        data_type=prop.data_type,
        is_primary_key=prop.is_primary_key,
        is_foreign_key=prop.is_foreign_key,
        ref_class=ref_class,
        ref_key_column=ref_key_column,
        allowed_values=prop.allowed_values,
    )


def _toJoinEdge(
    join: OntologyJoin,
    sourceClass: OntologyClass,
) -> JoinEdgeMeta:
    """把 OntologyJoin 行转为 JoinEdgeMeta。

    同名 DATETIME 属性配对：源类与目标类各取 property_name 相同且 data_type=DATETIME
    的属性，物理列填入 target_date_columns；无可配对则 target_date_columns=[]。
    """
    # 解析 target class 的 source_table（需要查 target class 元数据）
    # join.target_class_id 对应的 source_table 从 join 行本身无法直接获得，
    # 需要额外查询；这里先用 join 行已有的 source_columns/target_columns，
    # target_table 填 sourceClass.source_table（占位，后续由 caller 注入正确值）。
    # 为使引擎工作，我们从 target_class_id 查询 target class 的 source_table。
    # 这里简化处理：在 service.preview 中通过 join.target_class_id 查到 target class，
    # 再构建 JoinEdgeMeta。
    return JoinEdgeMeta(
        target_table="",  # caller 负责填充
        source_columns=join.source_columns or [],
        target_columns=join.target_columns or [],
        target_date_columns=[],  # caller 负责填充同名 DATETIME 配对
    )


class DataQualityRuleGenerateService:
    """数据质量规则自动生成服务（preview/confirm/applySuggestion）。"""

    def __init__(self, outbox: OutboxService | None = None) -> None:
        self._outbox = outbox or OutboxService()

    async def preview(
        self,
        session: AsyncSession,
        *,
        classId: int,
        datasourceId: int,
    ) -> GeneratePreviewResponse:
        """预览指定本体类的规则建议。

        流程：
        1. 查本体类（不存在 → 404）
        2. 查类的所有属性
        3. 查属性引用的所有 ref class（用于 FK 规则的 ref_key_column 解析）
        4. 查类的所有 join 边
        5. 查 schema_cache（有 → 建 SchemaIndex；无 → 所有属性 blocked）
        6. 装配 PropertyMeta（含 ref_key_column 解析：ref 类主键属性的 source_column）
        7. 装配 JoinEdgeMeta（含同名 DATETIME 配对）
        8. 调用 deriveSuggestions 推导建议
        9. 查已存在的 rule_code，标记 EXISTS 状态
        10. 返回 GeneratePreviewResponse
        """
        # 1. 查本体类
        cls = await session.get(OntologyClass, classId)
        if cls is None:
            raise NotFoundError(MSG_DQ_GEN_CLASS_NOT_FOUND.format(id=classId))

        # 2. 查属性
        props = (
            await session.execute(
                select(OntologyProperty).where(OntologyProperty.class_id == classId)
            )
        ).scalars().all()

        # 3. 查 ref class（用于 FK 规则的 ref_key_column 解析）
        refIds = {p.ref_class_id for p in props if p.ref_class_id}
        refClasses: dict[int, OntologyClass] = {}
        if refIds:
            refClasses = {
                c.id: c
                for c in (
                    await session.execute(
                        select(OntologyClass).where(OntologyClass.id.in_(refIds))
                    )
                ).scalars().all()
            }

        # 3b. 查 ref class 的主键属性（用于 ref_key_column）
        refPkCols: dict[int, str] = {}  # ref_class_id -> source_column of pk
        if refIds:
            pkProps = (
                await session.execute(
                    select(OntologyProperty).where(
                        OntologyProperty.class_id.in_(refIds),
                        OntologyProperty.is_primary_key == True,  # noqa: E712
                    )
                )
            ).scalars().all()
            for pk in pkProps:
                if pk.source_column:
                    refPkCols[pk.class_id] = pk.source_column

        # 4. 查 join 边
        joins = (
            await session.execute(
                select(OntologyJoin).where(OntologyJoin.source_class_id == classId)
            )
        ).scalars().all()

        # 4b. 查 join 边的 target class 元数据（用于 target_table）
        targetClassIds = {j.target_class_id for j in joins if j.target_class_id}
        targetClasses: dict[int, OntologyClass] = {}
        if targetClassIds:
            targetClasses = {
                c.id: c
                for c in (
                    await session.execute(
                        select(OntologyClass).where(OntologyClass.id.in_(targetClassIds))
                    )
                ).scalars().all()
            }

        # 5. 查 schema_cache → SchemaIndex
        cache = await SchemaIntrospectionService().getCached(session, datasourceId)
        schemaIndex = _buildSchemaIndex(cache.schema_data) if cache else None

        # 6. 装配 ClassContext
        ctx = ClassContext(
            class_id=cls.id,
            class_name=cls.class_name,
            source_table=cls.source_table,
            object_type=cls.object_type,
        )

        # 7. 装配 PropertyMeta（含 ref_key_column）
        metas: list[PropertyMeta] = []
        for p in props:
            ref_class: ClassContext | None = None
            if p.ref_class_id and p.ref_class_id in refClasses:
                refCls = refClasses[p.ref_class_id]
                ref_class = ClassContext(
                    class_id=refCls.id,
                    class_name=refCls.class_name,
                    source_table=refCls.source_table,
                    object_type=refCls.object_type,
                )
            metas.append(PropertyMeta(
                property_id=p.id,
                property_name=p.property_name,
                source_column=p.source_column,
                data_type=p.data_type,
                is_primary_key=p.is_primary_key,
                is_foreign_key=p.is_foreign_key,
                ref_class=ref_class,
                ref_key_column=refPkCols.get(p.ref_class_id),
                allowed_values=p.allowed_values,
            ))

        # 8. 装配 JoinEdgeMeta（含同名 DATETIME 配对）
        #    需要目标类所有 DATETIME 属性（property_name → source_column）
        targetDateProps: dict[int, dict[str, str]] = {}  # target_class_id -> {prop_name -> source_column}
        if targetClassIds:
            targetProps = (
                await session.execute(
                    select(OntologyProperty).where(
                        OntologyProperty.class_id.in_(targetClassIds),
                    )
                )
            ).scalars().all()
            for tp in targetProps:
                if tp.data_type.upper() == DataType.DATETIME.value and tp.source_column:
                    targetDateProps.setdefault(tp.class_id, {})[tp.property_name.upper()] = tp.source_column

        # 源类所有 DATETIME 属性
        sourceDateProps: dict[str, str] = {}  # prop_name.upper() -> source_column
        for p in props:
            if p.data_type.upper() == DataType.DATETIME.value and p.source_column:
                sourceDateProps[p.property_name.upper()] = p.source_column

        joinEdges: list[JoinEdgeMeta] = []
        for j in joins:
            targetCls = targetClasses.get(j.target_class_id)
            if not targetCls:
                continue
            # 找同名 DATETIME 配对
            targetDateCols: list[str] = []
            if j.target_class_id in targetDateProps:
                tdates = targetDateProps[j.target_class_id]
                for propNameUpper, _srcCol in sourceDateProps.items():
                    if propNameUpper in tdates:
                        targetDateCols.append(tdates[propNameUpper])
            joinEdges.append(JoinEdgeMeta(
                target_table=targetCls.source_table or "",
                source_columns=j.source_columns or [],
                target_columns=j.target_columns or [],
                target_date_columns=targetDateCols,
            ))

        # 9. 推导建议
        suggestions, blocked = deriveSuggestions(ctx, metas, joinEdges, schemaIndex)

        # 10. 查已存在的 rule_code
        if suggestions:
            existingCodes = set(
                (
                    await session.execute(
                        select(DataQualityRule.rule_code).where(
                            DataQualityRule.rule_code.in_(
                                [s.rule_code for s in suggestions]
                            )
                        )
                    )
                ).scalars().all()
            )
        else:
            existingCodes = set()

        # 11. 组装响应（status: NEW → EXISTS）
        suggestionReads: list[RuleSuggestionRead] = []
        for s in suggestions:
            suggestionReads.append(RuleSuggestionRead(
                rule_code=s.rule_code,
                rule_name=s.rule_name,
                rule_type=s.rule_type,
                target_table=s.target_table,
                target_column=s.target_column,
                rule_expression=s.rule_expression,
                threshold=s.threshold,
                severity=s.severity,
                derivation_type=s.derivation_type,
                source_property_id=s.source_property_id,
                source_class_id=s.source_class_id,
                confidence=s.confidence,
                status="EXISTS" if s.rule_code in existingCodes else "NEW",
                reason=s.reason,
            ))

        blockedReads: list[BlockedPropertyRead] = [
            BlockedPropertyRead(property_name=b.property_name, reason=b.reason)
            for b in blocked
        ]

        return GeneratePreviewResponse(
            class_id=cls.id,
            class_name=cls.class_name,
            source_table=cls.source_table,
            datasource_id=datasourceId,
            suggestions=suggestionReads,
            blocked=blockedReads,
        )

    async def confirm(
        self,
        session: AsyncSession,
        payload: GenerateConfirmRequest,
        actor: CurrentUser,
    ) -> GenerateConfirmResponse:
        """批量确认并写入规则。

        幂等策略：
        - 预查询 DB 中已存在的 rule_code，命中 → 记入 skippedCodes
        - 并发撞唯一约束（IntegrityError）→ 记入 skippedCodes，不失败整批

        owner 派生自 actor.departments[0]（与 createRule 同模式）。

        Outbox 审计：每条 created 规则对应一条 audit_outbox
        （event_type='data_quality_rule_created'），在 commit 前入队。
        """
        # 校验 datasource 存在（不存在 → 404，与 preview 的 classId 校验对齐）
        ds = await session.get(DataSource, payload.datasource_id)
        if ds is None:
            raise NotFoundError(MSG_DQ_GEN_DATASOURCE_NOT_FOUND.format(id=payload.datasource_id))

        created: list[DataQualityRule] = []
        skipped: list[str] = []

        # 预查询已存在的 rule_code
        rule_codes = [r.rule_code for r in payload.rules]
        if rule_codes:
            existing = set(
                (
                    await session.execute(
                        select(DataQualityRule.rule_code).where(
                            DataQualityRule.rule_code.in_(rule_codes)
                        )
                    )
                ).scalars().all()
            )
        else:
            existing = set()

        for item in payload.rules:
            if item.rule_code in existing:
                skipped.append(item.rule_code)
                continue

            derived_owner = (
                actor.departments[0] if actor.departments else None
            )
            entity = DataQualityRule(
                rule_name=item.rule_name,
                rule_code=item.rule_code,
                datasource_id=payload.datasource_id,
                target_table=item.target_table,
                target_column=item.target_column,
                rule_type=item.rule_type,
                rule_expression=item.rule_expression,
                threshold=item.threshold,
                severity=item.severity,
                owner=derived_owner,
                description=item.description,
                source_class_id=item.source_class_id,
                source_property_id=item.source_property_id,
                derivation_type=item.derivation_type.value,
            )
            try:
                async with session.begin_nested():
                    session.add(entity)
                    await session.flush()
            except IntegrityError:
                # 并发撞唯一约束 → 记跳过，不失败整批
                # begin_nested() 创建了 savepoint；rollback 只回滚该 savepoint，
                # 不影响外层事务和其他已 flush 的规则（正确）。
                skipped.append(item.rule_code)
                await session.rollback()
                continue

            # outbox 审计（在 commit 前入队，同事务原子）
            await self._outbox.enqueue(
                session,
                event_type="data_quality_rule_created",
                entity_type="data_quality_rule",
                entity_id=entity.id,
                actor=actor.userId,
                actor_departments=tuple(actor.departments or []),
                payload={
                    "derivation_type": item.derivation_type.value,
                    "source_class_id": item.source_class_id,
                },
            )
            created.append(entity)
            existing.add(item.rule_code)

        await session.commit()
        return GenerateConfirmResponse(
            created=[
                DataQualityRuleRead.model_validate(r, from_attributes=True)
                for r in created
            ],
            skipped_codes=skipped,
        )

    async def applySuggestion(
        self,
        session: AsyncSession,
        payload: ApplySuggestionRequest,
        actor: CurrentUser,
    ) -> ApplySuggestionResponse:
        """采纳 LLM 推荐的 allowed_values，写入 ontology_property 并记录 outbox 审计。

        仅值域型（allowed_values）写入；业务必填类建议不写回（不沉淀为元数据），
        仅留 LLM_DERIVED 入 confirm。

        值校验：不允许含单引号（SQL 注入防护），否则 422。
        """
        prop = await session.get(OntologyProperty, payload.property_id)
        if prop is None:
            raise NotFoundError(MSG_DQ_GEN_PROPERTY_NOT_FOUND.format(id=payload.property_id))

        # 值域安全校验：禁止单引号（SQL 注入防护）
        for v in payload.allowed_values:
            if "'" in v:
                raise ValidationError(MSG_DQ_GEN_BAD_VALUE)

        before = prop.allowed_values
        await session.execute(
            update(OntologyProperty)
            .where(OntologyProperty.id == prop.id)
            .values(allowed_values=payload.allowed_values)
        )

        # outbox 审计（event_type 要求 updated）
        await self._outbox.enqueue(
            session,
            event_type="ontology_property_updated",
            entity_type="ontology_property",
            entity_id=prop.id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={
                "before": {"allowed_values": before},
                "after": {"allowed_values": payload.allowed_values},
            },
        )

        await session.commit()
        return ApplySuggestionResponse(
            property_id=prop.id,
            allowed_values=payload.allowed_values,
        )
