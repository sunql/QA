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

⚠️ **已知残留隐患（不在本文件解决）**：`unit/conftest.py` 的 autouse 夹具
（`warmBusinessObjectRegistry` → `dbSession` → `seedEngine` → `_truncateAll`）
每个用例都会 **TRUNCATE 共享测试库 5434**。本文件不写业务数据、不依赖库内数据，
TRUNCATE 对本文件的断言（只读 `pg_constraint`）无影响；但「共享库被并发套件截断」
这条隐患本身是仓库级遗留项，已在 `Harness/changes/fix-schema-check-constraints/summary.md`
的「后续项」里登记，根治需要给该类用例一条不继承截断型 autouse 的独立夹具作用域。
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.asyncio.engine import AsyncConnection

from app.domain.models import Base
from app.tests import _pg_support

_BACKEND_ROOT = Path(__file__).resolve().parents[3]  # backend/

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

# SQL 标识符白名单：与 `app/services/data_quality_evaluators/_common.py` 的
# `validate_identifier` 同规则（^[A-Za-z_][A-Za-z0-9_]*$）。表名 / 约束名会被
# 拼进 DDL 文本，必须先过这一关。
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 表达式里不允许出现的片段：分号（拆语句）、`--` / `/*`（注释）、
# `$$`（美元引号）。ORM 表达式由代码作者书写，不是用户输入，但这条守卫
# 防止「顺手把某段 SQL 片段贴进 CHECK」时静默改变语句结构。
_FORBIDDEN_IN_EXPRESSION = (";", "--", "/*", "*/", "$$")


def _validateIdentifier(value: str, *, role: str) -> str:
    """标识符必须是合法 SQL 标识符，否则拒绝拼进 DDL。"""
    if not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(f"非法 {role} 标识符（必须 ^[A-Za-z_][A-Za-z0-9_]*$）: {value!r}")
    return value


def _assertExpressionIsSafe(tableName: str, constraintName: str, expression: str) -> None:
    """表达式不得含会改变语句结构的片段。"""
    for forbidden in _FORBIDDEN_IN_EXPRESSION:
        if forbidden in expression:
            raise ValueError(
                f"{tableName}.{constraintName} 的 CHECK 表达式含禁止片段 {forbidden!r}：{expression!r}"
            )


def _headRevisions() -> set[str]:
    """代码里 Alembic 的 head 修订号集合。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return set(ScriptDirectory.from_config(config).get_heads())


async def _assertDbAtHead(conn: AsyncConnection) -> None:
    """前置条件：测试库必须已迁移到代码 head。

    否则 DB 上的 CHECK 约束集合与 ORM 不是同一代 schema，断言会把
    「库还没升级」误报成「ORM 漏声明」—— 必须给一句人话，而不是让下游
    断言报出一堆看不懂的名字差异。
    """
    rows = (await conn.execute(text("SELECT version_num FROM alembic_version"))).all()
    versions = {row[0] for row in rows}
    if not versions:
        raise RuntimeError(
            "测试库没有 alembic_version 行：尚未执行 `alembic upgrade head`，"
            "无法比对 CHECK 约束。"
        )
    heads = _headRevisions()
    unknown = versions - heads
    if unknown:
        raise RuntimeError(
            f"测试库 alembic_version={sorted(versions)} 不在代码 head={sorted(heads)} 之内："
            "请先对测试库执行 `alembic upgrade head`，否则 CHECK 约束集合不是同一代 schema。"
        )


def _ormCheckConstraints() -> dict[tuple[str, str], CheckConstraint]:
    """ORM 侧全部命名 CHECK 约束：(表名, 约束名) → 约束对象。"""
    result: dict[tuple[str, str], CheckConstraint] = {}
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, CheckConstraint):
                continue
            if not constraint.name:
                raise ValueError(f"{table.name} 上存在未命名的 CHECK 约束：{constraint.sqltext!r}")
            _validateIdentifier(table.name, role="表名")
            _validateIdentifier(constraint.name, role="约束名")
            result[(table.name, constraint.name)] = constraint
    return result


def _renderExpression(constraint: CheckConstraint) -> str:
    """把 ORM 的 CHECK 表达式渲染成 PostgreSQL 方言的原始 SQL 文本。"""
    return str(
        constraint.sqltext.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


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

    DDL 走 `exec_driver_sql`（原始 SQL，不做 bindparam 解析）⇒ 表达式里的
    冒号等字符不会被 SQLAlchemy 当成参数占位符改写。
    """
    byTable: dict[str, list[CheckConstraint]] = {}
    for (tableName, _), constraint in ormConstraints.items():
        byTable.setdefault(tableName, []).append(constraint)

    rendered: dict[tuple[str, str], str] = {}
    for tableName, constraints in byTable.items():
        # 探针表名一律【带 pg_temp. 前缀】：裸名会按 search_path 解析，
        # 万一 public 下存在同名表，`DROP TABLE IF EXISTS <裸名>` 会误删那张真表。
        probe = f"pg_temp._ckprobe_{tableName}"
        await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {probe}")
        await conn.exec_driver_sql(f"CREATE TEMP TABLE {probe} (LIKE public.{tableName})")
        try:
            for constraint in constraints:
                expression = _renderExpression(constraint)
                _assertExpressionIsSafe(tableName, constraint.name, expression)
                await conn.exec_driver_sql(
                    f'ALTER TABLE {probe} ADD CONSTRAINT "{constraint.name}" '
                    f"CHECK ({expression})"
                )
            rows = (await conn.execute(_PROBE_CONSTRAINTS_SQL, {"probe": probe})).all()
            for constraintName, definition in rows:
                rendered[(tableName, constraintName)] = definition
        finally:
            await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {probe}")
    return rendered


async def test_db_check_constraints_are_all_declared_in_orm() -> None:
    """DB 上的每个命名 CHECK 约束，ORM 都必须声明（否则 alembic 会提议 DROP）。"""
    engine = await _pg_support._newEngine()
    try:
        async with engine.connect() as conn:
            await _assertDbAtHead(conn)
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
    engine = await _pg_support._newEngine()
    try:
        async with engine.connect() as conn:
            await _assertDbAtHead(conn)
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
    ormConstraints = _ormCheckConstraints()
    engine = await _pg_support._newEngine()
    try:
        async with engine.connect() as conn:
            await _assertDbAtHead(conn)
            dbConstraints = await _readDbConstraints(conn)
            rendered = await _renderOrmConstraints(conn, ormConstraints)
    finally:
        await engine.dispose()

    # 探针没渲染出来就静默跳过 = 假绿 ⇒ 这里先硬断言集合相等。
    assert set(rendered) == set(ormConstraints), (
        "ORM 的 CHECK 约束未能在探针表上渲染出来（缺约束名 / 表达式被 PG 拒绝 / 探针表建失败）: "
        f"缺少 {sorted(set(ormConstraints) - set(rendered))}"
    )

    mismatches: list[str] = []
    for (tableName, name), dbDefinition in dbConstraints.items():
        ormDefinition = rendered.get((tableName, name))
        if ormDefinition is None:
            # DB 有、ORM 没有 —— 由 test_db_check_constraints_are_all_declared_in_orm 负责；
            # 这里必须显式记一条，不能静默跳过（否则集合相等断言之外仍有盲区）。
            mismatches.append(f"{tableName}.{name}: ORM 未声明该约束")
            continue
        if ormDefinition != dbDefinition:
            mismatches.append(f"{tableName}.{name}:\n  ORM={ormDefinition}\n  DB ={dbDefinition}")
    assert mismatches == [], f"CHECK 约束表达式与 DB 不一致: {mismatches}"
