"""本地导入主服务。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import NotFoundError
from app.domain.models import DataSource
from app.domain.schemas import FilterSuggestions as FilterSuggestionsDto
from app.domain.schemas import (
    ImportErrorInfo,
    ImportExecuteRequest,
    ImportExecuteResponse,
    ImportPreviewResponse,
    ImportRuleConfig,
    LlmUsageInfo,
    OntologyClassCreate,
    OntologyJoinCreate,
    OntologyPropertyCreate,
    ProposedClass,
    ProposedJoin,
    ProposedProperty,
)
from app.services.import_conflict_resolver import ImportConflictResolver
from app.services.import_llm_enhancer import EnhancedSchemaResult, ImportLlmEnhancer
from app.services.import_rule_engine import ImportRuleEngine
from app.services.join_inference import infer_sage_x3_name_convention_joins
from app.services.ontology_service import OntologyService
from app.services.schema_introspection_service import SchemaIntrospectionService

logger = logging.getLogger(__name__)


class LocalImportService:
    def __init__(
        self,
        *,
        schema_service: SchemaIntrospectionService | None = None,
        ontology_service: OntologyService | None = None,
        rule_engine: ImportRuleEngine | None = None,
        conflict_resolver: ImportConflictResolver | None = None,
        llm_enhancer: ImportLlmEnhancer | None = None,
    ) -> None:
        self._schema_service = schema_service or SchemaIntrospectionService()
        self._ontology_service = ontology_service or OntologyService()
        self._rule_engine = rule_engine or ImportRuleEngine()
        self._conflict_resolver = conflict_resolver or ImportConflictResolver()
        self._llm_enhancer = llm_enhancer or ImportLlmEnhancer()

    async def build_preview(
        self,
        session: AsyncSession,
        datasource_id: int,
        rules: ImportRuleConfig,
        selected_tables: list[str] | None = None,
        selected_columns: dict[str, list[str]] | None = None,
        schema: str | None = None,
    ) -> ImportPreviewResponse:
        ds = await session.get(DataSource, datasource_id)
        if ds is None:
            raise NotFoundError(f"数据源 {datasource_id} 不存在")

        cache = await self._schema_service.introspectAndCache(session, ds, owner=schema)
        response = self._schema_service.buildResponse(cache)

        filtered = self._rule_engine.filter_tables(response.tables, rules.table_filter)
        filtered = self._apply_selected_tables(filtered, selected_tables)
        enhanced = await self._llm_enhancer.enhance_schema(
            filtered,
            generate_aliases=rules.llm_enhance_options.generate_aliases,
            generate_descriptions=rules.llm_enhance_options.generate_descriptions,
            detect_enums=rules.llm_enhance_options.detect_enums,
            suggest_filters=rules.llm_enhance_options.suggest_filters,
        )

        proposed_classes, proposed_properties, proposed_joins = self._build_proposals(
            enhanced, rules, filtered, selected_columns
        )
        # 分批导入：只保留两端都在白名单内的 join，避免对未导入的表生成悬空关联。
        if selected_tables:
            wanted = {t.strip().lower() for t in selected_tables if t and t.strip()}
            if wanted:
                proposed_joins = [
                    j
                    for j in proposed_joins
                    if (j.source_table or "").lower() in wanted
                    and (j.target_table or "").lower() in wanted
                ]

        existing_classes = await self._ontology_service.listClasses(session)
        existing_properties: list = []
        for cls in existing_classes:
            existing_properties.extend(
                await self._ontology_service.listPropertiesByClass(session, cls.id)
            )

        conflicts = self._conflict_resolver.detect_conflicts(
            [c.model_dump() for c in proposed_classes],
            proposed_properties,
            existing_classes,
            existing_properties,
        )

        return ImportPreviewResponse(
            datasource_id=datasource_id,
            proposed_classes=proposed_classes,
            proposed_joins=proposed_joins,
            conflicts=conflicts,
            filter_suggestions=FilterSuggestionsDto(
                recommended_blacklist_patterns=enhanced.filter_suggestions.exclude_patterns,
                excluded_tables=enhanced.filter_suggestions.exclude_tables,
            ),
            llm_usage=LlmUsageInfo(),
        )

    @staticmethod
    def _apply_selected_tables(
        tables: Sequence[Any],
        selected_tables: list[str] | None,
    ) -> list[Any]:
        """按表名白名单收窄预览范围（超大 schema 分批导入用）。

        传入 None 或空列表时返回原列表（不限制）。白名单与规则引擎过滤是 AND 关系：
        先按 nameBlacklistPatterns/临时表规则剔除，再仅保留白名单命中的表。
        表名匹配不区分大小写（Oracle 表名大写，用户可能输入小写）。返回新列表，不改输入。
        """
        if not selected_tables:
            return list(tables)
        wanted = {t.strip().lower() for t in selected_tables if t and t.strip()}
        if not wanted:
            return list(tables)
        return [t for t in tables if (t.table_name or "").lower() in wanted]

    def _build_proposals(
        self,
        enhanced: EnhancedSchemaResult,
        rules: ImportRuleConfig,
        original_tables: Sequence[Any],
        selected_columns: dict[str, list[str]] | None = None,
    ) -> tuple[list[ProposedClass], list[dict[str, Any]], list[ProposedJoin]]:
        enhanced_by_name = {t.name: t for t in enhanced.tables}
        col_whitelists = self._normalizeSelectedColumns(selected_columns)

        proposed_classes: list[ProposedClass] = []
        proposed_properties: list[dict[str, Any]] = []
        proposed_joins: list[ProposedJoin] = []

        for original in original_tables:
            table_name = original.table_name
            enhanced_table = enhanced_by_name.get(table_name)

            pk_set = set(original.primary_keys or [])
            fk_cols = {fk.column_name for fk in (original.foreign_keys or [])}
            enhanced_cols = (
                {c.name: c for c in enhanced_table.columns} if enhanced_table else {}
            )
            col_whitelist = col_whitelists.get((table_name or "").upper())

            properties: list[ProposedProperty] = []
            for col in original.columns:
                # 单表「部分属性导入」：仅保留白名单命中的列（未知列忽略）
                if col_whitelist is not None and col.column_name.upper() not in col_whitelist:
                    continue
                ec = enhanced_cols.get(col.column_name)
                mapped = self._rule_engine.map_data_type(
                    col.data_type, rules.type_mapping
                )
                prop = ProposedProperty(
                    source_column=col.column_name,
                    property_name=col.column_name,
                    property_alias=ec.alias if ec else None,
                    description=ec.description if ec else None,
                    data_type=mapped.value,
                    is_primary_key=col.column_name in pk_set,
                    is_foreign_key=col.column_name in fk_cols,
                    enum_values=ec.enum_values if ec else None,
                )
                properties.append(prop)
                proposed_properties.append(
                    {
                        "source_table": table_name,
                        "source_column": col.column_name,
                        "property_name": prop.property_name,
                    }
                )

            proposed_classes.append(
                ProposedClass(
                    source_table=table_name,
                    class_name=table_name,
                    class_alias=enhanced_table.alias if enhanced_table else None,
                    description=enhanced_table.description if enhanced_table else None,
                    properties=properties,
                )
            )

        proposed_joins = self._pruneJoinsOnUnimportedColumns(
            self._build_joins(original_tables, rules), col_whitelists
        )

        return proposed_classes, proposed_properties, proposed_joins

    @staticmethod
    def _pruneJoinsOnUnimportedColumns(
        joins: list[ProposedJoin],
        col_whitelists: dict[str, set[str]],
    ) -> list[ProposedJoin]:
        """丢弃引用到未导入列的 join（避免指向未落库的 property）。

        表未做列选（不在 whitelist 内）时视为全列导入，不受影响；表做列选时，
        仅当该 join 的所有 source/target 列都在白名单内才保留。返回新列表，不改输入。
        """
        kept: list[ProposedJoin] = []
        for j in joins:
            src_white = col_whitelists.get((j.source_table or "").upper())
            if src_white is not None and not all(
                c.upper() in src_white for c in (j.source_columns or [])
            ):
                continue
            tgt_white = col_whitelists.get((j.target_table or "").upper())
            if tgt_white is not None and not all(
                c.upper() in tgt_white for c in (j.target_columns or [])
            ):
                continue
            kept.append(j)
        return kept

    @staticmethod
    def _normalizeSelectedColumns(
        selected_columns: dict[str, list[str]] | None,
    ) -> dict[str, set[str]]:
        """把 selected_columns 归一化为 {表名大写: 列名大写集合}；入参为 None 返回空。

        返回新映射，不改输入。列名校验（大小写不敏感）：未知列名在调用处忽略，
        空表名/空列表条目剔除。
        """
        if not selected_columns:
            return {}
        out: dict[str, set[str]] = {}
        for table, columns in selected_columns.items():
            key = (table or "").strip().upper()
            if not key or not columns:
                continue
            out[key] = {c.strip().upper() for c in columns if c and c.strip()}
        return out

    @staticmethod
    def _build_joins(
        tables: Sequence[Any],
        rules: ImportRuleConfig,
    ) -> list[ProposedJoin]:
        """生成关联候选：声明外键 + Sage X3 列名约定（按规则开关），去重。

        返回新列表。声明 FK（declared_fk）与列名约定（name_convention）产出同一条
        边时仅保留首次（先声明后约定，二者等价无歧义）。输入不变。
        """
        joins: list[ProposedJoin] = []
        seen: set[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = set()

        def _add(j: ProposedJoin) -> None:
            key = (
                (j.source_table or "").upper(),
                tuple(c.upper() for c in (j.source_columns or [])),
                (j.target_table or "").upper(),
                tuple(c.upper() for c in (j.target_columns or [])),
            )
            if key not in seen:
                seen.add(key)
                joins.append(j)

        inference = rules.join_inference
        if inference.infer_declared_fk:
            for original in tables:
                for fk in original.foreign_keys or []:
                    _add(
                        ProposedJoin(
                            source_table=original.table_name,
                            source_columns=[fk.column_name],
                            target_table=fk.ref_table,
                            target_columns=[fk.ref_column],
                            join_type="INNER",
                            relation_type="foreign_key",
                            is_selected=True,
                            inferred_by="declared_fk",
                        )
                    )
        if inference.infer_name_convention:
            for j in infer_sage_x3_name_convention_joins(list(tables)):
                _add(j)
        return joins

    async def execute_import(
        self,
        session: AsyncSession,
        datasource_id: int,
        request: ImportExecuteRequest,
        created_by: str | None,
        actor: CurrentUser,
    ) -> ImportExecuteResponse:
        ds = await session.get(DataSource, datasource_id)
        if ds is None:
            raise NotFoundError(f"数据源 {datasource_id} 不存在")

        created_classes = 0
        created_properties = 0
        created_joins = 0
        errors: list[ImportErrorInfo] = []
        created_class_by_table: dict[str, int] = {}

        for proposed in request.confirmed_classes:
            if not proposed.is_selected:
                continue
            class_id, props_created, item_errors = await self._create_class_with_properties(
                session, proposed, created_by, actor
            )
            errors.extend(item_errors)
            created_properties += props_created
            if class_id is not None:
                created_classes += 1
                created_class_by_table[proposed.source_table] = class_id

        class_id_by_table = await self._build_table_to_class_id_map(
            session, created_class_by_table
        )

        for proposed_join in request.confirmed_joins:
            if not proposed_join.is_selected:
                continue
            try:
                source_id = class_id_by_table.get(proposed_join.source_table.lower())
                target_id = class_id_by_table.get(proposed_join.target_table.lower())
                if source_id is None or target_id is None:
                    raise ValueError(
                        f"无法为 join 找到对应类: "
                        f"{proposed_join.source_table} -> {proposed_join.target_table}"
                    )
                join_dto = OntologyJoinCreate(
                    source_class_id=source_id,
                    source_columns=proposed_join.source_columns,
                    target_class_id=target_id,
                    target_columns=proposed_join.target_columns,
                    join_type=proposed_join.join_type,
                    relation_type=proposed_join.relation_type,
                )
                await self._ontology_service.createJoin(
                    session,
                    join_dto,
                    actor=actor.userId,
                    actor_departments=(
                        ",".join(actor.departments) if actor.departments else None
                    ),
                )
                created_joins += 1
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                msg = str(exc) if str(exc) else type(exc).__name__
                logger.warning("创建 join 失败 %s: %s", proposed_join.source_table, exc)
                errors.append(
                    ImportErrorInfo(
                        type="join",
                        name=f"{proposed_join.source_table}->{proposed_join.target_table}",
                        message=msg,
                    )
                )

        skipped_conflicts = sum(
            1 for r in request.conflict_resolutions if r.action == "skip"
        )
        overwritten_conflicts = sum(
            1 for r in request.conflict_resolutions if r.action == "overwrite"
        )

        return ImportExecuteResponse(
            success=len(errors) == 0,
            created_classes=created_classes,
            created_properties=created_properties,
            created_joins=created_joins,
            skipped_conflicts=skipped_conflicts,
            overwritten_conflicts=overwritten_conflicts,
            errors=errors,
        )

    async def _create_class_with_properties(
        self,
        session: AsyncSession,
        proposed: ProposedClass,
        created_by: str | None,
        actor: CurrentUser,
    ) -> tuple[int | None, int, list[ImportErrorInfo]]:
        """创建单个类及其属性；逐项失败隔离（回滚会话，继续处理其余项）。

        每个 createClass/createProperty 内部自行 commit；DB 层失败会把会话留在
        pending rollback 状态，故在 except 中显式 rollback 恢复会话，避免后续项
        因 InvalidRequestError 级联失败。返回 (class_id, 属性成功数, 错误项)。
        """
        errors: list[ImportErrorInfo] = []
        class_dto = OntologyClassCreate(
            class_name=proposed.class_name,
            class_alias=proposed.class_alias,
            description=proposed.description,
            source_table=proposed.source_table,
            created_by=created_by,
        )
        try:
            created_class = await self._ontology_service.createClass(
                session,
                class_dto,
                actor=actor.userId,
                actor_departments=(
                    ",".join(actor.departments) if actor.departments else None
                ),
            )
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            msg = str(exc) if str(exc) else type(exc).__name__
            logger.warning("创建类失败 %s: %s", proposed.source_table, exc)
            errors.append(
                ImportErrorInfo(
                    type="class",
                    name=proposed.source_table,
                    message=msg,
                )
            )
            return None, 0, errors

        # 立即取 id：后续属性失败回滚会话会使 created_class 属性过期，
        # 延迟访问会触发同步惰性加载（MissingGreenlet），故先固化为普通 int。
        class_id = created_class.id
        props_created = 0
        for prop in proposed.properties:
            try:
                prop_dto = OntologyPropertyCreate(
                    class_id=class_id,
                    property_name=prop.property_name,
                    property_alias=prop.property_alias,
                    description=prop.description,
                    data_type=prop.data_type,
                    is_primary_key=prop.is_primary_key,
                    is_foreign_key=prop.is_foreign_key,
                    source_column=prop.source_column,
                )
                await self._ontology_service.createProperty(
                    session,
                    prop_dto,
                    actor=actor.userId,
                    actor_departments=(
                        ",".join(actor.departments) if actor.departments else None
                    ),
                )
                props_created += 1
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                msg = str(exc) if str(exc) else type(exc).__name__
                logger.warning(
                    "创建属性失败 %s.%s: %s",
                    proposed.source_table,
                    prop.property_name,
                    exc,
                )
                errors.append(
                    ImportErrorInfo(
                        type="property",
                        name=f"{proposed.source_table}.{prop.property_name}",
                        message=msg,
                    )
                )
        return class_id, props_created, errors

    async def _build_table_to_class_id_map(
        self,
        session: AsyncSession,
        created_class_by_table: dict[str, int],
    ) -> dict[str, int]:
        all_classes = await self._ontology_service.listClasses(session)
        table_to_id: dict[str, int] = {
            (c.source_table or "").lower(): c.id
            for c in all_classes
            if c.source_table
        }
        for table, class_id in created_class_by_table.items():
            table_to_id[table.lower()] = class_id
        return table_to_id
