"""采购源绑定数据模型（`procurement_source_binding` / `procurement_property_binding`）单测。

本文件**双向钉住** ORM 与 Alembic 迁移 0154：

1. 两张表的表名 / 列名 / 类型 / 可空性；
2. 约束（FK 的 ondelete、CHECK 表达式、**部分唯一索引**的谓词）；
3. 迁移的 revision 链与 ``downgrade()`` 对称性。

为什么用「读迁移源码 + 比字符串」而不是 ``alembic check``：
``alembic check`` 只比**名**不比**式**（见 ``Harness/changes`` 里 CHECK 约束补进 ORM
的复盘：只补名 = 假绿）。**部分唯一索引的 ``postgresql_where`` 同样不在 alembic
的比对范围内** ⇒ 必须在这里逐字钉住。

为什么不新建 integration 测试文件：
CI 集成测试按「排序后下标 mod 4」轮转分片归属 ⇒ 新建文件会假 net-new。
本文件是 unit（路径哈希分片）⇒ 新增安全。

设计来源：``docs/update/1118/M0/A8-采购源绑定候选与表结构设计.md`` §4（D2 已定：
复合键 / 有效期 / 过滤白名单走独立列，不进 JSON）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "alembic" / "versions"

# 迁移号串行占号：head 0153 → 本批 0154（A4-0 顺延，见 A8 文档 §4.4）。
_MIGRATION_STEM = "0154_procurement_bindings"

_TABLE_SOURCE = "procurement_source_binding"
_TABLE_PROPERTY = "procurement_property_binding"

_ALL_MODELS = (ProcurementSourceBinding, ProcurementPropertyBinding)

_SOURCE_COLUMNS = {
    "id",
    "class_id",
    "datasource_id",
    "schema_name",
    "source_table",
    "binding_role",
    "filter_expression",
    "filter_allowlist",
    "is_candidate",
    "confirmed_by",
    "confirmed_time",
    "valid_from",
    "valid_to",
    "created_by",
    "created_time",
    "updated_time",
}

_PROPERTY_COLUMNS = {
    "id",
    "source_binding_id",
    "property_id",
    "source_column",
    "key_ordinal",
    "transform_expression",
    "is_candidate",
    "confirmed_by",
    "confirmed_time",
    "valid_from",
    "valid_to",
    "created_by",
    "created_time",
    "updated_time",
}


def _loadMigration(stem: str):
    path = _MIGRATIONS_DIR / f"{stem}.py"
    assert path.exists(), f"迁移文件不存在：{path}"
    spec = importlib.util.spec_from_file_location(f"_mig_{stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recordedOps(module, funcName: str) -> list[tuple]:
    """跑一遍迁移函数，记录它对 ``alembic.op`` 发的 DDL 调用。"""

    calls: list[tuple] = []

    class _FakeOp:
        def create_table(self, tableName, *columns, **kwargs):
            calls.append(("create_table", tableName, tuple(columns)))

        def create_index(self, name, tableName, *columns, **kwargs):
            calls.append(("create_index", name, tableName, tuple(columns), kwargs))

        def drop_table(self, tableName, **kwargs):
            calls.append(("drop_table", tableName))

        def drop_index(self, name, **kwargs):
            calls.append(("drop_index", name))

        def __getattr__(self, name):
            def _unexpected(*args, **kwargs):
                calls.append((name, *args))

            return _unexpected

    original = module.op
    module.op = _FakeOp()
    try:
        getattr(module, funcName)()
    finally:
        module.op = original
    return calls


def _migrationTables() -> dict[str, dict]:
    """返回 {表名: {'columns', 'checks', 'uniques'}}（从迁移源码解析）。"""

    result: dict[str, dict] = {}
    for call in _recordedOps(_loadMigration(_MIGRATION_STEM), "upgrade"):
        if call[0] != "create_table":
            continue
        tableName = call[1]
        columns: dict[str, sa.Column] = {}
        checks: dict[str, str] = {}
        uniques: dict[str, tuple[str, ...]] = {}
        for item in call[2]:
            if isinstance(item, sa.Column):
                columns[item.name] = item
            elif isinstance(item, sa.CheckConstraint):
                checks[item.name] = str(item.sqltext)
            elif isinstance(item, sa.UniqueConstraint):
                uniques[item.name] = tuple(getattr(item, "_pending_colargs", ()))
        result[tableName] = {"columns": columns, "checks": checks, "uniques": uniques}
    return result


def _migrationIndexes() -> dict[str, dict]:
    result: dict[str, dict] = {}
    for call in _recordedOps(_loadMigration(_MIGRATION_STEM), "upgrade"):
        if call[0] != "create_index":
            continue
        _kind, name, tableName, columns, kwargs = call
        result[name] = {"table": tableName, "columns": columns, "kwargs": kwargs}
    return result


def _ormChecks(table) -> dict[str, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }


def _ormPartialIndexes(table) -> dict[str, str]:
    """返回 {索引名: postgresql_where 的 SQL 文本}（仅唯一索引）。"""

    result: dict[str, str] = {}
    for index in table.indexes:
        if not index.unique:
            continue
        where = index.dialect_options.get("postgresql", {}).get("where")
        if where is None:
            continue
        result[index.name] = str(where)
    return result


# ---------------------------------------------------------------- 表与列


def test_两张表名与契约一致():
    assert {model.__tablename__ for model in _ALL_MODELS} == {_TABLE_SOURCE, _TABLE_PROPERTY}


def test_源绑定表列集合():
    assert set(ProcurementSourceBinding.__table__.columns.keys()) == _SOURCE_COLUMNS


def test_属性绑定表列集合():
    assert set(ProcurementPropertyBinding.__table__.columns.keys()) == _PROPERTY_COLUMNS


def test_迁移与ORM列类型和可空性逐字一致():
    tables = _migrationTables()
    for model in _ALL_MODELS:
        tableName = model.__tablename__
        assert tableName in tables, f"迁移未建表：{tableName}"
        migrationColumns = tables[tableName]["columns"]
        ormColumns = model.__table__.columns

        assert set(migrationColumns) == set(ormColumns.keys()), tableName
        for name, ormColumn in ormColumns.items():
            migColumn = migrationColumns[name]
            assert migColumn.nullable == ormColumn.nullable, f"{tableName}.{name}"
            assert type(migColumn.type).__name__ == type(ormColumn.type).__name__, (
                f"{tableName}.{name}"
            )
            assert getattr(migColumn.type, "length", None) == getattr(
                ormColumn.type, "length", None
            ), f"{tableName}.{name}"
            assert migColumn.comment == ormColumn.comment, f"{tableName}.{name}"


def test_迁移与ORM的CHECK表达式逐字一致():
    tables = _migrationTables()
    for model in _ALL_MODELS:
        tableName = model.__tablename__
        assert tables[tableName]["checks"] == _ormChecks(model.__table__), tableName


# ---------------------------------------------------------------- 约束语义


def test_源绑定表约束语义():
    table = ProcurementSourceBinding.__table__
    checks = _ormChecks(table)

    assert checks["ck_procurement_source_binding_role"] == (
        "binding_role IN ('BASE','NUMERATOR','DENOMINATOR','FILTER')"
    )
    assert checks["ck_procurement_source_binding_valid_range"] == (
        "valid_to IS NULL OR valid_to > valid_from"
    )
    # 候选 ⇔ 无确认人（D-3：未确认的绑定在库层就是候选）
    assert checks["ck_procurement_source_binding_candidate"] == (
        "NOT is_candidate OR confirmed_by IS NULL"
    )
    assert checks["ck_procurement_source_binding_confirmed"] == (
        "is_candidate OR (confirmed_by IS NOT NULL AND confirmed_time IS NOT NULL)"
    )

    fks = {
        constraint.name: (constraint.elements[0].target_fullname, constraint.ondelete)
        for constraint in table.constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
    }
    assert fks["fk_procurement_source_binding_class"] == ("ontology_class.id", "RESTRICT")
    assert fks["fk_procurement_source_binding_datasource"] == ("data_source.id", "RESTRICT")


def test_属性绑定表约束语义():
    table = ProcurementPropertyBinding.__table__
    checks = _ormChecks(table)

    assert checks["ck_procurement_property_binding_key_ordinal"] == (
        "key_ordinal IS NULL OR key_ordinal >= 1"
    )
    assert checks["ck_procurement_property_binding_valid_range"] == (
        "valid_to IS NULL OR valid_to > valid_from"
    )
    assert checks["ck_procurement_property_binding_candidate"] == (
        "NOT is_candidate OR confirmed_by IS NULL"
    )

    fks = {
        constraint.name: (constraint.elements[0].target_fullname, constraint.ondelete)
        for constraint in table.constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
    }
    # 属性绑定的生命周期从属于源绑定 ⇒ CASCADE 是唯一合理的语义
    assert fks["fk_procurement_property_binding_source"] == (
        f"{_TABLE_SOURCE}.id",
        "CASCADE",
    )
    assert fks["fk_procurement_property_binding_property"] == (
        "ontology_property.id",
        "RESTRICT",
    )


# ---------------------------------------------------------------- 部分唯一索引


def test_部分唯一索引_同键只允许一条生效绑定():
    sourceWhere = _ormPartialIndexes(ProcurementSourceBinding.__table__)
    assert sourceWhere["uq_procurement_source_binding_active"] == "valid_to IS NULL"

    propertyWhere = _ormPartialIndexes(ProcurementPropertyBinding.__table__)
    assert propertyWhere["uq_procurement_property_binding_active"] == "valid_to IS NULL"
    assert propertyWhere["uq_procurement_property_binding_key_ordinal"] == (
        "key_ordinal IS NOT NULL AND valid_to IS NULL"
    )


def test_迁移与ORM的部分唯一索引逐字一致():
    migrationIndexes = _migrationIndexes()
    for model in _ALL_MODELS:
        ormWhere = _ormPartialIndexes(model.__table__)
        for name, where in ormWhere.items():
            assert name in migrationIndexes, f"迁移未建索引：{name}"
            migIndex = migrationIndexes[name]
            assert migIndex["table"] == model.__tablename__
            assert migIndex["kwargs"].get("unique") is True, name
            assert str(migIndex["kwargs"].get("postgresql_where")) == where, name


# ---------------------------------------------------------------- 迁移链与对称性


def test_迁移链():
    module = _loadMigration(_MIGRATION_STEM)
    assert module.revision == "0154"
    assert module.down_revision == "0153"


def test_downgrade与upgrade对称():
    module = _loadMigration(_MIGRATION_STEM)
    upgrades = _recordedOps(module, "upgrade")
    downgrades = _recordedOps(module, "downgrade")

    createdTables = {call[1] for call in upgrades if call[0] == "create_table"}
    droppedTables = {call[1] for call in downgrades if call[0] == "drop_table"}
    assert createdTables == droppedTables == {_TABLE_SOURCE, _TABLE_PROPERTY}

    createdIndexes = {call[1] for call in upgrades if call[0] == "create_index"}
    droppedIndexes = {call[1] for call in downgrades if call[0] == "drop_index"}
    assert createdIndexes == droppedIndexes, "索引 up/down 不对称"

    # 只撤自己：不得 drop 任何既有表 / 既有索引
    assert droppedIndexes.isdisjoint(
        {"ix_ontology_class_name", "uq_approval_task_matrix", "ix_semantic_release_status"}
    )
