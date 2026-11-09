"""表达式索引的「列序 + 方向」守卫，以及唯一索引 ↔ 唯一约束的声明方向守卫。

## 为什么需要这个文件（换了守卫，不是纯收益）

`fix-alembic-residual-36` 把 9 条 DB 侧带 `DESC` 的索引在 ORM 侧改成
`sa_text("col DESC")`。这一改会让 `alembic check` 归零，但**同时失去**
原本由 drift 工具与 `test_orm_schema_ssot.py` 提供的列序守卫：

- `app/infrastructure/schema_drift.py::_indexColumnsOf`：任一侧是表达式即返回
  `None` ⇒ 跳过列比较；
- `test_orm_schema_ssot.py::test_orm_index_columns_match_db_columns`：同规则跳过。

因此本文件用 PG 的 `pg_get_indexdef` 把「表达式索引的完整列序 + 排序方向」
逐字节钉住：ORM 的 `index.expressions` 渲染结果必须与 DB 的索引定义**逐段相等**。
另外把 6 组「DB 是唯一索引、ORM 改声明为 `Index(unique=True)`」的方向钉住，
防止有人改回 `UniqueConstraint`（那会触发 alembic 的 remove/add 重建提案，
且 downgrade 会踩 `op.drop_index(<同名>)` 的既有迁移雷）。

`uq_doc_entity_rel` 反向守卫：DB 在 0042 迁移里丢了该约束（DROP COLUMN 级联删除后
未重建），ORM 曾一直声明它 ⇒ alembic 提议 ADD。本守卫钉住「ORM 不再声明 DB 上
不存在的约束」，避免下次有人凭 ORM 记忆把它加回来。

## 执行代价

本文件只读 ORM metadata 与 `pg_indexdef`，不写业务数据；但 `unit/conftest.py`
的 autouse 夹具会 TRUNCATE 共享测试库 5434（仓库级遗留项，见
`Harness/changes/fix-schema-check-constraints/summary.md` 后续项 #5）。
"""

from __future__ import annotations

from sqlalchemy import TextClause, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.models import Base
from app.tests import _pg_support

# 索引名 → ORM/DB 都必须一致的表达式段（列名或 `列名 DESC`，按索引列顺序）
_REQUIRED_EXPRESSION_INDEXES: dict[str, tuple[str, ...]] = {
    "ix_audit_log_actor": ("actor", "created_at DESC"),
    "ix_audit_log_entity": ("entity_type", "entity_id", "created_at DESC"),
    "ix_feature_def_history_feature_changed": ("feature_id", "changed_at DESC"),
    "ix_knowledge_conflict_open": ("severity", "auto_detected_at DESC"),
    "ix_kpi_history_kpi_revision": ("kpi_id", "revision DESC"),
    "ix_learning_feedback_time": ("feedback_at DESC",),
    "ix_structure_suggestion_page": ("page_id", "suggested_at DESC"),
    "ix_structure_suggestion_pending": ("status", "suggested_at DESC"),
    "ix_wiki_token_usage_mechanism_time": ("mechanism", "request_time DESC"),
}

# 表名 → 唯一索引名 → 有序列元组（DB 是唯一索引，ORM 必须声明 Index(unique=True)）
_REQUIRED_UNIQUE_INDEXES: dict[str, dict[str, tuple[str, ...]]] = {
    "data_lineage": {
        "uq_data_lineage_edge": (
            "source_layer",
            "source_system",
            "source_object",
            "source_field",
            "target_layer",
            "target_system",
            "target_object",
            "target_field",
        )
    },
    "entity_mapping": {
        "uq_entity_mapping_entity_source": (
            "entity_type",
            "enterprise_key",
            "source_system",
        )
    },
    "feature_definition": {"uq_feature_definition_name": ("feature_name",)},
    "feature_value": {
        "uq_feature_value_dedup": ("feature_id", "entity_key", "valid_at")
    },
    "id_mapping": {"uq_id_mapping_bo_ext": ("business_object", "external_id")},
    "kpi_catalog": {"uq_kpi_catalog_code": ("kpi_code",)},
}

_DB_INDEXDEF_SQL = (
    "SELECT i.relname AS index_name, pg_get_indexdef(i.oid) AS indexdef "
    "FROM pg_class i "
    "JOIN pg_namespace n ON n.oid = i.relnamespace "
    "WHERE n.nspname = 'public' AND i.relkind = 'i' AND i.relname = ANY(:names)"
)

