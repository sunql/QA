"""验证 Alembic 0132：kpi_catalog.datasource_id FK 改 ON DELETE RESTRICT（F2 §4.5）。

契约：
- upgrade 0132 后：``fk_kpi_catalog_datasource.confdeltype == 'r'``（RESTRICT）
- downgrade 0131 后：``confdeltype == 'n'``（SET NULL，旧语义）
- 链路 0131 → 0132 无断点

安全闸：与 test_alembic_0131 同款 —— alembic/env.py 只读 DATABASE_URL，
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

_REVISION = "0132"
_PREVIOUS = "0131"
_TABLE = "kpi_catalog"
_CONSTRAINT = "fk_kpi_catalog_datasource"

_RESTRICT = "r"
_SET_NULL = "n"

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


async def _confdeltype(dbSession: AsyncSession) -> str:
    """读 FK 的 ON DELETE 动作（r=RESTRICT, n=SET NULL, c=CASCADE...）。"""
    await dbSession.rollback()
    row = (
        await dbSession.execute(
            text(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conname = :n AND conrelid = 'kpi_catalog'::regclass"
            ),
            {"n": _CONSTRAINT},
        )
    ).first()
    assert row is not None, f"约束 {_CONSTRAINT} 不存在于 {_TABLE}"
    value = row[0]
    # pg_constraint.confdeltype 是 "char"；asyncpg 返回 bytes（b'r'）
    return value.decode() if isinstance(value, (bytes, bytearray)) else value


@pytest.mark.asyncio
async def test_0132_upgrade_sets_restrict(dbSession: AsyncSession) -> None:
    """downgrade 0131 → upgrade 0132 → FK ON DELETE = RESTRICT。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "downgrade", _PREVIOUS)
    try:
        assert await _confdeltype(dbSession) == _SET_NULL, "0131 baseline 应为 SET NULL"
        _alembic(url, "upgrade", _REVISION)
        assert await _confdeltype(dbSession) == _RESTRICT, (
            "upgrade 0132 后 FK 应为 RESTRICT"
        )
    finally:
        _alembic(url, "upgrade", "head")


@pytest.mark.asyncio
async def test_0132_downgrade_restores_set_null(dbSession: AsyncSession) -> None:
    """upgrade 0132 → downgrade 0131 → FK ON DELETE 恢复 SET NULL。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "upgrade", _REVISION)
    try:
        assert await _confdeltype(dbSession) == _RESTRICT
        _alembic(url, "downgrade", _PREVIOUS)
        assert await _confdeltype(dbSession) == _SET_NULL, (
            "downgrade 0131 后 FK 应恢复 SET NULL"
        )
    finally:
        _alembic(url, "upgrade", "head")


@pytest.mark.asyncio
async def test_0132_chain_is_linear(dbSession: AsyncSession) -> None:
    """0131 → 0132 链路可走通（alembic 返回码 0 由 _alembic 断言）。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "downgrade", _PREVIOUS)
    try:
        _alembic(url, "upgrade", _REVISION)
        assert await _confdeltype(dbSession) == _RESTRICT
    finally:
        _alembic(url, "upgrade", "head")
