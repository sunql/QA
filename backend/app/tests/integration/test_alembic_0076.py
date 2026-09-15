"""验证 Alembic 0076：`data_quality_rule.rule_params` JSONB 可空列（feat-dq-rule-params）。

契约：
- upgrade head 后 `data_quality_rule` 多出 `rule_params` 列，类型 JSONB、可空。
- downgrade 0075 后该列被撤掉，再 upgrade head 恢复 —— 双向对称，版本号如实反映结构
  （「版本号相同 ≠ 结构相同」是本项目「两库结构漂移」的病根，见 0062 测试 docstring）。
- 存量规则零迁移：列可空，旧行不写即为 NULL（legacy 自定义 SQL 模式）。

为什么真跑 alembic：「跑完库里剩什么」是 alembic 运行时的可观测结果，抄写 DDL 重放
证明不了（抄写本身就可能抄错）。代价是会短暂改动测试库 `alembic_version`，
故整个降级-升级循环由 try/finally 兜底复原。

安全闸：`alembic/env.py` 只读 `DATABASE_URL`（`TEST_DATABASE_URL` 被静默忽略），
传错就打到 **prod**。故 shell 出去之前强制校验库名，不匹配直接失败、不执行任何 DDL。
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

_BACKEND_ROOT = Path(__file__).resolve().parents[3]  # backend/
_REQUIRED_DB = "qa_metadata_test"

_HEAD = "0076_data_quality_rule_params"
_PREV = "0075_dq_sample_error"
_TABLE = "data_quality_rule"
_COLUMN = "rule_params"

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


async def _columnInfo(dbSession: AsyncSession) -> dict[str, Any] | None:
    """读真实 PG：data_quality_rule.rule_params 列的元数据（不存在则 None）。

    先 rollback 丢掉可能持有的旧快照，否则 alembic 在**另一个连接**上做的 DDL
    在本会话里看不见（READ COMMITTED 下 REPEATABLE 快照未刷新）。
    """
    await dbSession.rollback()
    row = (
        await dbSession.execute(
            text(
                "SELECT data_type, udt_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ),
            {"t": _TABLE, "c": _COLUMN},
        )
    ).mappings().first()
    return dict(row) if row else None


async def _versionNum(dbSession: AsyncSession) -> str:
    await dbSession.rollback()
    return (
        await dbSession.execute(text("SELECT version_num FROM alembic_version"))
    ).scalar_one()


@pytest.mark.asyncio
async def test_0076_adds_rule_params_jsonb_column(dbSession: AsyncSession) -> None:
    """upgrade head 后：rule_params 存在、JSONB、可空，且版本号如实落在 0076。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    # Arrange：确保起点是 head（幂等；迁移文件缺失时此处停在 0075）
    _alembic(url, "upgrade", "head")
    atHead = await _columnInfo(dbSession)
    version = await _versionNum(dbSession)

    # Assert
    assert version == _HEAD, (
        f"alembic_version = {version!r}，期望 {_HEAD!r} —— 迁移没有成为 head"
    )
    assert atHead is not None, (
        f"upgrade head 后 {_TABLE}.{_COLUMN} 列不存在 —— 迁移未生效"
    )
    assert atHead["udt_name"] == "jsonb", (
        f"rule_params 底层类型是 {atHead['udt_name']!r}，期望 'jsonb'"
    )
    assert "json" in atHead["data_type"].lower()
    assert atHead["is_nullable"] == "YES", (
        "rule_params 必须可空：存量规则零迁移，旧行不写即为 NULL（legacy 自定义 SQL 模式）"
    )


@pytest.mark.asyncio
async def test_0076_downgrade_drops_column_and_upgrade_restores(
    dbSession: AsyncSession,
) -> None:
    """双向对称：downgrade 0075 撤列、版本号回落；finally 里 upgrade head 复原。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "upgrade", "head")
    before = await _columnInfo(dbSession)
    assert before is not None, "起点不在 head：rule_params 列缺失"

    try:
        # Act
        _alembic(url, "downgrade", _PREV)
        after = await _columnInfo(dbSession)
        version = await _versionNum(dbSession)

        # Assert：列撤掉、版本号如实反映结构
        assert version == _PREV, (
            f"downgrade 后 alembic_version = {version!r}，期望 {_PREV!r}"
        )
        assert after is None, (
            "downgrade 0076 之后 rule_params 列仍在 —— 降级没有对称反转 upgrade"
        )
    finally:
        # 无论如何把库恢复到 head，避免把测试库留在 0075 影响后续测试
        _alembic(url, "upgrade", "head")

    restored = await _columnInfo(dbSession)
    restoredVersion = await _versionNum(dbSession)
    assert restoredVersion == _HEAD
    assert restored is not None, (
        "升级回来之后 rule_params 列没恢复 —— 本测试把测试库留在了坏状态"
    )
    assert restored["udt_name"] == "jsonb"
