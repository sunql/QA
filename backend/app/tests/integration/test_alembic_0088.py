"""验证 Alembic 0091：`wiki_ontology_link` + `nl2sql_wiki_trace` 两张表落库（feat-wiki-ontology-link）。

契约：
- upgrade head 后真实 PG 上存在 `wiki_ontology_link` 与 `nl2sql_wiki_trace` 两表。
- downgrade 0090 后两表被撤，再 upgrade head 恢复 —— 双向对称。

为什么真跑 alembic：「跑完库里剩什么」是 alembic 运行时的可观测结果，抄写 DDL 重放
证明不了（抄写本身就可能抄错）。代价是会短暂改动测试库 `alembic_version`，
故整个降级-升级循环由 try/finally 兜底复原。

安全闸：`alembic/env.py` 只读 `DATABASE_URL`（`TEST_DATABASE_URL` 被静默忽略），
传错就打到 **prod**。故 shell 出去之前强制校验库名，不匹配直接失败、不执行任何 DDL。

与 0076 测试的差异：本测试不钉版本号（链 0087→0088→0089→0090→0091 还在长；
断言「upgrade head 后表存在」已经足够说明 0091 是 head 的下家）。如果要钉就钉
`0091_wiki_ontology_link`，但耦合链的版本号易触发 false-positive（别人补迁移就挂）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests._pg_support import resolveTestDatabaseUrl

_BACKEND_ROOT = Path(__file__).resolve().parents[3]  # backend/
_REQUIRED_DB = "qa_metadata_test"

_REVISION = "0091_wiki_ontology_link"
_PREVIOUS = "0090_llm_cache_hit_multiplier"
_TABLE_LINK = "wiki_ontology_link"
_TABLE_TRACE = "nl2sql_wiki_trace"

_ALEMBIC_TIMEOUT_SEC = 120


def _assertTestDb(url: str) -> None:
    """写库闸：只认测试库，否则 raise（不执行任何 DDL）。"""
    match = re.search(r"/([^/?]+)(?:\?|$)", url)
    dbName = match.group(1) if match else ""
    if dbName != _REQUIRED_DB:
        raise RuntimeError(
            f"拒绝对非测试库执行迁移：解析到库名 {dbName!r}，要求 {_REQUIRED_DB!r}。"
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


async def _tableNames(dbSession: AsyncSession) -> set[str]:
    """读真实 PG：当前 schema 的所有表名（alembic 跨连接 DDL 必须 rollback 刷快照）。"""
    await dbSession.rollback()

    def _sync_inspect(sync_session) -> set[str]:
        return set(inspect(sync_session.bind).get_table_names())

    return await dbSession.run_sync(_sync_inspect)


@pytest.mark.asyncio
async def test_0091_creates_wiki_ontology_link_tables(dbSession: AsyncSession) -> None:
    """upgrade head 后：`wiki_ontology_link` 与 `nl2sql_wiki_trace` 两表实际落库。

    断言两表都在 —— 不钉 head 版本号（链会继续长）。
    """
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    # Arrange：升级到 head（含本迁移的最终状态）
    _alembic(url, "upgrade", "head")

    # Act
    tables = await _tableNames(dbSession)

    # Assert
    assert _TABLE_LINK in tables, (
        f"upgrade head 后 {_TABLE_LINK!r} 表不存在 —— 0091 迁移未生效"
    )
    assert _TABLE_TRACE in tables, (
        f"upgrade head 后 {_TABLE_TRACE!r} 表不存在 —— 0091 迁移未生效"
    )


@pytest.mark.asyncio
async def test_0091_downgrade_removes_tables_and_upgrade_restores(
    dbSession: AsyncSession,
) -> None:
    """双向对称：downgrade 0090 后两表被撤，再 upgrade head 复原（finally）。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    # Arrange：先把测试库升到 head（含 0091）
    _alembic(url, "upgrade", "head")
    before = await _tableNames(dbSession)
    assert _TABLE_LINK in before and _TABLE_TRACE in before, (
        "起点不在 head：迁移表不在测试库"
    )

    try:
        # Act：降级到 0090
        _alembic(url, "downgrade", _PREVIOUS)
        after = await _tableNames(dbSession)
        version = (
            await dbSession.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()

        # Assert：两张表都撤掉、alembic_version 落在 0090
        assert version == _PREVIOUS, (
            f"downgrade 后 alembic_version = {version!r}，期望 {_PREVIOUS!r}"
        )
        assert _TABLE_LINK not in after, (
            f"downgrade 0091 之后 {_TABLE_LINK!r} 仍存在 —— 降级没有对称反转 upgrade"
        )
        assert _TABLE_TRACE not in after, (
            f"downgrade 0091 之后 {_TABLE_TRACE!r} 仍存在 —— 降级没有对称反转 upgrade"
        )
    finally:
        # 无论如何恢复测试库到 head，避免污染后续测试
        _alembic(url, "upgrade", "head")

    restored = await _tableNames(dbSession)
    assert _TABLE_LINK in restored, (
        "升级回来后 _TABLE_LINK 没恢复 —— 本测试把测试库留在了坏状态"
    )
    assert _TABLE_TRACE in restored, (
        "升级回来后 _TABLE_TRACE 没恢复 —— 本测试把测试库留在了坏状态"
    )