_DB_UNIQUE_SHAPE_SQL = (
    "SELECT t.relname AS table_name, i.relname AS index_name, "
    "ix.indisunique AS is_unique, "
    "ARRAY(SELECT a.attname FROM unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) "
    "      LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
    "      ORDER BY k.ord) AS columns, "
    "(SELECT count(*) FROM pg_constraint con WHERE con.conindid = ix.indexrelid) "
    "  AS constraint_count "
    "FROM pg_index ix "
    "JOIN pg_class i ON i.oid = ix.indexrelid "
    "JOIN pg_class t ON t.oid = ix.indrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE n.nspname = 'public' AND i.relname = ANY(:names)"
)


def _ormSegments(index) -> tuple[str, ...]:
    """ORM `Index.expressions` → 可读段元组（列名 / `sa_text` 原文）。"""
    segments: list[str] = []
    for expression in index.expressions:
        name = getattr(expression, "name", None)
        if name is not None and hasattr(expression, "table"):
            segments.append(str(name))
        else:
            segments.append(str(expression))
    return tuple(segments)


def _balancedParenthesis(text: str, start: int) -> tuple[str, int]:
    """从 `text[start] == '('` 起取成对括号内容；返回 (内容, 结束下标)。"""
    depth = 0
    for position in range(start, len(text)):
        char = text[position]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : position], position
    raise ValueError(f"索引定义括号不配对: {text!r}")


def _indexdefSegments(indexdef: str) -> tuple[str, ...]:
    """从 `pg_get_indexdef` 输出提取索引列/表达式段（保持顺序）。"""
    using = indexdef.find(" USING ")
    if using < 0:
        raise ValueError(f"无法解析索引定义（缺 USING）: {indexdef!r}")
    opening = indexdef.find("(", using)
    if opening < 0:
        raise ValueError(f"无法解析索引定义（缺列括号）: {indexdef!r}")
    content, _ = _balancedParenthesis(indexdef, opening)
    # 我们的目标索引都是单层表达式（无函数调用嵌套）；顶层逗号切分即可。
    return tuple(
        " ".join(segment.split()) for segment in _splitTopLevel(content)
    )


def _splitTopLevel(content: str) -> list[str]:
    """按顶层逗号切分（括号内的逗号不切）。"""
    segments: list[str] = []
    depth = 0
    current: list[str] = []
    for char in content:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            segments.append("".join(current))
            current = []
            continue
        current.append(char)
    if current:
        segments.append("".join(current))
    return segments


def _stripQuotes(segment: str) -> str:
    """PG 会对需要引用的标识符加双引号；本仓库目标列名都是小写简单名。"""
    return segment.replace('"', "")


async def _loadDbIndexdefs(engine: AsyncEngine, names: list[str]) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text(_DB_INDEXDEF_SQL), {"names": names})
        ).mappings().all()
        await conn.rollback()
    return {row["index_name"]: row["indexdef"] for row in rows}


async def test_desc_expression_indexes_match_db_definition() -> None:
    """9 条 DESC 表达式索引：ORM 段序 == DB `pg_get_indexdef` 段序（含方向）。

    失败即意味着 drift / `test_orm_schema_ssot.py` 跳过列比较之后**再无兜底**，
    索引可能静默变成 `col`（丢 DESC）或被重排。
    """
    ormIndexes: dict[str, object] = {}
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            if index.name in _REQUIRED_EXPRESSION_INDEXES:
                ormIndexes[index.name] = index

    missingOrm = sorted(set(_REQUIRED_EXPRESSION_INDEXES) - set(ormIndexes))
    assert missingOrm == [], f"ORM 缺少表达式索引声明: {missingOrm}"

    engine = await _pg_support._newEngine()
    try:
        dbIndexdefs = await _loadDbIndexdefs(
            engine, sorted(_REQUIRED_EXPRESSION_INDEXES)
        )
    finally:
        await engine.dispose()

    missingDb = sorted(set(_REQUIRED_EXPRESSION_INDEXES) - set(dbIndexdefs))
    assert missingDb == [], f"DB 上缺少表达式索引: {missingDb}"

    mismatches: list[str] = []
    for name, expected in _REQUIRED_EXPRESSION_INDEXES.items():
        ormSegments = _ormSegments(ormIndexes[name])
        # 至少一个 `sa_text` 表达式 —— 否则说明有人改回了普通列引用，
        # 那样 drift 反而会重新比较列序，但 DESC 方向仍然丢失。
        if not any(
            isinstance(expression, TextClause)
            for expression in ormIndexes[name].expressions
        ):
            mismatches.append(f"{name}: ORM 已不再是表达式索引（缺 sa_text）")

        dbSegments = tuple(
            _stripQuotes(segment) for segment in _indexdefSegments(dbIndexdefs[name])
        )
        if ormSegments != expected:
            mismatches.append(
                f"{name}: ORM={list(ormSegments)} 期望={list(expected)}"
            )
        if dbSegments != expected:
            mismatches.append(
                f"{name}: DB={list(dbSegments)} 期望={list(expected)}"
            )

    assert mismatches == [], f"表达式索引的列序/方向与 DB 不一致: {mismatches}"


