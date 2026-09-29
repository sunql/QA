"""验证 Alembic 0098：evidence.claim_id 放开 NOT NULL（v3.1 任务 B2）。

契约（v3.1 蓝图 §5.7：SQL_QUERY 型证据创建时不挂 claim，related_claim_ids
由上层填充）：
- upgrade head 后真实 PG 上 evidence.claim_id 可空（is_nullable = YES）
- downgrade 0096 后恢复 NOT NULL（is_nullable = NO）—— downgrade 内置
  占位 UPDATE 兜住 NULL 行，re-add 约束不会因存量 SQL_QUERY 证据而失败
- 再 upgrade head 复原 —— 双向对称

为什么真跑 alembic：与 0088 测试同思路，「跑完库里剩什么」是 alembic 运行时
的可观测结果。安全闸：alembic/env.py 只读 DATABASE_URL，shell 出去之前强制
校验库名，不匹配直接失败、不执行任何 DDL。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests._pg_support import resolveTestDatabaseUrl

_BACKEND_ROOT = Path(__file__).resolve().parents[3]  # backend/
_REQUIRED_DB = "qa_metadata_test"

_REVISION = "0098"
_PREVIOUS = "0096"
_TABLE = "evidence"
_COLUMN = "claim_id"

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


async def _claimIdNullable(dbSession: AsyncSession) -> str:
    """读真实 PG information_schema：claim_id 的 is_nullable（rollback 刷快照）。"""
    await dbSession.rollback()
    result = await dbSession.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": _TABLE, "c": _COLUMN},
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_0098_claim_id_becomes_nullable(dbSession: AsyncSession) -> None:
    """upgrade head 后：evidence.claim_id 可空。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "upgrade", "head")
    assert await _claimIdNullable(dbSession) == "YES", (
        "upgrade head 后 evidence.claim_id 仍 NOT NULL —— 0098 迁移未生效"
    )


@pytest.mark.asyncio
async def test_0098_downgrade_restores_not_null_and_upgrade_reopens(
    dbSession: AsyncSession,
) -> None:
    """双向对称：downgrade 0096 恢复 NOT NULL（占位 UPDATE 兜 NULL 行），再 upgrade 复原。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "upgrade", "head")
    assert await _claimIdNullable(dbSession) == "YES", "起点不在 head：0098 未生效"

    try:
        _alembic(url, "downgrade", _PREVIOUS)
        version = (
            await dbSession.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
        assert version == _PREVIOUS, (
            f"downgrade 后 alembic_version = {version!r}，期望 {_PREVIOUS!r}"
        )
        assert await _claimIdNullable(dbSession) == "NO", (
            "downgrade 0098 之后 evidence.claim_id 未恢复 NOT NULL —— 降级没有对称反转"
        )
    finally:
        _alembic(url, "upgrade", "head")

    assert await _claimIdNullable(dbSession) == "YES", (
        "升级回来后 claim_id 没恢复可空 —— 本测试把测试库留在了坏状态"
    )
