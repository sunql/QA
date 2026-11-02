"""ORM 必须声明 DB 上全部的命名 CHECK 约束（**名字与表达式都要一致**）。

2026-11-02 实测（真实库 `qa_metadata_test` / 5434）：

- DB 侧 48 个命名 CHECK 约束（`pg_constraint.contype='c'` 且 `connamespace='public'`）；
- ORM 侧只有 28 个 ⇒ **缺 20 个**；
- `alembic check`（1.19.1）**只报「DB 有、ORM 没有」的名字，不比较表达式内容**
  ⇒ 光把名字补进去就能消掉 `Detected removed check constraint` 告警，
  但表达式写错（漏一个枚举值、写错比较方向）两个工具都发现不了；
- drift 工具（`app/infrastructure/schema_drift.py`）**根本不比较 CHECK 约束**
  ⇒ 补没补、补对没补对，它都是 exit 0。

⇒ 本文件用 PostgreSQL 自己做规范化对比：把 ORM 的表达式原样交给 PG
（临时表 `CREATE TEMP TABLE ... (LIKE public.<表>)` + `ADD CONSTRAINT ... CHECK (<表达式>)`），
再读回 `pg_get_constraintdef` —— 两边都是 PG 自己渲染的规范形式 ⇒ 可以逐字节比。

放在 `unit/` 而不是 `integration/`：CI 的 integration 分片 =
`find app/tests/integration -name 'test_*.py' | sort` 后按**下标 mod 4** 取
⇒ 在 `integration/` 新增任何一个文件都会让其后所有文件的分片归属整体轮转一位
（见 `Harness/rules/测试规范.md` 的「⚠️ 目录分片稳定性」条）。
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.asyncio.engine import AsyncConnection

from app.domain.models import Base
from app.tests._pg_support import resolveTestDatabaseUrl

_DB_CONSTRAINTS_SQL = text(
    "SELECT c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid) "
    "FROM pg_constraint c "
    "JOIN pg_class t ON t.oid = c.conrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE c.contype = 'c' AND n.nspname = 'public'"
)

_PROBE_CONSTRAINTS_SQL = text(
    "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
    "WHERE c.conrelid = to_regclass(:probe)"
)

# 表达式里不允许出现分号 / 美元引号：它们会把一条 ALTER TABLE 拆成多条语句。
_FORBIDDEN_IN_EXPRESSION = (";", "$$")


def _ormCheckConstraints() -> dict[tuple[str, str], CheckConstraint]:
    """ORM 侧全部命名 CHECK 约束：(表名, 约束名) → 约束对象。"""
    result: dict[tuple[str, str], CheckConstraint] = {}
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, CheckConstraint):
                continue
            assert constraint.name, f"{table.name} 上存在未命名的 CHECK 约束"
            result[(table.name, constraint.name)] = constraint
    return result


def _renderExpression(constraint: CheckConstraint) -> str:
    """把 ORM 的 CHECK 表达式渲染成 PostgreSQL 方言的原始 SQL 文本。"""
    return str(constraint.sqltext.compile(dialect=postgresql.dialect()))


async def _readDbConstraints(conn: AsyncConnection) -> dict[tuple[str, str], str]:
    """DB 侧全部命名 CHECK 约束：(表名, 约束名) → `pg_get_constraintdef` 规范形式。"""
    rows = (await conn.execute(_DB_CONSTRAINTS_SQL)).all()
    return {(row[0], row[1]): row[2] for row in rows}


async def _renderOrmConstraints(
    conn: AsyncConnection,
    ormConstraints: dict[tuple[str, str], CheckConstraint],
) -> dict[tuple[str, str], str]:
    """把 ORM 的 CHECK 表达式交给 PG 渲染成规范形式。

    手法：先 `CREATE TEMP TABLE <probe> (LIKE public.<表>)` 复制列定义
    （**不**复制 CHECK 约束 —— `LIKE` 只在 `INCLUDING CONSTRAINTS` 时才复制），
    再把 ORM 的表达式原样 `ADD CONSTRAINT` 上去 ⇒ PG 解析后的表达式树与
    DB 上的完全同构 ⇒ `pg_get_constraintdef` 的输出可以逐字节比。
    """
    byTable: dict[str, list[CheckConstraint]] = {}
    for (tableName, _), constraint in ormConstraints.items():
        byTable.setdefault(tableName, []).append(constraint)

    rendered: dict[tuple[str, str], str] = {}
    for tableName, constraints in byTable.items():
        probe = f"_ckprobe_{tableName}"
        await conn.execute(text(f"DROP TABLE IF EXISTS {probe}"))
        await conn.execute(text(f"CREATE TEMP TABLE {probe} (LIKE public.{tableName})"))
        for constraint in constraints:
            expression = _renderExpression(constraint)
            for forbidden in _FORBIDDEN_IN_EXPRESSION:
                assert forbidden not in expression, (
                    f"{tableName}.{constraint.name} 的表达式含禁止片段 {forbidden!r}"
                )
            await conn.execute(
                text(
                    f'ALTER TABLE {probe} ADD CONSTRAINT "{constraint.name}" '
                    f"CHECK ({expression})"
                )
            )
        rows = (await conn.execute(_PROBE_CONSTRAINTS_SQL, {"probe": probe})).all()
        for constraintName, definition in rows:
            rendered[(tableName, constraintName)] = definition
        await conn.execute(text(f"DROP TABLE {probe}"))
    return rendered


async def test_db_check_constraints_are_all_declared_in_orm() -> None:
    """DB 上的每个命名 CHECK 约束，ORM 都必须声明（否则 alembic 会提议 DROP）。"""
    url = resolveTestDatabaseUrl()
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            dbConstraints = await _readDbConstraints(conn)
    finally:
        await engine.dispose()

    ormConstraints = _ormCheckConstraints()
    missing = sorted(
        f"{tableName}.{name}"
        for (tableName, name) in dbConstraints
        if (tableName, name) not in ormConstraints
    )
    assert missing == [], (
        f"ORM 缺少 CHECK 约束声明（DB 有、ORM 没有 ⇒ alembic autogenerate 会提议 DROP）: {missing}"
    )


async def test_orm_declares_no_check_constraint_missing_in_db() -> None:
    """反向守卫：ORM 不得声明 DB 上不存在的 CHECK 约束（否则 alembic 会提议 ADD）。"""
    url = resolveTestDatabaseUrl()
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            dbConstraints = await _readDbConstraints(conn)
    finally:
        await engine.dispose()

    ormConstraints = _ormCheckConstraints()
    extra = sorted(
        f"{tableName}.{name}"
        for (tableName, name) in ormConstraints
        if (tableName, name) not in dbConstraints
    )
    assert extra == [], f"ORM 声明了 DB 上不存在的 CHECK 约束: {extra}"


async def test_check_constraint_expressions_match_db() -> None:
    """同名约束的**表达式内容**必须与 DB 一致（PG 规范化后逐字节比）。

    alembic 不比较 CHECK 表达式、drift 根本不比较 CHECK 约束
    ⇒ 表达式写错（漏枚举值、方向反了）只能靠这条用例暴露。
    """
    url = resolveTestDatabaseUrl()
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            dbConstraints = await _readDbConstraints(conn)
            rendered = await _renderOrmConstraints(conn, _ormCheckConstraints())
    finally:
        await engine.dispose()

    mismatches: list[str] = []
    for (tableName, name), dbDefinition in dbConstraints.items():
        ormDefinition = rendered.get((tableName, name))
        if ormDefinition is None:
            continue  # 缺失由 test_db_check_constraints_are_all_declared_in_orm 负责
        if ormDefinition != dbDefinition:
            mismatches.append(f"{tableName}.{name}:\n  ORM={ormDefinition}\n  DB ={dbDefinition}")
    assert mismatches == [], f"CHECK 约束表达式与 DB 不一致: {mismatches}"