async def test_unique_indexes_are_declared_as_index_not_constraint() -> None:
    """6 组 DB 唯一索引必须在 ORM 侧声明为 `Index(unique=True)`。

    「DB 为真」方向：若改回 `UniqueConstraint`，alembic 会提议 DROP INDEX +
    ADD CONSTRAINT；而 6 个既有迁移的 downgrade 里已有 `op.drop_index(<同名>)`
    ⇒ 回滚必报 index does not exist。
    """
    names: list[str] = []
    for tableName, byName in _REQUIRED_UNIQUE_INDEXES.items():
        table = Base.metadata.tables.get(tableName)
        assert table is not None, f"ORM 里没有表 {tableName}"
        indexes = {index.name: index for index in table.indexes}
        constraints = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        for name, expected in byName.items():
            names.append(name)
            if name not in indexes:
                raise AssertionError(f"{tableName}.{name} 未声明为 Index")
            index = indexes[name]
            if index.unique is not True:
                raise AssertionError(f"{tableName}.{name} 必须是 unique=True 的 Index")
            if _ormSegments(index) != expected:
                raise AssertionError(
                    f"{tableName}.{name}: ORM={list(_ormSegments(index))} "
                    f"期望={list(expected)}"
                )
            if name in constraints:
                raise AssertionError(
                    f"{tableName}.{name} 不能同时声明为 UniqueConstraint"
                )

    engine = await _pg_support._newEngine()
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text(_DB_UNIQUE_SHAPE_SQL), {"names": names})
            ).mappings().all()
            await conn.rollback()
    finally:
        await engine.dispose()

    dbByName = {row["index_name"]: row for row in rows}
    mismatches: list[str] = []
    for tableName, byName in _REQUIRED_UNIQUE_INDEXES.items():
        for name, expected in byName.items():
            row = dbByName.get(name)
            if row is None:
                mismatches.append(f"{tableName}.{name}: DB 上没有该索引")
                continue
            if row["table_name"] != tableName:
                mismatches.append(
                    f"{name}: DB 表={row['table_name']} 期望={tableName}"
                )
            if row["is_unique"] is not True:
                mismatches.append(f"{name}: DB 不是唯一索引")
            if row["constraint_count"]:
                mismatches.append(f"{name}: DB 上是约束支撑索引，不是裸唯一索引")
            if tuple(row["columns"]) != expected:
                mismatches.append(
                    f"{name}: DB 列序={list(row['columns'])} 期望={list(expected)}"
                )
    assert mismatches == [], f"唯一索引的 ORM/DB 形状不一致: {mismatches}"


# 0141 迁移的 DB 终态：(表, 列) → (data_type, is_nullable, column_default 片段)
_MUST_DDL_COLUMNS: dict[tuple[str, str], tuple[str, str, str | None]] = {
    ("data_quality_violation_sample", "sample_pk_values"): ("json", "NO", "'[]'::json"),
    ("document_entity_relation", "entity_type"): ("character varying", "NO", None),
    ("l3_acl", "user_id"): ("bigint", "NO", None),
    ("l3_acl", "granted_by"): ("bigint", "NO", None),
    ("l3_acl", "granted_at"): ("timestamp with time zone", "NO", "now()"),
    ("wiki_token_usage", "compile_task_id"): ("integer", "YES", None),
}

_DB_COLUMN_SHAPE_SQL = (
    "SELECT table_name, column_name, data_type, is_nullable, column_default "
    "FROM information_schema.columns "
    "WHERE table_schema = 'public' AND ("
    + " OR ".join(
        f"(table_name = '{table}' AND column_name = '{column}')"
        for table, column in _MUST_DDL_COLUMNS
    )
    + ")"
)


