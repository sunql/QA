"""验证 Alembic 0104：report_instance 表（v3.1 A8 / M4 Report 模板 MVP）。

契约（brief §实勘 5）：
- upgrade head 后真实 PG 上 report_instance 存在，核心列齐备
  （template_code NOT NULL / sections JSONB NOT NULL / status 默认
  PENDING_REVIEW / created_by NOT NULL）
- ix_report_instance_status + ix_report_instance_created_by 两个索引存在
- downgrade 0103 后表消失，再 upgrade head 复原 —— 双向对称

安全闸：与 test_alembic_0100 同款 —— alembic/env.py 只读 DATABASE_URL，
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

_REVISION = "0104"
_PREVIOUS = "0103"
_TABLE = "report_instance"

_ALEMBIC_TIMEOUT_SEC = 120

# brief §实勘 5 列清单（subset 校验，TimestampMixin 两列惯例免检）
_EXPECTED_COLUMNS = {
    "template_code": ("NO",),  # NOT NULL
    "title": ("NO",),
    "params": ("YES",),
    "sections": ("NO",),
    "summary": ("YES",),
    "status": ("NO",),
    "review_note": ("YES",),
    "reviewed_by": ("YES",),
    "created_by": ("NO",),
}
_EXPECTED_INDEXES = {
    "ix_report_instance_status",
    "ix_report_instance_created_by",
}


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


async def _tableExists(dbSession: AsyncSession) -> bool:
    await dbSession.rollback()
    result = await dbSession.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": _TABLE},
    )
    return result.first() is not None


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


async def _indexNames(dbSession: AsyncSession) -> set[str]:
    await dbSession.rollback()
    result = await dbSession.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = :t"
        ),
        {"t": _TABLE},
    )
    return {row[0] for row in result.fetchall()}


@pytest.mark.asyncio
async def test_0104_report_instance_columns_and_indexes(dbSession: AsyncSession) -> None:
    """upgrade head 后：列与索引按 brief §实勘 5 齐备。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "upgrade", "head")
    assert await _tableExists(dbSession), "upgrade head 后 report_instance 表缺失"
    for column, (nullable,) in _EXPECTED_COLUMNS.items():
        meta = await _columnMeta(dbSession, column)
        assert meta is not None, f"report_instance.{column} 列缺失"
        assert meta[1] == nullable, (
            f"report_instance.{column} 可空性期望 {nullable}，实际 {meta[1]}"
        )
    assert (await _columnMeta(dbSession, "sections"))[0] == "jsonb"
    assert (await _columnMeta(dbSession, "params"))[0] == "jsonb"
    indexes = await _indexNames(dbSession)
    assert _EXPECTED_INDEXES <= indexes, (
        f"索引缺失：{_EXPECTED_INDEXES - indexes}"
    )


@pytest.mark.asyncio
async def test_0104_downgrade_drops_table_and_upgrade_restores(
    dbSession: AsyncSession,
) -> None:
    """双向对称：downgrade 0103 后表消失，再 upgrade head 复原。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "upgrade", "head")
    assert await _tableExists(dbSession), "起点不在 head：0104 未生效"

    try:
        await dbSession.rollback()  # 释放测试连接持有的表锁，避免 ALTER 等锁超时
        _alembic(url, "downgrade", _PREVIOUS)
        version = (
            await dbSession.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
        assert version == _PREVIOUS, (
            f"downgrade 后 alembic_version = {version!r}，期望 {_PREVIOUS!r}"
        )
        assert not await _tableExists(dbSession), "downgrade 后 report_instance 仍存在"

        _alembic(url, "upgrade", "head")
        assert await _tableExists(dbSession), "再 upgrade 后 report_instance 未复原"
    finally:
        # 无论断言成败，把测试库送回 head，避免污染后续测试
        await dbSession.rollback()
        _alembic(url, "upgrade", "head")
