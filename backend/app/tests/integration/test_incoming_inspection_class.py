"""采购域 ``IncomingInspection``（来料检验）本体类的**有效绑定契约**。

## 规范值：``source_table`` 为 NULL（未绑定物理表）

``backend/seed_ontology.py`` 的 ``CLASSES`` 里**没有** ``IncomingInspection``
（来料检验尚无物理表）⇒ 规范绑定就是**不绑**（``NULL``）。
测试侧 ``app/tests/_pg_support.seedHeaderOntologyClasses`` 对它也是置 ``None``
⇒ 两个种子一致。

而迁移 ``0039`` 当年写的是 ``'DWD_INCOMING_INSPECTION'`` —— 与其余 4 个类同款问题：
那张表在系统的查询目标里不存在。迁移 ``0153`` 已把它一并校准为 ``NULL``
（详见 ``alembic/versions/0153_reconcile_procurement_class_source_tables.py``）。

## 本文件断言的是「有效契约」，不是「迁移链落了这些值」

共享测试库每个用例前 TRUNCATE 全库 ⇒ 0039/0153 的种子行**必被清掉**，
本文件自带的种子重放是唯一写者 ⇒ 它证明「两个种子不再分叉」+「重放幂等」。
迁移链真值（干净库跑完 ``alembic upgrade head`` 后逐类核对）由
``scripts/procurement_class_source_table_realdata.py`` 证明。

**库来源**：走 ``app.tests._pg_support._newEngine()``（``resolveTestDatabaseUrl()`` +
``_ensureSchema()``），**不硬编码 DSN**。硬编码会绕过 ``_ensureSchema()`` ⇒ 引擎连到
**schema 陈旧的旧库**（2026-11-18 实测：旧 5433 ``qa_metadata_test`` 停在 0139）⇒
本用例在缺列的表上跑，断言仍绿 ⇒ **假绿**。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.tests import _pg_support

# 规范种子重放：source_table 为 NULL（未绑定物理表）。
# 幂等守卫与迁移一致（WHERE NOT EXISTS + (class_name, version) 唯一约束）。
_SEED_SQL = """
    INSERT INTO ontology_class (
            class_name, source_table, object_type,
            version, valid_from, created_by,
            created_time, updated_time
        )
    SELECT 'IncomingInspection',
           NULL,
           'Transaction',
           1,
           now(),
           'seed-test',
           now(),
           now()
    WHERE NOT EXISTS (
        SELECT 1 FROM ontology_class WHERE class_name = 'IncomingInspection'
    )
"""


@pytest.fixture(autouse=True)
async def _canonicalSeeds(client,):
    """重放规范种子：依赖 client fixture（conftest 每测试 TRUNCATE 后重放），
    保证本文件不依赖别的夹具是否写过这个类。"""
    engine = await _pg_support._newEngine()
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_SEED_SQL))
    finally:
        await engine.dispose()
    yield


@pytest.mark.asyncio
async def test_incoming_inspection_has_no_business_table_binding() -> None:
    """来料检验未绑定物理表 ⇒ source_table 必须是 NULL。

    若实得 ``'DWD_INCOMING_INSPECTION'`` ⇒ 迁移 0153 未应用，
    或某个种子又写回了 DWD 词表（两个种子分叉）。
    """
    engine = await _pg_support._newEngine()
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT class_name, source_table, object_type, version
                        FROM ontology_class
                        WHERE class_name = 'IncomingInspection'
                        """
                    )
                )
            ).first()
        assert row is not None, "IncomingInspection 行不存在"
        assert row[0] == "IncomingInspection"
        assert row[1] is None, (
            "IncomingInspection 的 source_table 应为 NULL（来料检验无物理表），"
            f"实得 {row[1]!r}；若为 DWD_* 词表 ⇒ 迁移 0153 未应用，"
            "或某个种子又写回了旧词表（两个种子分叉）"
        )
        assert row[3] == 1, "version 应为 1"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_canonical_seed_replay_is_idempotent() -> None:
    """重复重放规范种子不产生重复行，也不改写已生效的绑定。"""
    engine = await _pg_support._newEngine()
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_SEED_SQL))
            rows = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM ontology_class WHERE class_name = 'IncomingInspection'")
                )
            ).scalar()
            assert rows == 1, f"IncomingInspection 期望 1 行，实得 {rows}"
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT class_name, source_table, object_type, version
                        FROM ontology_class
                        WHERE class_name = 'IncomingInspection'
                        """
                    )
                )
            ).first()
            assert row is not None and row[1] is None, (
                f"IncomingInspection 重放后绑定被改写：{row[1] if row else None!r}"
            )
    finally:
        await engine.dispose()
