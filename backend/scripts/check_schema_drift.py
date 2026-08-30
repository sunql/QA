"""Schema drift 检测（Harness 强制：工程结构.md §Schema 漂移防护）。

启动时检测：
1. Alembic head（最新迁移 revision）与 DB `alembic_version` 当前值是否一致
2. ORM 模型表集合 vs DB `pg_tables` 表集合的差集

返回码：
- 0：DB 与代码完全一致（可能：DB 是空库无 alembic_version 表；或已 upgrade head）
- 1：发现漂移（DB 缺表 / 版本滞后 / 多了未声明的表）
- 2：环境错误（DATABASE_URL 未指向 PG / 连接失败）

设计原则：
- 不修改 DB（纯只读）
- 输出明确列出漂移项，便于开发者立即修复（alembic upgrade head / 清理 DB）
- 支持 CI 调用：脚本退出码即可作为门禁
- 支持运行时调用：lifespan 在启动时跑，DB 不一致即 fail-fast
- 异步：项目仅 asyncpg，sync engine 不能直连
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_VERSIONS_DIR = _BACKEND_ROOT / "alembic" / "versions"

_REVISION_LINE = re.compile(
    r"^revision\s*:?\s*[^=]*=\s*[\"'](\w+)[\"']", re.MULTILINE
)
_DOWN_REVISION_LINE = re.compile(
    r"^down_revision\s*:?\s*[^=]*=\s*(.*?)(?=\n[a-zA-Z_]|\n\n|\Z)",
    re.MULTILINE | re.DOTALL,
)


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
            if str_match:
                down = str_match.group(1)
            else:
                down = None
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


async def _queryCurrentRevision(engine) -> str | None:
    """查询 DB 当前 alembic_version。表不存在返回 None（全新空库）。"""
    async with engine.connect() as conn:
        def_sync_conn = await conn.get_raw_connection()
        # 简单方法：直接尝试查询
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


def _queryOrmTables() -> set[str]:
    """列出 ORM Base.metadata 中声明的所有表名。"""
    # 延迟导入：避免在 alembic 环境外触发 config 副作用
    from app.domain.models import Base

    return set(Base.metadata.tables.keys())


async def _checkDriftAsync(engine) -> list[str]:
    """执行漂移检查，返回漂移项描述列表（空 = 无漂移）。"""
    issues: list[str] = []

    # (1) Alembic 版本对齐
    revisions, graph = _parseRevisionFiles(_ALEMBIC_VERSIONS_DIR)
    head = _findHead(graph)
    current = await _queryCurrentRevision(engine)

    if current is None:
        issues.append(
            f"[alembic] DB 无 alembic_version 表（空库或表不存在），预期 head={head}。"
            "请执行 alembic upgrade head。"
        )
    elif current not in revisions:
        issues.append(
            f"[alembic] DB 当前版本 {current} 不在迁移文件集合中。"
            "可能是旧版本残留，请执行 alembic upgrade head。"
        )
    elif current != head:
        issues.append(
            f"[alembic] DB 版本滞后：current={current} head={head}。"
            f"请执行 alembic upgrade {head}。"
        )

    # (2) ORM 表 vs DB 表
    orm_tables = _queryOrmTables()
    db_tables = await _queryDbTables(engine)

    missing_in_db = orm_tables - db_tables
    if missing_in_db:
        issues.append(
            "[orm] ORM 声明但 DB 缺失的表："
            + ", ".join(sorted(missing_in_db))
            + "。模型已被代码引用但迁移未应用，请执行 alembic upgrade head。"
        )

    extra_in_db = db_tables - orm_tables
    if extra_in_db:
        issues.append(
            "[orm] DB 存在但 ORM 未声明的表："
            + ", ".join(sorted(extra_in_db))
            + "。可能为旧迁移残留；如确认无用可手动 DROP，否则检查模型是否遗漏声明。"
        )

    return issues


# 同步包装（lifespan 中调用），复用 main 的 event loop
def _checkDrift(engine) -> list[str]:
    """同步包装：在调用方 event loop 中跑 async 任务。"""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(_checkDriftAsync(engine))


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
            logger.info("Schema drift 检查通过：ORM 与 DB 完全一致。")
        return 0

    logger.error("发现 %d 项 schema 漂移：", len(issues))
    for issue in issues:
        logger.error("  - %s", issue)
    logger.error(
        "修复方式：cd backend && uv run alembic upgrade head "
        "（如迁移文件已合并但未应用）；多余表需手工确认后 DROP。"
    )

    if args.strict:
        return 1
    # 默认模式：任何 blocking 漂移（alembic 滞后 / ORM 缺表）必须阻断；
    # ORM 多余表（历史残留）只警告不阻断。
    blocking_count = sum(
        1 for i in issues
        if i.startswith("[alembic]") or (i.startswith("[orm]") and "缺失" in i)
    )
    if blocking_count > 0:
        logger.error("其中 %d 项为 blocking 漂移（必须修复），启动阻断。", blocking_count)
        return 1
    logger.warning("仅发现 ORM 未声明的多余表（可能是历史残留），默认不阻断。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Schema drift 检测（ORM vs DB）")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())