async def test_must_ddl_columns_match_database_after_0141() -> None:
    """0141 迁移的 6 项 DDL 终态必须落在 DB 上（含 sample_pk_values 的 json 默认值）。

    `alembic check` 归零只证明「ORM 与 DB 相等」，不证明「等的是正确形状」：
    类型/空值方向写反时两边也会一致。这里把 DB 实测终态钉死。
    `server_default` 尤其重要：若只改类型不摘旧默认，PG 会在 ALTER TYPE 时报错；
    迁移已处理，本断言防止后续被改回 `'[]'::jsonb`。
    """
    engine = await _pg_support._newEngine()
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text(_DB_COLUMN_SHAPE_SQL))
            ).mappings().all()
            await conn.rollback()
    finally:
        await engine.dispose()

    byKey = {
        (row["table_name"], row["column_name"]): row for row in rows
    }
    mismatches: list[str] = []
    for (table, column), (dataType, nullable, defaultFragment) in _MUST_DDL_COLUMNS.items():
        row = byKey.get((table, column))
        if row is None:
            mismatches.append(f"{table}.{column}: DB 上不存在该列")
            continue
        if row["data_type"] != dataType:
            mismatches.append(
                f"{table}.{column}: data_type={row['data_type']} 期望={dataType}"
            )
        if row["is_nullable"] != nullable:
            mismatches.append(
                f"{table}.{column}: is_nullable={row['is_nullable']} 期望={nullable}"
            )
        if defaultFragment is not None and defaultFragment not in (row["column_default"] or ""):
            mismatches.append(
                f"{table}.{column}: default={row['column_default']!r} 期望含={defaultFragment!r}"
            )
    assert mismatches == [], f"0141 迁移的 DB 终态不符合预期: {mismatches}"


_FK_SHAPE_SQL = (
    "SELECT c.conname, a.attname AS column_name, pg_get_constraintdef(c.oid) AS definition "
    "FROM pg_constraint c "
    "JOIN pg_class t ON t.oid = c.conrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = c.conkey[1] "
    "WHERE n.nspname = 'public' AND t.relname = 'wiki_compile_task' AND c.contype = 'f'"
)


async def test_wiki_compile_task_model_foreign_keys_exist() -> None:
    """`selected_model_id` / `fallback_model_id` 必须有指向 `llm_config(id)` 的 FK。

    0141 是幂等补建：全新库的 0077 已建（PG 自动命名），prod 侧缺则由 0141 补。
    这里不看约束名，只按「列 → 引用目标」断言，兼容两种命名来源。
    """
    engine = await _pg_support._newEngine()
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text(_FK_SHAPE_SQL))
            ).mappings().all()
            await conn.rollback()
    finally:
        await engine.dispose()

    byColumn = {row["column_name"]: row["definition"] for row in rows}
    required = ("selected_model_id", "fallback_model_id")
    missing = [
        f"wiki_compile_task.{column}"
        for column in required
        if "REFERENCES llm_config(id)" not in byColumn.get(column, "")
    ]
    assert missing == [], f"缺少指向 llm_config(id) 的外键: {missing}"


async def test_doc_entity_rel_unique_constraint_is_not_declared() -> None:
    """`uq_doc_entity_rel` 在 DB 上不存在（0042 重建 entity_key 时丢失），ORM 不得声明。

    这条不是「ORM 漏声明」，而是「迁移链的真实终态没有该约束」：
    - 0030 建表时声明；
    - 0042 `ALTER TABLE ... DROP COLUMN entity_key` 级联删除该唯一约束，
      重建列后未重建约束；
    - 全新库与 5433 生产同构库实测都不存在。

    若有人按 0030 的记忆把它加回 ORM，`alembic check` 会重新出现
    `Detected added unique constraint`。要真的恢复唯一性，必须先评估重复行并
    写一条专用迁移（见 summary 后续项），不能只改 ORM。
    """
    table = Base.metadata.tables["document_entity_relation"]
    constraintNames = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    indexNames = {index.name for index in table.indexes}
    assert "uq_doc_entity_rel" not in constraintNames, (
        "ORM 重新声明了 DB 上不存在的 uq_doc_entity_rel；"
        "恢复唯一性必须走独立迁移，不能只改 ORM"
    )
    assert "uq_doc_entity_rel" not in indexNames, (
        "ORM 把 uq_doc_entity_rel 声明成了 Index；DB 上不存在该索引"
    )
