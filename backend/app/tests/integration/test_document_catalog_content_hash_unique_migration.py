"""验证 Alembic 0062：`document_catalog.content_hash` 唯一索引独立成档。

**为什么这条索引要被拆出来（本测试存在的全部理由）**：运维上需要一类操作 ——
「只撤唯一索引、保留另外两项」。唯一索引是三项里唯一有**阻断**能力的：留下一行
`content_hash = X` 就能永久占住该哈希，此后任何哈希为 X 的**合法**上传/导入一律
409（删掉那行反而释放哈希，所以「删文档」替代不了这个能力）。
原先这条索引写在 0061 里，而 `downgrade()` 必须对称反转 `upgrade()`，三项只能
一起撤；若为「只撤索引」把它改成不对称，库就会停在一个**没有任何 revision
描述**的状态（`alembic_version` = 0060 而两列仍在）—— 而「版本号相同 ≠ 结构
相同」正是本项目历史「两库结构漂移」的病根。拆开后，
**撤索引 = `alembic downgrade 0062`**：一条命令、只撤这一项、版本号如实反映结构。

**本测试真的跑 alembic**，与本目录 `test_schema_reconcile_migration.py`
（那里刻意**不**跑，理由是「会改 alembic_version」）形成有意的对照：
本条要求问的是「降级跑完后库里剩下什么」，这是 alembic **运行时**的可观测结果，
把 DDL 抄一遍重放证明不了 —— 抄写本身就可能抄错，而"抄错"正是要防的事。
代价是它会短暂改动测试库的 `alembic_version`，故整个降级-升级循环由
`try/finally` 兜底复原；循环之外任何断言失败都不会把库留在 0061。

安全闸：`alembic/env.py` **只读 `DATABASE_URL`**（`TEST_DATABASE_URL` 会被静默
忽略 —— 见 Harness 档案与迁移 0062 的 docstring），传错就打到 **prod**。
故 shell 出去之前**强制校验库名**，不匹配直接失败、不执行任何 DDL。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests._pg_support import resolveTestDatabaseUrl

# 本文件**混有非 async 的静态断言测试**（读迁移源码），故不设模块级 pytestmark
# —— 否则会连同它们一起打上 asyncio 标记，pytest 每次都要报
# "marked with '@pytest.mark.asyncio' but it is not an async function"。
# 两个需要库的测试各自显式标注。

_BACKEND_ROOT = Path(__file__).resolve().parents[3]  # backend/
_REQUIRED_DB = "qa_metadata_test"

_HEAD = "0062_doc_catalog_hash_unique"
_PREV = "0061_wiki_dedup"
_UNIQUE_INDEX = "uq_document_catalog_content_hash"
_NON_UNIQUE_INDEX = "ix_wiki_page_content_hash"

# 子进程上限：正常情况下 alembic 几秒内返回。超时说明被锁住（例如某个会话
# 持有 document_catalog 的 AccessShareLock 挡住 DROP INDEX）—— 让测试明确失败
# 而不是挂死，是本项目踩过锁等待的教训。
_ALEMBIC_TIMEOUT_SEC = 120


def _assertTestDb(url: str) -> None:
    """写库闸：只认测试库，否则 raise（不执行任何 DDL）。"""
    match = re.search(r"/([^/?]+)(?:\?|$)", url)
    dbName = match.group(1) if match else ""
    if dbName != _REQUIRED_DB:
        raise RuntimeError(
            f"拒绝对非测试库执行迁移降级：解析到库名 {dbName!r}，要求 {_REQUIRED_DB!r}。"
            "alembic/env.py 只读 DATABASE_URL（TEST_DATABASE_URL 被静默忽略），"
            "传错就会打到 prod。"
        )


def _alembic(url: str, *args: str) -> str:
    """在测试库上跑一条 alembic 子命令；先过写库闸。"""
    _assertTestDb(url)
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_ALEMBIC_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} 失败（rc={result.returncode}）：{result.stderr[-2000:]}"
    )
    return result.stdout


async def _snapshot(dbSession: AsyncSession) -> dict[str, Any]:
    """读真实 PG：版本号 + 三项的存亡 + 唯一索引的方向。

    先 rollback 丢掉可能持有的旧快照，否则 alembic 在**另一个连接**上做的 DDL
    在本会话里看不见（READ COMMITTED 下 REPEATABLE 快照未刷新）。
    """
    await dbSession.rollback()

    async def existsIndex(name: str) -> bool:
        return bool(
            (
                await dbSession.execute(
                    text("SELECT count(*) FROM pg_indexes WHERE indexname = :n"),
                    {"n": name},
                )
            ).scalar_one()
        )

    async def existsColumn(table: str, column: str) -> bool:
        return bool(
            (
                await dbSession.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = :c"
                    ),
                    {"t": table, "c": column},
                )
            ).scalar_one()
        )

    indexDef = (
        await dbSession.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
            {"n": _UNIQUE_INDEX},
        )
    ).scalar_one_or_none()

    return {
        "version": (
            await dbSession.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one(),
        "uniqueIndex": await existsIndex(_UNIQUE_INDEX),
        "uniqueIndexDef": indexDef,
        "nonUniqueIndex": await existsIndex(_NON_UNIQUE_INDEX),
        "contentHashColumn": await existsColumn("wiki_page", "content_hash"),
        "skippedPagesColumn": await existsColumn("wiki_import_task", "skipped_pages"),
    }


@pytest.mark.asyncio
async def test_downgrade_0062_drops_only_the_unique_index(
    dbSession: AsyncSession,
) -> None:
    """**本变更的核心断言**：`alembic downgrade 0062` 只撤唯一索引，另三项原样保留。

    这三条「保留」断言就是本迁移独立成档的验收标准 —— 任何一条打红，都说明
    「只撤索引」这个操作已经不可用，运维就只剩「连 content_hash 列一起删掉」
    或「手工 DROP INDEX」这两条退路（前者丢数据、后者绕过版本管理）。
    """
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    # Arrange：确保起点是 head（幂等，若已在 head 则为 no-op）
    _alembic(url, "upgrade", "head")
    atHead = await _snapshot(dbSession)
    assert atHead["version"] == _HEAD
    assert atHead["uniqueIndex"] is True, "起点不在 head：唯一索引缺失"

    try:
        # Act
        _alembic(url, "downgrade", _PREV)
        after = await _snapshot(dbSession)

        # Assert：撤掉的那一项
        assert after["version"] == _PREV
        assert after["uniqueIndex"] is False, (
            "downgrade 0062 之后唯一索引仍在 —— 撤离操作没有生效"
        )

        # Assert：必须保留的三项
        assert after["nonUniqueIndex"] is True, (
            "downgrade 0062 把 wiki_page 的非唯一索引也撤了。它是**另一项**，"
            "不该随索引撤离而消失（0062 的 downgrade 只该 DROP 一条索引）。"
        )
        assert after["contentHashColumn"] is True, (
            "downgrade 0062 把 wiki_page.content_hash 列也撤了 —— 会丢掉已回填的摘要，"
            "且正是拆分要避免的「三项一起撤」"
        )
        assert after["skippedPagesColumn"] is True, (
            "downgrade 0062 把 wiki_import_task.skipped_pages 列也撤了 —— "
            "会让「重复项记跳过」失去台账字段，正是拆分要避免的「三项一起撤」"
        )
    finally:
        # 无论如何把库恢复到 head，避免把测试库留在 0061 影响后续测试
        _alembic(url, "upgrade", "head")

    restored = await _snapshot(dbSession)
    assert restored["version"] == _HEAD
    assert restored["uniqueIndex"] is True, (
        "升级回来之后唯一索引没恢复 —— 本测试把测试库留在了坏状态"
    )
    assert "UNIQUE" in (restored["uniqueIndexDef"] or "").upper()


@pytest.mark.asyncio
async def test_unique_index_allows_null_content_hash(dbSession: AsyncSession) -> None:
    """唯一索引不挡 `content_hash IS NULL` 的行（PostgreSQL 视 NULL 互不相等）。

    0062 **不回填**历史行，所以「行存在但摘要为 NULL」是升级后的常态形状；
    若索引被改成 `NULLS NOT DISTINCT`，这些行会互相冲突，写入立刻炸。
    这条把该依赖钉住：换数据库或改索引定义时马上打红。
    """
    await dbSession.rollback()
    indexDef = (
        await dbSession.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
            {"n": _UNIQUE_INDEX},
        )
    ).scalar_one()
    assert "UNIQUE" in indexDef.upper()
    assert "NULLS NOT DISTINCT" not in indexDef.upper(), (
        "索引变成了 NULLS NOT DISTINCT —— content_hash 为 NULL 的行会互相冲突，"
        "而 0062 不回填历史行，写入会立刻炸"
    )


def test_0062_downgrade_touches_exactly_one_object() -> None:
    """源码级钉死：0062 的 `downgrade()` 有且只有一条 DDL，且撤的是那条唯一索引。

    与上面真跑 alembic 的测试互补 —— 那个证明「跑完库里剩什么」，这个证明
    「不会有人后来往里塞第二/第三条」，且**不必连库**就能跑（静态契约断言，
    参考本项目前端 `VirtualList` 源码断言的先例）。
    """
    source = (
        _BACKEND_ROOT / "alembic" / "versions" / "0062_doc_catalog_hash_unique.py"
    ).read_text(encoding="utf-8")

    body = source.split("def downgrade() -> None:")[1]
    statements = [line.strip() for line in body.splitlines() if "op.execute(" in line]
    assert len(statements) == 1, (
        f"0062 的 downgrade 应当只有一条 DDL（撤唯一索引），实际 {len(statements)} 条："
        f"{statements}。多出来的每一条都会让「只撤索引」不再成立。"
    )

    # 只对**可执行 DDL** 设限，不限制注释：0062 的注释里刻意写了「另两项不在此处」
    # 作为说明，那是要保留的文档，不是要禁止的对象引用。
    ddl = "\n".join(statements)

    assert _UNIQUE_INDEX in ddl, (
        f"0062 的 downgrade 没有撤 {_UNIQUE_INDEX} —— 撤错了对象"
    )
    for other in (_NON_UNIQUE_INDEX, "wiki_page", "skipped_pages"):
        assert other not in ddl, (
            f"0062 的 downgrade 实际执行的 DDL 里出现了 {other!r} —— 那不属于本迁移，"
            "「只撤唯一索引」的前提被破坏"
        )


def test_0061_no_longer_owns_the_unique_index() -> None:
    """0061 不得再引用唯一索引 —— 否则「撤索引」又会被它的对称 downgrade 带走。

    这是拆分的另一半：只把索引加进 0062 而忘了从 0061 摘掉，两条迁移会同时
    声称拥有该索引，`downgrade 0061` 又会把它撤掉，需求悄悄失效。
    """
    source = (
        _BACKEND_ROOT / "alembic" / "versions" / "0061_wiki_dedup.py"
    ).read_text(encoding="utf-8")

    body = source.split('"""')[2]  # 跳过模块 docstring（其中会**提到**索引名作说明）
    assert "op.execute" in body, "分割点不对：取到的不是代码体"
    assert _UNIQUE_INDEX not in body, (
        f"0061 的代码体里仍有 {_UNIQUE_INDEX} —— 唯一索引没有被真正摘出去，"
        "`alembic downgrade 0061` 会再次把它撤掉"
    )
