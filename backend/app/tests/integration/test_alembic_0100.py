"""验证 Alembic 0100：knowledge_claim 补 confidence_level（v3.1 任务 B4）。

契约（v3.1 蓝图 §12.2 离散 4 级置信度）：
- upgrade head 后真实 PG 上 knowledge_claim.confidence_level 存在且可空
  （VARCHAR(10)，存量行不回填）
- **原始 confidence Numeric(5,4) 列原样保留**（原始分保留供未来校准）
- downgrade 0099 后列消失
- 再 upgrade head 复原 —— 双向对称

安全闸：与 test_alembic_0099 同款 —— alembic/env.py 只读 DATABASE_URL，
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
_REQUIRED_DB = "qa_metadata_test"

_REVISION = "0100"
_PREVIOUS = "0099"
_TABLE = "knowledge_claim"
_COLUMN = "confidence_level"

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


async def _columnMeta(dbSession: AsyncSession, column: str) -> tuple[str, str] | None:
    """读真实 PG information_schema：指定列的 (data_type, is_nullable)。"""
    await dbSession.rollback()
    result = await dbSession.execute(
        text(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": _TABLE, "c": column},
    )
    row = result.first()
    return (row[0], row[1]) if row else None


@pytest.mark.asyncio
async def test_0100_confidence_level_added_nullable(dbSession: AsyncSession) -> None:
    """upgrade head 后：confidence_level 存在且可空（VARCHAR(10)）。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "upgrade", "head")
    meta = await _columnMeta(dbSession, _COLUMN)
    assert meta is not None, "upgrade head 后 knowledge_claim.confidence_level 列缺失"
    dataType, isNullable = meta
    assert isNullable == "YES", "confidence_level 必须可空（存量行不回填）"
    assert dataType in ("character varying", "VARCHAR"), f"期望 VARCHAR，实际 {dataType}"


@pytest.mark.asyncio
async def test_0100_original_confidence_numeric_preserved(dbSession: AsyncSession) -> None:
    """原始 confidence Numeric(5,4) 列原样保留（不删列、不改型）。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "upgrade", "head")
    meta = await _columnMeta(dbSession, "confidence")
    assert meta is not None, "原始 confidence 列被误删"
    dataType, isNullable = meta
    assert dataType == "numeric", f"confidence 类型被改动：{dataType}"
    assert isNullable == "YES"


@pytest.mark.asyncio
async def test_0100_downgrade_drops_column_and_upgrade_restores(
    dbSession: AsyncSession,
) -> None:
    """双向对称：downgrade 0099 后列消失，再 upgrade head 复原。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "upgrade", "head")
    assert await _columnMeta(dbSession, _COLUMN) is not None, "起点不在 head：0100 未生效"

    try:
        await dbSession.rollback()  # 释放测试连接持有的表锁，避免 ALTER 等锁超时
        _alembic(url, "downgrade", _PREVIOUS)
        version = (
            await dbSession.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
        assert version == _PREVIOUS, (
            f"downgrade 后 alembic_version = {version!r}，期望 {_PREVIOUS!r}"
        )
        assert await _columnMeta(dbSession, _COLUMN) is None, "downgrade 后 confidence_level 未删除"
    finally:
        await dbSession.rollback()
        _alembic(url, "upgrade", "head")

    await dbSession.rollback()
    assert await _columnMeta(dbSession, _COLUMN) is not None, (
        "升级回来后 confidence_level 没恢复 —— 本测试把测试库留在了坏状态"
    )


@pytest.mark.asyncio
async def test_0100_existing_rows_keep_working(dbSession: AsyncSession) -> None:
    """存量行不动：升级后旧 claim 行 confidence_level 为 NULL，仍可正常读写。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "upgrade", "head")
    await dbSession.rollback()
    # 播种一条不带 confidence_level 的 claim（wiki_page FK 需要先种 page）
    await dbSession.execute(
        text(
            "INSERT INTO wiki_page (page_id, title, content) "
            "VALUES ('pg-0100-test', 't', 'c') "
            "ON CONFLICT (page_id) DO NOTHING"
        )
    )
    await dbSession.execute(
        text(
            "INSERT INTO knowledge_claim (page_id, claim_text) "
            "VALUES ('pg-0100-test', '存量断言')"
        )
    )
    await dbSession.commit()

    value = (
        await dbSession.execute(
            text(
                "SELECT confidence_level FROM knowledge_claim "
                "WHERE claim_text = '存量断言'"
            )
        )
    ).scalar_one()
    assert value is None, "存量行 confidence_level 必须为 NULL（不回填）"
