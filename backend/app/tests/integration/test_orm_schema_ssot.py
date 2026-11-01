"""ORM 必须声明迁移建过的一切索引（否则 drift 报 extra_index，autogenerate 提议 DROP）。

2026-11-01 实测：真实库上有 33 个索引「迁移建了、ORM 没声明」，被 drift 判成
`extra_index` warning（刷了几个月启动日志）。本文件把这 33 条钉死。

33 = 32 个 `Index` + 1 个 `UniqueConstraint`：

- `uq_metric_source_triple` 在 DB 里是**约束支撑**的唯一索引
  （`pg_constraint.conindid` 命中），ORM 侧声明为 `UniqueConstraint`，
  它的名字**不在** `table.indexes` 里 ⇒ 单独断言，不进 `_REQUIRED_INDEXES`。
- 其余 32 条是普通 `Index`（含 3 条 partial unique）。

比较规则与 drift 工具一致（`app/infrastructure/schema_drift.py`）：
索引**先按名字配对**，名字相同才比**有序列元组**；任一侧是表达式
（ORM `sa_text("x DESC")` 或 DB `<expr>` 哨兵）就**跳过列比较**。
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint
from sqlalchemy.ext.asyncio import create_async_engine

from app.domain.models import Base
from app.infrastructure import schema_drift as _drift
from app.tests._pg_support import resolveTestDatabaseUrl

# 表 → ORM 必须声明的 Index 名（32 条）
_REQUIRED_INDEXES: dict[str, tuple[str, ...]] = {
    "agent_definition": ("ix_agent_definition_owner", "ix_agent_definition_status"),
    "agent_schedule": ("ix_agent_schedule_agent",),
    "audit_log": ("idx_audit_log_actor", "idx_audit_log_created_at"),
    "business_object": (
        "ix_business_object_header_class",
        "uq_business_object_header_class",
    ),
    "data_layer_rule": ("uq_layer_rule_manual", "uq_layer_rule_pattern"),
    "data_source": ("idx_datasource_default", "idx_datasource_name"),
    "document_catalog": (
        "ix_document_catalog_entity",
        "ix_document_catalog_security",
        "ix_document_catalog_type",
    ),
    "embedding_provider": ("uq_embedding_provider_active",),
    "entity_mapping": (
        "idx_entity_mapping_supplier_name_trgm",
        "ix_entity_mapping_enterprise_code",
        "ix_entity_mapping_source_code",
    ),
    "evaluation_report": ("ix_evaluation_report_window",),
    "feature_definition": (
        "ix_feature_definition_datasource",
        "ix_feature_definition_owner",
        "ix_feature_definition_status",
    ),
    "kpi_catalog": (
        "ix_kpi_catalog_owner",
        "ix_kpi_catalog_semantic_keywords",
        "ix_kpi_catalog_status",
    ),
    "ontology_class": ("idx_class_name",),
    "ontology_metric": ("idx_metric_class",),
    "ontology_metric_source": ("ix_metric_source_class_id",),
    "ontology_property": ("idx_property_class",),
    "wiki_import_task": ("ix_wiki_import_task_created",),
    "wiki_page": ("ix_wiki_page_category_id",),
    "wiki_token_usage": ("ix_wiki_token_usage_compile_task",),
}

# 表 → ORM 必须声明的 UniqueConstraint 名（DB 侧是约束支撑的唯一索引）
_REQUIRED_UNIQUE_CONSTRAINTS: dict[str, tuple[str, ...]] = {
    "ontology_metric_source": ("uq_metric_source_triple",),
}

# partial unique：必须带 postgresql_where，否则语义从「局部唯一」变成「全局唯一」
_PARTIAL_UNIQUE_PREDICATES: dict[tuple[str, str], str] = {
    ("embedding_provider", "uq_embedding_provider_active"): "is_active",
    ("data_layer_rule", "uq_layer_rule_manual"): "pattern IS NULL",
    ("data_layer_rule", "uq_layer_rule_pattern"): "pattern IS NOT NULL",
}


def _indexOf(tableName: str, indexName: str):
    table = Base.metadata.tables[tableName]
    return next(index for index in table.indexes if index.name == indexName)


def test_required_indexes_are_declared_in_orm() -> None:
    """32 条 Index 必须逐条出现在 ORM 的 `table.indexes` 里。"""
    missing: list[str] = []
    for tableName, indexNames in _REQUIRED_INDEXES.items():
        table = Base.metadata.tables.get(tableName)
        assert table is not None, f"ORM 里没有表 {tableName}"
        declared = {index.name for index in table.indexes}
        for name in indexNames:
            if name not in declared:
                missing.append(f"{tableName}.{name}")
    assert missing == [], f"ORM 缺少索引声明（迁移建了、ORM 没声明 ⇒ drift 报 extra_index）: {missing}"


def test_required_unique_constraints_are_declared() -> None:
    """`uq_metric_source_triple` 必须是 UniqueConstraint，不能是 Index。"""
    table = Base.metadata.tables["ontology_metric_source"]
    uniqueNames = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    indexNames = {index.name for index in table.indexes}

    for tableName, names in _REQUIRED_UNIQUE_CONSTRAINTS.items():
        for name in names:
            assert name in uniqueNames, f"{tableName}.{name} 必须是 UniqueConstraint"
            assert name not in indexNames, f"{tableName}.{name} 不能同时声明成 Index"


def test_no_duplicate_index_names_per_table() -> None:
    """同一张表上不得有两个同名 Index（重复声明 ⇒ 迁移生成两个同名索引，PG 直接报错）。"""
    duplicates: list[str] = []
    for table in Base.metadata.tables.values():
        seen: set[str] = set()
        for index in table.indexes:
            if index.name in seen:
                duplicates.append(f"{table.name}.{index.name}")
            seen.add(index.name)
    assert duplicates == [], f"同一张表上有重复的索引名: {duplicates}"


def test_partial_unique_indexes_keep_their_predicate() -> None:
    """3 条 partial unique 必须带 `postgresql_where`，且谓词与迁移一致。"""
    for (tableName, indexName), expected in _PARTIAL_UNIQUE_PREDICATES.items():
        index = _indexOf(tableName, indexName)
        assert index.unique is True, f"{indexName} 必须是 unique 索引"
        where = index.dialect_options["postgresql"].get("where")
        assert where is not None, f"{indexName} 缺少 postgresql_where 谓词"
        assert str(where) == expected, f"{indexName} 谓词与迁移不一致"


async def test_orm_index_columns_match_db_columns() -> None:
    """ORM 与 DB 的同名索引列序必须逐条一致（drift 的 blocking 类别 index_mismatch）。"""
    url = resolveTestDatabaseUrl()
    engine = create_async_engine(url)
    try:
        dbIndexes = await _drift._queryDbIndexes(engine)
    finally:
        await engine.dispose()

    mismatches: list[str] = []
    for tableName, indexNames in _REQUIRED_INDEXES.items():
        ormShape = _drift._ormTableShape(Base.metadata.tables[tableName])
        dbByName = {index.name: index for index in dbIndexes.get(tableName, [])}
        for name in indexNames:
            dbIndex = dbByName.get(name)
            if dbIndex is None:
                mismatches.append(f"{tableName}.{name}: DB 上没有这个索引")
                continue
            ormColumns = ormShape.indexColumns.get(name)
            if ormColumns is None:
                # 表达式索引（ORM 侧 sa_text("... DESC")）：drift 工具同规则跳过列比较
                continue
            if ormColumns != dbIndex.columns:
                mismatches.append(
                    f"{tableName}.{name}: ORM={list(ormColumns)} DB={list(dbIndex.columns)}"
                )
    assert mismatches == [], f"ORM 与 DB 的索引列序不一致: {mismatches}"


async def test_unique_constraint_columns_match_db_index() -> None:
    """`uq_metric_source_triple` 的列序必须与 DB 上约束支撑的索引一致。"""
    url = resolveTestDatabaseUrl()
    engine = create_async_engine(url)
    try:
        dbIndexes = await _drift._queryDbIndexes(engine)
    finally:
        await engine.dispose()

    for tableName, names in _REQUIRED_UNIQUE_CONSTRAINTS.items():
        table = Base.metadata.tables[tableName]
        dbByName = {index.name: index for index in dbIndexes.get(tableName, [])}
        for name in names:
            constraint = next(
                c
                for c in table.constraints
                if isinstance(c, UniqueConstraint) and c.name == name
            )
            ormColumns = tuple(column.name for column in constraint.columns)
            dbIndex = dbByName.get(name)
            assert dbIndex is not None, f"DB 上没有 {tableName}.{name}"
            assert ormColumns == dbIndex.columns, (
                f"{tableName}.{name}: ORM={list(ormColumns)} DB={list(dbIndex.columns)}"
            )
