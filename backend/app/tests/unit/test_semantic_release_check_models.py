"""发布校验结论表（`semantic_release_check`）的 ORM ↔ 迁移 0155 双向钉死。

为什么要有这个文件：

1. ``alembic check`` **只比名不比式** ⇒ 只把 CHECK 的名字补进 ORM 是假绿
   （见 `Harness/changes/fix-schema-check-constraints/summary.md`）。
   表达式必须在这里逐字比对。
2. **表达式唯一索引**（`COALESCE(target_key, '')`）根本不在 ``alembic check``
   的比对范围内 ⇒ 只能靠本文件钉住「ORM 与迁移建的是同一个索引」。
3. **列注释**也要逐字一致：ORM 不声明注释 ⇒ ``alembic check`` 认为该删注释 ⇒
   报「New upgrade operations」（实测踩过：0155 首版漏注释 ⇒ alembic check 报红）。

本文件是 unit（路径稳定哈希分片）⇒ 新增文件不移动任何其它文件的分片归属。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from app.domain.semantic_release_check_models import SemanticReleaseCheck

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "alembic" / "versions"

# 迁移号串行占号：head 0154（采购源绑定）→ 本批 0155。
_MIGRATION_STEM = "0155_semantic_release_check"
_TABLE = "semantic_release_check"

_EXPECTED_COLUMNS = {
    "id",
    "release_id",
    "check_code",
    "check_item",
    "severity",
    "result",
    "target_type",
    "target_key",
    "detail",
    "checked_time",
    "checked_by",
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
            calls.append(("drop_index", name, kwargs.get("table_name")))

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


def _migrationTable() -> dict:
    """迁移里创建的表（列 / CHECK）。"""

    for call in _recordedOps(_loadMigration(_MIGRATION_STEM), "upgrade"):
        if call[0] != "create_table":
            continue
        columns: dict[str, sa.Column] = {}
        checks: dict[str, str] = {}
        for item in call[2]:
            if isinstance(item, sa.Column):
                columns[item.name] = item
            elif isinstance(item, sa.CheckConstraint):
                checks[item.name] = str(item.sqltext)
        return {"columns": columns, "checks": checks}
    raise AssertionError("迁移未调用 create_table")


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


def _expressionText(expression: object) -> str:
    """表达式索引项的 SQL 文本（``sa.text(...)`` ⇒ TextClause）。"""

    assert isinstance(expression, sa.sql.elements.TextClause), expression
    return str(expression.text)


# ---------------------------------------------------------------- 表与列


def test_table_name_and_columns() -> None:
    assert SemanticReleaseCheck.__tablename__ == _TABLE
    assert set(SemanticReleaseCheck.__table__.columns.keys()) == _EXPECTED_COLUMNS


def test_migration_and_orm_columns_match_type_nullability_length_and_comment() -> None:
    migration = _migrationTable()
    migrationColumns = migration["columns"]
    ormColumns = SemanticReleaseCheck.__table__.columns

    assert set(migrationColumns) == set(ormColumns.keys())
    for name, ormColumn in ormColumns.items():
        migrationColumn = migrationColumns[name]
        assert migrationColumn.nullable == ormColumn.nullable, name
        assert type(migrationColumn.type).__name__ == type(ormColumn.type).__name__, name
        assert getattr(migrationColumn.type, "length", None) == getattr(
            ormColumn.type, "length", None
        ), name
        # 注释逐字一致：ORM 不声明 ⇒ alembic check 认为该删注释 ⇒ 报红。
        assert migrationColumn.comment == ormColumn.comment, name


def test_migration_and_orm_check_expressions_match_verbatim() -> None:
    assert _migrationTable()["checks"] == _ormChecks(SemanticReleaseCheck.__table__)


# ---------------------------------------------------------------- 约束语义


def test_check_constraints_pin_severity_and_result_literals() -> None:
    checks = _ormChecks(SemanticReleaseCheck.__table__)

    assert checks["ck_semantic_release_check_severity"] == "severity IN ('ERROR','WARNING')"
    assert checks["ck_semantic_release_check_result"] == "result IN ('PASS','FAIL')"
    assert checks["ck_semantic_release_check_item_not_empty"] == (
        "length(check_code) > 0 AND length(check_item) > 0"
    )


def test_foreign_key_cascades_with_release_deletion() -> None:
    """发布被删 ⇒ 校验证据一并消失（不留孤儿证据）。"""

    fks = {
        constraint.name: (constraint.elements[0].target_fullname, constraint.ondelete)
        for constraint in SemanticReleaseCheck.__table__.constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
    }

    assert fks == {
        "fk_semantic_release_check_release": ("semantic_release.id", "CASCADE"),
    }


# ---------------------------------------------------------------- 索引


def test_indexes_match_migration_by_name_and_expression() -> None:
    """唯一索引带 COALESCE 表达式：它不在 alembic check 的比对范围内，只能在这里钉。"""

    migrationIndexes = _migrationIndexes()
    assert set(migrationIndexes) == {
        "ix_semantic_release_check_release",
        "uq_semantic_release_check_unique",
    }

    ormIndexes = {index.name: index for index in SemanticReleaseCheck.__table__.indexes}
    assert set(ormIndexes) == set(migrationIndexes)

    unique = ormIndexes["uq_semantic_release_check_unique"]
    assert unique.unique is True
    assert _expressionText(unique.expressions[-1]) == "COALESCE(target_key, '')"
    migrationColumns = migrationIndexes["uq_semantic_release_check_unique"]["columns"][0]
    assert _expressionText(migrationColumns[-1]) == "COALESCE(target_key, '')"

    plain = ormIndexes["ix_semantic_release_check_release"]
    assert plain.unique is False
    assert [column.name for column in plain.columns] == ["release_id"]


# ---------------------------------------------------------------- 迁移链


def test_revision_chain_is_serial() -> None:
    module = _loadMigration(_MIGRATION_STEM)

    assert module.revision == "0155"
    assert module.down_revision == "0154"


def test_downgrade_drops_indexes_before_table() -> None:
    """回滚顺序：先删索引再删表（否则删表会连带索引，报错掩盖真因）。"""

    calls = _recordedOps(_loadMigration(_MIGRATION_STEM), "downgrade")

    assert [call[0] for call in calls] == ["drop_index", "drop_index", "drop_table"]
    assert calls[0][1] == "uq_semantic_release_check_unique"
    assert calls[1][1] == "ix_semantic_release_check_release"
    assert calls[2][1] == _TABLE
