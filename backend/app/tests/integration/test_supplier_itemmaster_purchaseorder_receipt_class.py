"""采购域 4 个本体类（Supplier/ItemMaster/PurchaseOrder/Receipt）的**有效绑定契约**。

## 这条断言在盯什么（2026-11-18 实测出来的真事故）

迁移 ``0039``/``0040`` 把这 4 个类的 ``source_table`` 绑到了 **DWD_* 词表**
（``DWD_SUPPLIER`` / ``DWD_MATERIAL`` / ``DWD_PURCHASE_ORDER`` /
``DWD_GOODS_RECEIPT``），而本体 SSOT（``backend/seed_ontology.py``）绑的是
**ERP 业务表**（``BPSUPPLIER`` / ``ITMMASTER`` / ``PORDER`` / ``PRECEIPT``）：

| class_name | 迁移 0039/0040 写的 | 规范值（ERP 词表） |
|---|---|---|
| Supplier | ``DWD_SUPPLIER`` | ``BPSUPPLIER`` |
| ItemMaster | ``DWD_MATERIAL`` | ``ITMMASTER`` |
| PurchaseOrder | ``DWD_PURCHASE_ORDER`` | ``PORDER`` |
| Receipt | ``DWD_GOODS_RECEIPT`` | ``PRECEIPT`` |

⇒ **两个种子在同一个 ``class_name`` 上写不同的 ``source_table``**，
谁先跑谁说了算：

- 测试侧 ``app/tests/_pg_support.seedHeaderOntologyClasses`` 写 ERP 词表；
- 迁移侧写 DWD 词表。

后果有三条（均已实测）：

1. NL2SQL 拼 SQL 时按 ``class.source_table`` 取物理表 ⇒ 指到一张**不存在的表**
   （本地栈只有 ``BPSUPPLIER`` / ``PORDER`` / ``PORDERQ`` 三张真实 ERP 表）；
2. ``seed_ontology.seed()`` 在干净库上**直接崩**（唯一约束
   ``uq_ontology_class_name_version`` = UNIQUE(class_name, version)，
   而它按 ``source_table`` 查重）；
3. 本用例的 verdict 取决于夹具顺序（迁移重放被 ``WHERE NOT EXISTS`` 挡掉 ⇒
   断言落在夹具写的值上 ⇒ 实测红）。

迁移 ``0153`` 已把绑定校准到 ERP 词表，两个种子就此不再分叉。

## 本文件断言的是「有效契约」，不是「迁移链落了这些值」

共享测试库每个用例前 TRUNCATE 全库 ⇒ 0039/0040/0153 的种子行**必被清掉**，
本文件自带的种子重放是唯一写者 ⇒ 它证明的是
「**两个种子不再分叉**」+「**重放幂等**」。

迁移链本身的真值（干净库跑完 ``alembic upgrade head`` 后逐类核对，
含 ``object_type`` / ``version``）由
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

# 规范绑定 = seed_ontology.CLASSES 的 source_table（本体 SSOT）。
# 迁移 0153 把 0039/0040 的 DWD_* 值校准到这里。
CANONICAL_SOURCE_TABLE = {
    "Supplier": "BPSUPPLIER",
    "ItemMaster": "ITMMASTER",
    "PurchaseOrder": "PORDER",
    "Receipt": "PRECEIPT",
}

# 与迁移 0153 校准后的有效状态对齐的种子重放（幂等守卫同迁移：
# WHERE NOT EXISTS + (class_name, version) 唯一约束）。
# 只写「绑定」不写「内容」（description / alias 归 seed_ontology 管）——
# 本文件不替内容层做断言。
_SEED_SQL = """
    INSERT INTO ontology_class (
            class_name, source_table, object_type,
            version, valid_from, created_by,
            created_time, updated_time
        )
    SELECT CAST(:class_name AS VARCHAR),
           CAST(:source_table AS VARCHAR),
           CAST(:object_type AS VARCHAR),
           1,
           now(),
           'seed-test',
           now(),
           now()
    WHERE NOT EXISTS (
        SELECT 1 FROM ontology_class WHERE class_name = CAST(:class_name AS VARCHAR)
    )
"""

_OBJECT_TYPE = {
    "Supplier": "Master",
    "ItemMaster": "Master",
    "PurchaseOrder": "Transaction",
    "Receipt": "Transaction",
}


def _seedParams() -> list[dict[str, object]]:
    return [
        {
            "class_name": name,
            "source_table": sourceTable,
            "object_type": _OBJECT_TYPE[name],
        }
        for name, sourceTable in CANONICAL_SOURCE_TABLE.items()
    ]


@pytest.fixture(autouse=True)
async def _canonicalSeeds(client,):
    """重放规范种子：依赖 client fixture（conftest 每测试 TRUNCATE 后重放），
    保证「规范绑定」在本文件里总是存在（不依赖别的夹具是否写过这批类）。"""
    engine = await _pg_support._newEngine()
    try:
        async with engine.begin() as conn:
            for params in _seedParams():
                await conn.execute(text(_SEED_SQL), params)
    finally:
        await engine.dispose()
    yield


@pytest.mark.asyncio
async def test_four_classes_bound_to_business_tables() -> None:
    """4 个类的 source_table 必须是 ERP 业务表词表（迁移 0153 校准后的状态）。"""
    engine = await _pg_support._newEngine()
    try:
        for className, sourceTable in CANONICAL_SOURCE_TABLE.items():
            row = await _fetch(engine, className)
            assert row is not None, f"{className} 行不存在"
            assert row[0] == className
            assert row[1] == sourceTable, (
                f"{className} 的 source_table 必须是 {sourceTable}（ERP 业务表词表），"
                f"实得 {row[1]!r}；若为 DWD_* 词表 ⇒ 迁移 0153 未应用，"
                "或某个种子又写回了旧词表（两个种子分叉）"
            )
            assert row[3] == 1, f"{className} version 应为 1"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_canonical_seed_replay_is_idempotent() -> None:
    """重复重放规范种子不产生重复行，也不改写已生效的绑定。"""
    engine = await _pg_support._newEngine()
    try:
        async with engine.begin() as conn:
            for params in _seedParams():
                await conn.execute(text(_SEED_SQL), params)
            for className, sourceTable in CANONICAL_SOURCE_TABLE.items():
                cnt = (
                    await conn.execute(
                        text("SELECT COUNT(*) FROM ontology_class WHERE class_name = :name"),
                        {"name": className},
                    )
                ).scalar()
                assert cnt == 1, f"{className} 期望 1 行，实得 {cnt}"
                row = await _fetch(engine, className)
                assert row is not None and row[1] == sourceTable, (
                    f"{className} 重放后绑定被改写：{row[1] if row else None!r}"
                )
    finally:
        await engine.dispose()


async def _fetch(engine, class_name: str):
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT class_name, source_table, object_type, version
                    FROM ontology_class WHERE class_name = :name
                    """
                ),
                {"name": class_name},
            )
        ).first()
    return row
