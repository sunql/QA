"""验证 Alembic 0097：`id_mapping` 表落库（M0-P0.1）。

契约：
- upgrade head 后真实 PG 上存在 `id_mapping`，且列集合与 ORM 声明一致；
- 唯一约束 `(business_object, external_id)` 由 DB 真实兜底（不靠 service 自觉）。

**来历（2026-11-27 归档清理）**：这个契约最初写在 `0095_id_mapping` 迁移的配套
用例里（`test_alembic_0095.py`）。0095 → 0097 改号时，**用例没跟着改号，
结果两者都没进 main** ⇒ main 上 `id_mapping`（已合入的 M0-P0.1 特性）**一直没有
迁移契约测试**。本次归档清理把它落到 0097 上，补上这个缺口。

⚠️ 与原用例的一处**刻意差异**：原用例还有第三条「downgrade 0094 → 表被撤 →
upgrade head 复原」的双向对称用例，**这里没有保留**。原因：0097 在迁移链上距
head（0171）已有 **75 个版本**，downgrade 到 0096 会连带撤销 0098–0171 的全部
迁移（DROP 大量表 + 重建）⇒ 一旦中途失败就把整条 integration 分片的测试库留在
坏状态（原用例在 0095 紧邻 0094 时只需退 1 个版本，风险不可比）。
「升级方向」的契约由本文件的两个用例覆盖；「降级方向」属破坏性验证，不进分片。

为什么不抄 DDL 重放：抄写本身就可能抄错，「跑完库里剩什么」只有真跑 alembic 才知道。
代价是会短暂改动测试库 `alembic_version`，故降级-升级循环由 try/finally 兜底复原。

安全闸：``alembic/env.py`` 只读 ``DATABASE_URL``（``TEST_DATABASE_URL`` 被静默忽略），
传错就打到 **prod**。故 shell 出去之前强制用仓内 ``assertTestDatabase`` 校验
（库名须是 ``qa_metadata_test*`` 且主机必须是回环地址），不匹配直接失败、不执行任何 DDL。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests._pg_support import assertTestDatabase, resolveTestDatabaseUrl

_BACKEND_ROOT = Path(__file__).resolve().parents[3]  # backend/

_REVISION = "0097"
_TABLE = "id_mapping"

_ALEMBIC_TIMEOUT_SEC = 120

_EXPECTED_COLUMNS = {
    "unified_id",
    "business_object",
    "external_id",
    "pg_table",
    "pg_id",
    "neo4j_node_id",
    "milvus_collection",
    "milvus_id",
    "created_time",
    "updated_time",
}


def _alembic(url: str, *args: str) -> str:
    """在测试库上跑一条 alembic 子命令；先过写库闸。"""
    assertTestDatabase(url)
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


async def _idMappingColumns(dbSession: AsyncSession) -> set[str]:
    """读真实 PG：id_mapping 的实际列集合（同样要先 rollback）。"""
    await dbSession.rollback()

    def _sync_inspect(sync_session) -> set[str]:
        columns = inspect(sync_session.bind).get_columns(_TABLE)
        return {c["name"] for c in columns}

    return await dbSession.run_sync(_sync_inspect)


@pytest.mark.asyncio
async def test_0097_creates_id_mapping_table_with_expected_columns(
    dbSession: AsyncSession,
) -> None:
    """upgrade head 后 `id_mapping` 实际落库，且列集合与 ORM 声明一致。"""
    url = resolveTestDatabaseUrl()
    assertTestDatabase(url)

    # Arrange：升级到 head（含本迁移的最终状态）
    _alembic(url, "upgrade", "head")

    # Act
    tables = await _tableNames(dbSession)

    # Assert：先断表存在，再断列集合 —— 顺序反了会先抛 NoSuchTableError，
    # 失败信息从「迁移未生效」退化成一句 inspect 内部异常
    assert _TABLE in tables, f"upgrade head 后 {_TABLE!r} 表不存在 —— 0097 迁移未生效"
    columns = await _idMappingColumns(dbSession)
    assert columns == _EXPECTED_COLUMNS, (
        f"{_TABLE} 列集合与 ORM 声明不一致：多 {columns - _EXPECTED_COLUMNS}，"
        f"缺 {_EXPECTED_COLUMNS - columns}"
    )


@pytest.mark.asyncio
async def test_0097_unique_constraint_rejects_duplicate_business_key(
    dbSession: AsyncSession,
) -> None:
    """唯一约束 `(business_object, external_id)` 由 DB 真实兜底（不靠 service 自觉）。"""
    url = resolveTestDatabaseUrl()
    assertTestDatabase(url)
    _alembic(url, "upgrade", "head")

    insert = text(
        "INSERT INTO id_mapping "
        "(unified_id, business_object, external_id, created_time, updated_time) "
        "VALUES (:uid, :bo, :ext, now(), now())"
    )
    await dbSession.execute(
        insert, {"uid": "obj:Supplier:4", "bo": "Supplier", "ext": "4"}
    )
    await dbSession.commit()

    try:
        with pytest.raises(IntegrityError):
            # 同 (business_object, external_id)、不同 unified_id ⇒ 必撞唯一约束
            await dbSession.execute(
                insert, {"uid": "obj:Supplier:4-dup", "bo": "Supplier", "ext": "4"}
            )
            await dbSession.commit()
    finally:
        await dbSession.rollback()
        # cleanup：本用例直接写了表，避免污染同进程后续用例
        await dbSession.execute(
            text("DELETE FROM id_mapping WHERE business_object = 'Supplier'")
        )
        await dbSession.commit()
