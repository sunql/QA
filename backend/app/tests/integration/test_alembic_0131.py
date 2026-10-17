"""验证 Alembic 0131：SessionQueryState NULL user_id 墓碑化（M4 数据迁移）。

契约（per aichat-p2-followups-2026-10-16/remaining-work-ssot §M4 方案 A）：
- upgrade 0131 后：所有 user_id IS NULL 的行被 UPDATE 为 -1（墓碑）
- 加列注释说明 -1 语义
- downgrade 0127 后：-1 行被还原为 NULL，列注释清空
- 双向对称：upgrade → downgrade → upgrade 状态一致

安全闸：与 test_alembic_0104 同款 —— alembic/env.py 只读 DATABASE_URL，
shell 出去之前强制校验库名，不匹配直接失败、不执行任何 DDL。
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
_REQUIRED_DB = ("qa_metadata_test", "qa_metadata_test_solo")

_REVISION = "0131"
_PREVIOUS = "0130"
_TABLE = "session_query_state"
_TOMBSTONE_USER_ID = -1

_ALEMBIC_TIMEOUT_SEC = 120


def _assertTestDb(url: str) -> None:
    """写库闸：只认测试库，否则 raise（不执行任何 DDL）。"""
    match = re.search(r"/([^/?]+)(?:\?|$)", url)
    dbName = match.group(1) if match else ""
    if dbName not in _REQUIRED_DB:
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


async def _seedNullRow(dbSession: AsyncSession, sessionId: str) -> None:
    """直接插一行 P0-3 前的 (NULL, NULL, session_id) 旧行（绕过 ORM）。"""
    await dbSession.rollback()
    await dbSession.execute(
        text(
            "INSERT INTO session_query_state (session_id, user_id, datasource_id, "
            "last_question, turn_count, created_time, updated_time) "
            "VALUES (:sid, NULL, NULL, :q, 1, now(), now())"
        ),
        {"sid": sessionId, "q": f"legacy-q-{sessionId}"},
    )
    await dbSession.commit()


async def _userIdBySessionId(dbSession: AsyncSession, sessionId: str) -> int | None:
    await dbSession.rollback()
    result = await dbSession.execute(
        text("SELECT user_id FROM session_query_state WHERE session_id = :sid"),
        {"sid": sessionId},
    )
    row = result.first()
    return row[0] if row else None


async def _columnComment(dbSession: AsyncSession) -> str | None:
    await dbSession.rollback()
    # 直接 JOIN pg_attribute 取 attnum，比 generate_series 简单且类型安全。
    result = await dbSession.execute(
        text(
            "SELECT col_description(c.oid, a.attnum) "
            "FROM pg_class c "
            "JOIN pg_attribute a ON a.attrelid = c.oid "
            "WHERE c.relname = :t AND a.attname = 'user_id' AND a.attnum > 0"
        ),
        {"t": _TABLE},
    )
    row = result.first()
    return row[0] if row else None


async def _deleteBySessionId(dbSession: AsyncSession, sessionId: str) -> None:
    await dbSession.rollback()
    await dbSession.execute(
        text("DELETE FROM session_query_state WHERE session_id = :sid"),
        {"sid": sessionId},
    )
    await dbSession.commit()


@pytest.mark.asyncio
async def test_0131_tombstones_null_user_id_rows(dbSession: AsyncSession) -> None:
    """upgrade 0131 后：NULL user_id 行被 UPDATE 为 -1（墓碑），列注释存在。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    # 起手：先回到 0127 baseline（保证本测试有 NULL 行可墓碑化）
    _alembic(url, "downgrade", _PREVIOUS)
    try:
        # 插一行 (NULL, NULL, session_id) 旧行
        sid = "m4-test-legacy-session"
        await _deleteBySessionId(dbSession, sid)
        await _seedNullRow(dbSession, sid)
        # 升级到 0131
        _alembic(url, "upgrade", _REVISION)
        # 验证：NULL → -1
        assert await _userIdBySessionId(dbSession, sid) == _TOMBSTONE_USER_ID, (
            f"upgrade 0131 后 user_id 应为 -1（墓碑），"
            f"实际 {await _userIdBySessionId(dbSession, sid)}"
        )
        # 验证：列注释含墓碑说明
        comment = await _columnComment(dbSession)
        assert comment is not None and "墓碑" in comment, (
            f"upgrade 0131 后列注释应含「墓碑」说明，实际 {comment!r}"
        )
    finally:
        # 清理：删测试行 + 送回 head 避免污染后续测试
        await _deleteBySessionId(dbSession, sid)
        _alembic(url, "upgrade", "head")


@pytest.mark.asyncio
async def test_0131_downgrade_restores_null_and_clears_comment(
    dbSession: AsyncSession,
) -> None:
    """downgrade 0127：-1 行还原为 NULL，列注释清空。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    # 起手：升到 0131，插一行（已是 -1），再回退
    _alembic(url, "upgrade", _REVISION)
    try:
        sid = "m4-test-downgrade-session"
        await _deleteBySessionId(dbSession, sid)
        # 升级到 0131 后再插：现在 user_id NOT NULL 是 False，可以插 NULL
        await _seedNullRow(dbSession, sid)
        # 但 seed 之后已经 -1（因为刚升的 0131 触发器式 UPDATE 不会重跑，所以
        # 这次新插的还是 NULL；先手动触发墓碑化用 SQL）
        await dbSession.rollback()
        await dbSession.execute(
            text(
                "UPDATE session_query_state SET user_id = :tombstone "
                "WHERE session_id = :sid"
            ),
            {"tombstone": _TOMBSTONE_USER_ID, "sid": sid},
        )
        await dbSession.commit()
        # 确认是 -1
        assert await _userIdBySessionId(dbSession, sid) == _TOMBSTONE_USER_ID
        # 回退 0127
        _alembic(url, "downgrade", _PREVIOUS)
        # 验证：-1 → NULL
        assert await _userIdBySessionId(dbSession, sid) is None, (
            f"downgrade 后 user_id 应为 NULL，实际 {await _userIdBySessionId(dbSession, sid)}"
        )
        # 验证：列注释清空
        comment = await _columnComment(dbSession)
        assert comment is None, f"downgrade 后列注释应清空，实际 {comment!r}"
    finally:
        # 清理 + 送回 head
        await _deleteBySessionId(dbSession, sid)
        _alembic(url, "upgrade", "head")


@pytest.mark.asyncio
async def test_0131_idempotent_upgrade(dbSession: AsyncSession) -> None:
    """二次 upgrade 0131：no-op（NULL 行已经在 -1 不会被再次更新）。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "upgrade", _REVISION)
    try:
        sid = "m4-test-idempotent-session"
        await _deleteBySessionId(dbSession, sid)
        # 升级后插行，user_id = -1（模拟迁移已完成）
        await dbSession.rollback()
        await dbSession.execute(
            text(
                "INSERT INTO session_query_state (session_id, user_id, datasource_id, "
                "last_question, turn_count, created_time, updated_time) "
                "VALUES (:sid, :tombstone, NULL, :q, 1, now(), now())"
            ),
            {"sid": sid, "tombstone": _TOMBSTONE_USER_ID, "q": f"q-{sid}"},
        )
        await dbSession.commit()
        # 再跑一次 upgrade 0131
        _alembic(url, "upgrade", _REVISION)
        # 验证：仍是 -1（没被改成 NULL 也没被改两次）
        assert await _userIdBySessionId(dbSession, sid) == _TOMBSTONE_USER_ID
    finally:
        await _deleteBySessionId(dbSession, sid)
        _alembic(url, "upgrade", "head")
