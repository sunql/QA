"""Schema drift 检测（Harness 强制：工程结构.md §Schema 漂移防护）。

启动时检测：
1. Alembic head（最新迁移 revision）与 DB `alembic_version` 当前值是否一致
2. ORM 模型表集合 vs DB `pg_tables` 表集合的差集
3. **列级**：ORM 声明 vs DB `information_schema.columns`（逐表）
4. **索引级**：ORM 声明 vs DB `pg_indexes`／`pg_index`（逐表，含列序比对）

返回码：
- 0：无 blocking 漂移（可能：DB 是空库无 alembic_version 表；或已 upgrade head）
- 1：发现漂移（blocking 项；或 `--strict` 下的任意项）
- 2：环境错误（DATABASE_URL 未指向 PG / 连接失败）

## 严重级别（启动阻断策略的单一事实来源）

- **blocking**：ORM 声明了但 DB 没有（表 / 列 / 索引）。运行期必然炸
  （`UndefinedColumn` 之类），必须在启动期挡住。
- **warning**：DB 多出来的对象。**不影响正确性**，且**绝不能自动删** ——
  多为手工 DDL 残留或旧迁移遗留。默认只告警；CI 用 `--strict` 升级为门禁。

这条线不是拍脑袋划的：prod `users` 上那 4 个「死列」带着唯一 admin 的非空 bcrypt
凭据，判成 blocking 等于让线上直接起不来；而 prod 缺 `ix_menu_config_visible_sort`
则是结构漂移，该挡。

## 为什么粒度从「表」扩到「列 + 索引」（2026-09-12）

两库那次漂移**全部落在列/索引级**（prod 缺一个索引、多 4 列），而本脚本当时只比
表集合，全程零告警。见 `Harness/changes/fix-schema-drift-two-dbs/`。

扩粒度之后最大的风险从「漏报」翻转成「误报」，因此归一化是这里的核心难点：

- PG 给每个 PK / UniqueConstraint 都建**支撑索引**，而 ORM 侧的 `UniqueConstraint`
  通常**无名**（`name=None`），DB 侧却有名字（`uq_xxx` / `<table>_<col>_key`）。
  只按名字比会在本仓库真实 schema 上凭空造出上百条「DB 多出索引」。
  → 对约束支撑的索引改用**列集合**认亲（`uniqueColumnSets`），名字只作补充。
- 表达式索引（`lower(x)`、`x DESC`）在 DB 侧只能读到 `<expr>`，**列名不可比**。
  → 任一侧是表达式即跳过列比对，绝不猜。（`ix_agent_definition_suggested_at` 这类
  `DESC` 索引正是 autogenerate 的知名噪音源。）

设计原则：
- 不修改 DB（纯只读 SELECT）
- 输出明确列出漂移项，便于开发者立即修复（alembic upgrade head / 清理 DB）
- 支持 CI 调用：脚本退出码即可作为门禁
- 支持运行时调用：lifespan 在启动时跑，blocking 漂移即 fail-fast
- 异步：项目仅 asyncpg，sync engine 不能直连

## 为什么住在 app/infrastructure/ 而不是 scripts/（2026-09-12 搬迁）

本模块与 `app/main.py` 是**一对必须同版本部署的两半**：main.py 调
`_checkDriftAsync` 拿 issue，再用 `_splitBySeverity` 判是否阻断。放在 `scripts/`
时两者分处两个目录，`docker cp backend/app/.`（《部署》文档里的容器更新手法）
只会带上 main.py，容器里留下**旧版**本模块 —— 实测两种错配都会炸：

- 新 main + 旧本模块：旧版返回 `list[str]`，`_splitBySeverity` 读 `.severity`
  → `AttributeError`，且该调用在 main.py 的 `try/except` **之外**，
  连「请执行 alembic upgrade head」都打不出来
- 旧 main + 新本模块：旧 main 是 `if issues:`，本模块新增的 DB 多余索引 warning
  会把它顶成真 → 容器直接起不来

搬进 `app/` 后这一个 `docker cp` 就同时带上两半，错配**结构性不可能**。
`scripts/check_schema_drift.py` 保留为薄 shim，仅为不打断既有的命令行用法与文档。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import (
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.asyncio import create_async_engine

logger = logging.getLogger(__name__)

# backend/ 根目录：本文件在 app/infrastructure/ 下，故上溯两层
# （parents[0]=infrastructure, [1]=app, [2]=backend）。搬迁目录时这个深度必须跟着改，
# 否则 _ALEMBIC_VERSIONS_DIR 指空 → heads=[] → 报「迁移链多 head 或无 head」。
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_VERSIONS_DIR = _BACKEND_ROOT / "alembic" / "versions"

_REVISION_LINE = re.compile(
    r"^revision\s*:?\s*[^=]*=\s*[\"'](\w+)[\"']", re.MULTILINE
)
_DOWN_REVISION_LINE = re.compile(
    r"^down_revision\s*:?\s*[^=]*=\s*(.*?)(?=\n[a-zA-Z_]|\n\n|\Z)",
    re.MULTILINE | re.DOTALL,
)

# DB 侧表达式索引在列位置上读不到列名，用这个哨兵占位
_EXPR_SENTINEL = "<expr>"


# --------------------------------------------------------------------------
# 严重级别 / 渲染（启动阻断策略的单一事实来源）
# --------------------------------------------------------------------------

_SEVERITY_BY_CODE: dict[str, str] = {
    "alembic": "blocking",
    "missing_table": "blocking",
    "extra_table": "warning",
    "missing_column": "blocking",
    "extra_column": "warning",
    "missing_index": "blocking",
    "index_mismatch": "blocking",
    "extra_index": "warning",
}

_LABEL_BY_CODE: dict[str, str] = {
    "alembic": "迁移版本不一致",
    "missing_table": "ORM 声明但 DB 缺失的表",
    "extra_table": "DB 存在但 ORM 未声明的表",
    "missing_column": "ORM 声明但 DB 缺失的列",
    "extra_column": "DB 存在但 ORM 未声明的列",
    "missing_index": "ORM 声明但 DB 缺失的索引",
    "index_mismatch": "同名但定义不一致的索引",
    "extra_index": "DB 存在但 ORM 未声明的索引",
}

_PREFIX_BY_CODE: dict[str, str] = {
    "alembic": "[alembic]",
    "missing_table": "[orm:table]",
    "extra_table": "[db:table]",
    "missing_column": "[orm:column]",
    "extra_column": "[db:column]",
    "missing_index": "[orm:index]",
    "index_mismatch": "[orm:index]",
    "extra_index": "[db:index]",
}

_ADVICE_BY_CODE: dict[str, str] = {
    "alembic": "。请执行 alembic upgrade head。",
    "missing_table": "。模型已被代码引用但迁移未应用，请执行 alembic upgrade head。",
    "extra_table": "。多为旧迁移残留 / 手工建表；确认无用后再处理，禁止自动 DROP。",
    "missing_column": "。运行期查询会直接报 UndefinedColumn，请执行 alembic upgrade head。",
    "extra_column": "。多为手工 DDL 残留；删前务必确认无真实数据（禁止自动 DROP）。",
    "missing_index": "。结构漂移，请执行 alembic upgrade head 补建。",
    "index_mismatch": "。ORM 与 DB 对同一索引的定义不同，请核对迁移与模型。",
    "extra_index": "。多为历史遗留，只影响写放大，不阻断启动。",
}


@dataclass(frozen=True)
class DriftIssue:
    """一条漂移。`detail` 是原子描述（如 `users.last_login_ip`），渲染时按 code 归类合并。"""

    code: str
    detail: str
    severity: str

    @property
    def message(self) -> str:
        return f"{_PREFIX_BY_CODE[self.code]} {_LABEL_BY_CODE[self.code]}：{self.detail}"

    def __str__(self) -> str:
        return self.message


def _issue(code: str, detail: str) -> DriftIssue:
    return DriftIssue(code=code, detail=detail, severity=_SEVERITY_BY_CODE[code])


def _splitBySeverity(
    issues: list[DriftIssue],
) -> tuple[list[DriftIssue], list[DriftIssue]]:
    """按 blocking / warning 分流。调用方（CLI 与 lifespan）共用，避免策略漂移。"""
    blocking = [i for i in issues if i.severity == "blocking"]
    warnings = [i for i in issues if i.severity == "warning"]
    return blocking, warnings


def _formatIssues(issues: list[DriftIssue]) -> list[DriftIssue]:
    """同 code 的漂移合并为一条。

    真实库上有 26 个 ORM 未声明的历史索引，逐条打日志会造成告警疲劳 ——
    然后大家就去设 SKIP_SCHEMA_CHECK=1，防线等于没有。
    """
    grouped: dict[str, list[str]] = {}
    for issue in issues:
        grouped.setdefault(issue.code, []).append(issue.detail)

    order = list(_LABEL_BY_CODE)
    return [
        _issue(code, ", ".join(details))
        for code, details in sorted(grouped.items(), key=lambda kv: order.index(kv[0]))
    ]


# --------------------------------------------------------------------------
# 形状（ORM 与 DB 各归一化成一个可比的抽象）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OrmTableShape:
    """ORM 侧一张表的可比形状。"""

    columns: frozenset[str]
    # 索引名 → 列名元组；None 表示表达式索引（列不可比）
    indexColumns: dict[str, tuple[str, ...] | None]
    # PK / UniqueConstraint 的列集合 —— 用于认领 PG 的约束支撑索引
    uniqueColumnSets: frozenset[tuple[str, ...]]


@dataclass(frozen=True)
class DbIndexShape:
    """DB 侧一个索引的形状（`pg_index` 直读，含列序）。"""

    name: str
    columns: tuple[str, ...]
    isPrimary: bool
    isUnique: bool
    constraintNames: tuple[str, ...]


def _indexColumnsOf(index: object) -> tuple[str, ...] | None:
    """取索引列名；含表达式元素（`lower(x)` / `text("x DESC")`）时返回 None。"""
    names: list[str] = []
    for expr in index.expressions:  # type: ignore[attr-defined]
        name = getattr(expr, "name", None)
        if name is None or not hasattr(expr, "table"):
            return None
        names.append(str(name))
    return tuple(names)


def _ormTableShape(table: Table) -> OrmTableShape:
    """从 ORM `Table` 提取可比形状。"""
    indexColumns: dict[str, tuple[str, ...] | None] = {}
    for index in table.indexes:
        indexColumns[index.name] = _indexColumnsOf(index)

    uniqueColumnSets = {
        tuple(sorted(column.name for column in constraint.columns))
        for constraint in table.constraints
        if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint))
    }

    return OrmTableShape(
        columns=frozenset(table.columns.keys()),
        indexColumns=indexColumns,
        uniqueColumnSets=frozenset(uniqueColumnSets),
    )


def _ormTableShapes() -> dict[str, OrmTableShape]:
    """ORM Base.metadata 中所有表的形状（延迟导入，避免 alembic 环境外的 config 副作用）。"""
    from app.domain.models import Base

    return {name: _ormTableShape(t) for name, t in Base.metadata.tables.items()}


def _isCoveredIndex(dbIndex: DbIndexShape, orm: OrmTableShape) -> bool:
    """DB 上的索引是否被 ORM「已知」（哪怕两边名字不同）。

    关键是最后一条：ORM 的 `UniqueConstraint` 多数**无名**（`name=None`），
    而 PG 侧的支撑索引总有个名字（`uq_users_code` / `<table>_<col>_key`）。
    名字对不上时，只能按列集合认亲 —— 否则真实库上会出上百条假阳性。
    """
    if not (dbIndex.isPrimary or dbIndex.isUnique or dbIndex.constraintNames):
        return False
    return tuple(sorted(dbIndex.columns)) in orm.uniqueColumnSets


def _compareTable(
    tableName: str,
    orm: OrmTableShape,
    dbColumns: set[str],
    dbIndexes: list[DbIndexShape],
) -> list[DriftIssue]:
    """逐表比对列与索引，返回原子漂移项。"""
    issues: list[DriftIssue] = []

    issues += [
        _issue("missing_column", f"{tableName}.{column}")
        for column in sorted(orm.columns - dbColumns)
    ]
    issues += [
        _issue("extra_column", f"{tableName}.{column}")
        for column in sorted(dbColumns - orm.columns)
    ]

    dbByName = {index.name: index for index in dbIndexes}

    issues += [
        _issue("missing_index", f"{tableName}.{name}")
        for name in sorted(set(orm.indexColumns) - set(dbByName))
    ]

    for name, dbIndex in sorted(dbByName.items()):
        if name in orm.indexColumns:
            ormColumns = orm.indexColumns[name]
            # 任一侧是表达式索引 → 列名不可比，跳过而不是猜
            if ormColumns is None or _EXPR_SENTINEL in dbIndex.columns:
                continue
            if ormColumns != dbIndex.columns:
                issues.append(
                    _issue(
                        "index_mismatch",
                        f"{tableName}.{name}"
                        f"（ORM={list(ormColumns)} DB={list(dbIndex.columns)}）",
                    )
                )
            continue
        if not _isCoveredIndex(dbIndex, orm):
            issues.append(_issue("extra_index", f"{tableName}.{name}"))

    return issues


# --------------------------------------------------------------------------
# Alembic 迁移图解析
# --------------------------------------------------------------------------


def _parseRevisionFiles(versionsDir: Path) -> tuple[set[str], dict[str, str | None]]:
    """解析 alembic/versions/*.py，提取 (所有 revisions, {revision: down_revision})。

    支持两种 Alembic 写法：
    - 现代：revision: str = "0023_kpi_catalog" / down_revision: str | None = "0022_..."
    - 旧式：revision = "0006_session_query_state" / down_revision = "0005_..."
    """
    revisions: set[str] = set()
    graph: dict[str, str | None] = {}
    # 提取形如 "..." 或 '...' 的字符串字面量（用于 down_revision 值）
    string_lit_re = re.compile(r'["\']([A-Za-z0-9_]+)["\']')

    for path in sorted(versionsDir.glob("*.py")):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue
        content = path.read_text(encoding="utf-8")
        rev_match = _REVISION_LINE.search(content)
        if not rev_match:
            continue
        rev = rev_match.group(1)
        revisions.add(rev)
        down = None
        down_match = _DOWN_REVISION_LINE.search(content)
        if down_match:
            rhs = down_match.group(1)
            # rhs 形如 `str | None = "0022_..."` 或 `= "0005_..."` 或 `Union[str, None] = "0014_..."`
            # 找第一个字符串字面量作为 down 值；找不到则 None
            str_match = string_lit_re.search(rhs)
            down = str_match.group(1) if str_match else None
        graph[rev] = down
    return revisions, graph


def _findHead(graph: dict[str, str | None]) -> str:
    """找 head（无任何 revision 把它当 down_revision）。"""
    all_down_revs = {d for d in graph.values() if d is not None}
    heads = [rev for rev in graph if rev not in all_down_revs]
    if len(heads) != 1:
        raise RuntimeError(
            f"Alembic 迁移链存在多 head 或无 head（heads={heads}）。"
            "禁止合并未关闭的分支。"
        )
    return heads[0]


# --------------------------------------------------------------------------
# DB 侧只读查询
# --------------------------------------------------------------------------

_DB_COLUMNS_SQL = text(
    "SELECT table_name, column_name FROM information_schema.columns "
    "WHERE table_schema = 'public'"
)

# 直接从 pg_index 读，而不是解析 pg_indexes.indexdef 的文本：
# 结构化读到的是列序与表达式标记，不必和 DDL 字符串的写法较劲。
_DB_INDEXES_SQL = text(
    """
    SELECT t.relname AS table_name,
           i.relname AS index_name,
           ix.indisprimary AS is_primary,
           ix.indisunique AS is_unique,
           ARRAY(
             SELECT con.conname FROM pg_constraint con
             WHERE con.conindid = ix.indexrelid
           ) AS constraint_names,
           ARRAY(
             SELECT COALESCE(a.attname, '<expr>')
             FROM unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord)
             LEFT JOIN pg_attribute a
               ON a.attrelid = t.oid AND a.attnum = k.attnum
             ORDER BY k.ord
           ) AS columns
    FROM pg_index ix
    JOIN pg_class i ON i.oid = ix.indexrelid
    JOIN pg_class t ON t.oid = ix.indrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE n.nspname = 'public'
    """
)


async def _queryCurrentRevision(engine) -> str | None:
    """查询 DB 当前 alembic_version。表不存在返回 None（全新空库）。"""
    async with engine.connect() as conn:
        try:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            row = result.first()
            return row[0] if row else None
        except Exception:
            return None


async def _queryDbTables(engine) -> set[str]:
    """列出 DB public schema 下的所有表名（排除 alembic_version）。"""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        )
        return {row[0] for row in result}


async def _queryDbColumns(engine) -> dict[str, set[str]]:
    """DB public schema 下每张表的列名集合。"""
    async with engine.connect() as conn:
        rows = (await conn.execute(_DB_COLUMNS_SQL)).all()

    columns: dict[str, set[str]] = {}
    for tableName, columnName in rows:
        columns.setdefault(tableName, set()).add(columnName)
    return columns


async def _queryDbIndexes(engine) -> dict[str, list[DbIndexShape]]:
    """DB public schema 下每张表的索引形状（列序 + 表达式标记 + 约束归属）。"""
    async with engine.connect() as conn:
        rows = (await conn.execute(_DB_INDEXES_SQL)).mappings().all()

    indexes: dict[str, list[DbIndexShape]] = {}
    for row in rows:
        indexes.setdefault(row["table_name"], []).append(
            DbIndexShape(
                name=row["index_name"],
                columns=tuple(row["columns"]),
                isPrimary=row["is_primary"],
                isUnique=row["is_unique"],
                constraintNames=tuple(row["constraint_names"]),
            )
        )
    return indexes


# --------------------------------------------------------------------------
# 主检查
# --------------------------------------------------------------------------


async def _checkAlembic(engine) -> list[DriftIssue]:
    revisions, graph = _parseRevisionFiles(_ALEMBIC_VERSIONS_DIR)
    head = _findHead(graph)
    current = await _queryCurrentRevision(engine)

    if current is None:
        return [
            _issue(
                "alembic",
                f"DB 无 alembic_version 表（空库或表不存在），预期 head={head}",
            )
        ]
    if current not in revisions:
        return [
            _issue(
                "alembic",
                f"DB 当前版本 {current} 不在迁移文件集合中（旧版本残留）",
            )
        ]
    if current != head:
        return [
            _issue("alembic", f"DB 版本滞后：current={current} head={head}")
        ]
    return []


async def _checkDriftAsync(engine) -> list[DriftIssue]:
    """执行漂移检查，返回漂移项（空 = 无漂移）。

    表级沿用原实现；列/索引级只比对 **ORM 与 DB 都有的表** —— 只在一边存在的表
    已由表级检查报出，再逐列报一遍是重复噪音。
    """
    issues: list[DriftIssue] = list(await _checkAlembic(engine))

    ormShapes = _ormTableShapes()
    dbTables = await _queryDbTables(engine)

    issues += [
        _issue("missing_table", name)
        for name in sorted(set(ormShapes) - dbTables)
    ]
    issues += [
        _issue("extra_table", name)
        for name in sorted(dbTables - set(ormShapes))
    ]

    commonTables = set(ormShapes) & dbTables
    dbColumns = await _queryDbColumns(engine)
    dbIndexes = await _queryDbIndexes(engine)

    for tableName in sorted(commonTables):
        issues += _compareTable(
            tableName,
            ormShapes[tableName],
            dbColumns.get(tableName, set()),
            dbIndexes.get(tableName, []),
        )

    return _formatIssues(issues)


def _resolveDbUrl(arg_url: str | None) -> str:
    """解析 DB URL：优先 CLI 参数，其次 DATABASE_URL。"""
    if arg_url:
        return arg_url
    env_url = os.environ.get("DATABASE_URL", "")
    if not env_url:
        raise RuntimeError("未指定数据库：请传 --database-url 或设置 DATABASE_URL 环境变量")
    return env_url


async def _amain(args: argparse.Namespace) -> int:
    try:
        db_url = _resolveDbUrl(args.database_url)
    except RuntimeError as e:
        logger.error("参数错误: %s", e)
        return 2

    if not db_url.startswith("postgresql"):
        logger.error(
            "仅支持 PostgreSQL：%s",
            db_url.split("@")[-1] if "@" in db_url else db_url,
        )
        return 2

    try:
        engine = create_async_engine(db_url, echo=False)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        logger.error("连接数据库失败: %s", e)
        return 2

    try:
        issues = await _checkDriftAsync(engine)
    except Exception as e:
        logger.exception("Schema drift 检查失败: %s", e)
        return 2
    finally:
        await engine.dispose()

    if not issues:
        if not args.quiet:
            logger.info("Schema drift 检查通过：ORM 与 DB 一致（表 / 列 / 索引）。")
        return 0

    blocking, warnings = _splitBySeverity(issues)
    for issue in warnings:
        logger.warning("  - %s", issue)
    for issue in blocking:
        logger.error("  - %s", issue)

    if blocking:
        logger.error(
            "发现 %d 类 blocking 漂移（必须修复），启动阻断。"
            "修复：cd backend && uv run alembic upgrade head",
            len(blocking),
        )
        return 1

    if args.strict:
        logger.error(
            "发现 %d 类非阻断漂移，--strict 模式下同样视为失败（CI 门禁）。",
            len(warnings),
        )
        return 1

    logger.warning(
        "仅发现 DB 多余对象（历史残留，%d 类），默认不阻断启动。"
        "如需追溯可跑 --strict 或人工核对。",
        len(warnings),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Schema drift 检测（ORM vs DB：表/列/索引）")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
