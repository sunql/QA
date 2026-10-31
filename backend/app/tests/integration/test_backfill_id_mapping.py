"""id_mapping 回填脚本集成测试（M0-P0.2 · A1 ③）。

契约：
- 存活本体类全部拿到统一 ID（`business_object="CLASS"`），count 与
  `valid_to IS NULL` 的行数对平。
- 二次执行幂等（PG conflict 路径 DO UPDATE，不新增行、不报错）。
- 软删墓碑（valid_to 非空）默认跳过，`includeDeleted=True` 时才纳入。
- 预演（`previewBackfill`）只读：不写任何行。
- 坏行（`unified_id` 与两段不一致）在**写入之前**报可执行的错。

为什么直接插 ontology_class 而不走 ontology API：本用例要钉的是「回填脚本对
给定行集做了什么」，class_id 必须由测试指定（否则断言不出 `obj:CLASS:<id>`
里的 id）。ontology_class.id 是 `nextval(...)` 默认值而非 GENERATED ALWAYS，
显式给 id 合法。

断言一律 raw SQL：ORM identity map 在跨 commit 后可能返回旧值（历史教训
见 seed_business_objects 的 header_class_id 回填事故）。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from scripts import backfill_id_mapping as backfillModule
from scripts.backfill_id_mapping import (
    CORRUPT_SAMPLE_LIMIT,
    CaseVariantMappingError,
    CorruptMappingError,
    backfillIdMappings,
    findCorruptMappings,
    previewBackfill,
)

pytestmark = pytest.mark.asyncio

_INSERT_CLASS_SQL = text(
    "INSERT INTO ontology_class "
    "(id, class_name, version, valid_from, valid_to, created_time, updated_time) "
    "VALUES (:id, :name, 1, now(), :validTo, now(), now())"
)


@pytest.fixture(autouse=True)
async def _cleanTables(db_session: AsyncSession) -> None:
    await db_session.execute(
        text("TRUNCATE TABLE id_mapping, ontology_class RESTART IDENTITY CASCADE")
    )
    await db_session.commit()


async def _insertClass(
    session: AsyncSession, classId: int, className: str, *, deleted: bool = False
) -> None:
    """直接插 ontology_class：class_id 显式给，便于断言 unified_id = obj:CLASS:<id>。"""
    await session.execute(
        _INSERT_CLASS_SQL,
        {
            "id": classId,
            "name": className,
            "validTo": datetime.now(UTC) if deleted else None,
        },
    )
    await session.commit()


async def _countMappings(session: AsyncSession) -> int:
    return int(
        (await session.execute(text("SELECT count(*) FROM id_mapping"))).scalar_one()
    )


class TestBackfill:
    async def test_registers_all_alive_classes(self, db_session: AsyncSession) -> None:
        # Arrange：3 个存活类
        await _insertClass(db_session, 101, "Supplier")
        await _insertClass(db_session, 102, "Customer")
        await _insertClass(db_session, 103, "ItemMaster")

        # Act
        summary = await backfillIdMappings(db_session)

        # Assert
        assert summary.aliveClasses == 3
        assert summary.scannedClasses == 3
        assert await _countMappings(db_session) == 3
        rows = (
            await db_session.execute(
                text(
                    "SELECT unified_id, business_object, external_id, pg_table, pg_id "
                    "FROM id_mapping ORDER BY external_id"
                )
            )
        ).all()
        assert [tuple(r) for r in rows] == [
            ("obj:CLASS:101", "CLASS", "101", "ontology_class", "101"),
            ("obj:CLASS:102", "CLASS", "102", "ontology_class", "102"),
            ("obj:CLASS:103", "CLASS", "103", "ontology_class", "103"),
        ]

    async def test_skips_soft_deleted_by_default(self, db_session: AsyncSession) -> None:
        await _insertClass(db_session, 201, "Supplier")
        await _insertClass(db_session, 202, "SupplierOld", deleted=True)

        summary = await backfillIdMappings(db_session)

        assert summary.aliveClasses == 1
        assert summary.scannedClasses == 1
        assert await _countMappings(db_session) == 1

    async def test_include_deleted_registers_tombstones(
        self, db_session: AsyncSession
    ) -> None:
        await _insertClass(db_session, 301, "Supplier")
        await _insertClass(db_session, 302, "SupplierOld", deleted=True)

        summary = await backfillIdMappings(db_session, includeDeleted=True)

        assert summary.aliveClasses == 1
        assert summary.scannedClasses == 2
        assert await _countMappings(db_session) == 2

    async def test_rerun_is_idempotent(self, db_session: AsyncSession) -> None:
        """重跑不得新增行，也不得报错（PG conflict 路径走 DO UPDATE）。"""
        await _insertClass(db_session, 401, "Supplier")
        await _insertClass(db_session, 402, "Customer")

        first = await backfillIdMappings(db_session)
        second = await backfillIdMappings(db_session)

        assert first.aliveClasses == 2
        assert second.aliveClasses == 2
        assert await _countMappings(db_session) == 2

    async def test_rerun_does_not_clobber_cross_store_columns(
        self, db_session: AsyncSession
    ) -> None:
        """A2/A3 回填的 neo4j/milvus 列不能被重跑抹掉 —— 回填只写它自己拥有的列。"""
        await _insertClass(db_session, 501, "Supplier")
        await backfillIdMappings(db_session)
        await db_session.execute(
            text(
                "UPDATE id_mapping SET neo4j_node_id = 'node-501', "
                "milvus_collection = 'ontology_embeddings', milvus_id = 'vec-501' "
                "WHERE unified_id = 'obj:CLASS:501'"
            )
        )
        await db_session.commit()

        await backfillIdMappings(db_session)

        raw = (
            await db_session.execute(
                text(
                    "SELECT neo4j_node_id, milvus_collection, milvus_id "
                    "FROM id_mapping WHERE unified_id = 'obj:CLASS:501'"
                )
            )
        ).one()
        assert tuple(raw) == ("node-501", "ontology_embeddings", "vec-501")

    async def test_empty_ontology_registers_nothing(
        self, db_session: AsyncSession
    ) -> None:
        summary = await backfillIdMappings(db_session)
        assert summary.aliveClasses == 0
        assert summary.scannedClasses == 0
        assert await _countMappings(db_session) == 0

    async def test_preview_writes_nothing(self, db_session: AsyncSession) -> None:
        """预演只读：报出计划行数，但一行都不写。"""
        await _insertClass(db_session, 801, "Supplier")
        await _insertClass(db_session, 802, "Customer")

        summary = await previewBackfill(db_session)

        assert summary.aliveClasses == 2
        assert summary.scannedClasses == 2
        assert summary.plannedRows == 2
        assert await _countMappings(db_session) == 0

    async def test_corrupt_unified_id_fails_loudly_not_with_integrity_error(
        self, db_session: AsyncSession
    ) -> None:
        """`unified_id` 与自身两段不一致时，回填必须给出可执行的报错。

        回填按 `unified_id` 做 conflict target，但表上还有第二条唯一约束
        `uq_id_mapping_bo_ext (business_object, external_id)`。一行若
        `unified_id` 与自身两段对不上（手工 SQL 改过），重算出来的 ID 会撞上
        第二条约束 —— 而 `on_conflict_do_update` 只能指定一个 conflict target，
        于是抛 **IntegrityError**，且抛在批次中途（前几批已 commit）。

        这个报错对运维不可执行：它不说「哪一行坏了」「该改什么」。所以回填要在
        动手**之前**体检并点名，而不是撞到约束才炸。
        """
        await _insertClass(db_session, 601, "Supplier")
        await db_session.execute(
            text(
                "INSERT INTO id_mapping "
                "(unified_id, business_object, external_id, created_time, updated_time) "
                "VALUES ('obj:WrongName:601', 'CLASS', '601', now(), now())"
            )
        )
        await db_session.commit()

        with pytest.raises(CorruptMappingError) as excInfo:
            await backfillIdMappings(db_session)

        # 报错必须点名坏行，否则运维还得自己去翻表
        assert "obj:WrongName:601" in str(excInfo.value)
        assert "obj:CLASS:601" in str(excInfo.value)

    async def test_corrupt_table_blocks_preview_too(self, db_session: AsyncSession) -> None:
        """预演也要体检：不能先给出「将写入 N 行」的结论再在 apply 时炸。"""
        await _insertClass(db_session, 611, "Supplier")
        await db_session.execute(
            text(
                "INSERT INTO id_mapping "
                "(unified_id, business_object, external_id, created_time, updated_time) "
                "VALUES ('obj:WrongName:611', 'CLASS', '611', now(), now())"
            )
        )
        await db_session.commit()

        with pytest.raises(CorruptMappingError):
            await previewBackfill(db_session)

    async def test_healthy_table_is_not_flagged_as_corrupt(
        self, db_session: AsyncSession
    ) -> None:
        """体检不得误报：正确行的 `unified_id` 恰等于两段派生值，必须判健康。

        误报的代价很大 —— 它会让回填对任何非空表**彻底锁死**（每次重跑都抛
        `CorruptMappingError`），而幂等重跑正是本脚本的核心承诺。
        """
        await _insertClass(db_session, 701, "Supplier")
        await _insertClass(db_session, 702, "Customer")
        await backfillIdMappings(db_session)

        total, samples = await findCorruptMappings(db_session)

        assert total == 0
        assert samples == []

    async def test_corrupt_count_reports_total_not_sample_size(
        self, db_session: AsyncSession
    ) -> None:
        """坏行多于取样上限时，报出的必须是**总数**，不是样例条数。"""
        corruptCount = 7  # > CORRUPT_SAMPLE_LIMIT (5)
        for offset in range(corruptCount):
            await db_session.execute(
                text(
                    "INSERT INTO id_mapping "
                    "(unified_id, business_object, external_id, created_time, "
                    "updated_time) "
                    "VALUES (:uid, 'CLASS', :ext, now(), now())"
                ),
                {"uid": f"obj:CLASS:{800 + offset}", "ext": str(900 + offset)},
            )
        await db_session.commit()

        total, samples = await findCorruptMappings(db_session)

        assert total == corruptCount
        assert len(samples) == CORRUPT_SAMPLE_LIMIT

        # 报错文案里出现的也必须是总数
        with pytest.raises(CorruptMappingError) as excInfo:
            await backfillIdMappings(db_session)
        assert f"有 {corruptCount} 行" in str(excInfo.value)
        assert "另有 2 行未列出" in str(excInfo.value)


class TestCaseVariants:
    """`class` vs `CLASS`：同一命名空间两种大小写 ⇒ 同一类两个统一 ID。"""

    async def test_flags_lowercase_business_object_before_write(
        self, db_session: AsyncSession
    ) -> None:
        await _insertClass(db_session, 101, "Supplier")
        await db_session.execute(
            text(
                "INSERT INTO id_mapping "
                "(unified_id, business_object, external_id, milvus_collection, "
                "created_time, updated_time) "
                "VALUES ('obj:class:101', 'class', '101', 'ontology_embeddings', "
                "now(), now())"
            )
        )
        await db_session.commit()

        with pytest.raises(CaseVariantMappingError) as excInfo:
            await backfillIdMappings(db_session)

        message = str(excInfo.value)
        assert "obj:class:101" in message
        assert "CLASS" in message
        # 文案要指出来源，否则运维不知道该找谁的小写占位行
        assert "reconcile_milvus_id_mapping" in message
        # 体检在写入之前：只留下预置的那一行
        assert await _countMappings(db_session) == 1

    async def test_other_business_objects_are_not_flagged(
        self, db_session: AsyncSession
    ) -> None:
        """PROPERTY / METRIC 是**别的**命名空间，不是大小写漂移，不得误报。"""
        await _insertClass(db_session, 101, "Supplier")
        await db_session.execute(
            text(
                "INSERT INTO id_mapping "
                "(unified_id, business_object, external_id, created_time, updated_time) "
                "VALUES ('obj:PROPERTY:7', 'PROPERTY', '7', now(), now())"
            )
        )
        await db_session.commit()

        summary = await backfillIdMappings(db_session)

        assert summary.scannedClasses == 1
        assert await _countMappings(db_session) == 2


class TestTransactionBoundary:
    """单事务：中途失败不得留下前几批已提交的半状态。"""

    async def test_failure_mid_run_commits_nothing(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _insertClass(db_session, 101, "Supplier")
        await _insertClass(db_session, 102, "Customer")
        monkeypatch.setattr(backfillModule, "_BATCH_SIZE", 1)

        realUpsert = backfillModule._upsertBatch
        calls = {"count": 0}

        async def flakyUpsert(session: AsyncSession, rows: list) -> int:
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("模拟第二批次失败")
            return await realUpsert(session, rows)

        monkeypatch.setattr(backfillModule, "_upsertBatch", flakyUpsert)

        with pytest.raises(RuntimeError):
            await backfillIdMappings(db_session)

        # 回滚后仍是 0 行 ⇒ 第一批没有 commit
        await db_session.rollback()
        assert await _countMappings(db_session) == 0

    async def test_commit_false_leaves_transaction_to_caller(
        self, db_session: AsyncSession
    ) -> None:
        """门禁的探针靠这条：哨兵与回填同属一个未提交事务。"""
        await _insertClass(db_session, 101, "Supplier")

        await backfillIdMappings(db_session, commit=False)
        assert await _countMappings(db_session) == 1  # 同一事务内可见

        await db_session.rollback()
        assert await _countMappings(db_session) == 0  # 从未落盘


class TestCorruptReporting:
    async def test_illegal_segments_are_reported_not_rethrown(self) -> None:
        """坏行本身可能含非法分段：报错器不能再抛一次 ValidationError。"""
        message = backfillModule._describeCorrupt(1, [("garbage", "CLASS", "A:B")])

        assert "garbage" in message
        assert "无法派生" in message

    async def test_main_reports_database_errors_as_fail(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """数据库异常要落成可读 FAIL，而不是 traceback（上线窗口里最怕这个）。"""
        from sqlalchemy.exc import OperationalError
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.infrastructure import database as dbModule

        async def _raiseOperationalError(*_args: object, **_kwargs: object) -> None:
            raise OperationalError("SELECT 1", {}, Exception("connection reset"))

        from app.tests._pg_support import _newEngine

        engine = await _newEngine()
        try:
            factory = async_sessionmaker(
                engine, expire_on_commit=False, autoflush=False
            )
            monkeypatch.setattr(dbModule, "getSessionFactory", lambda: factory)
            monkeypatch.setattr(
                backfillModule, "previewBackfill", _raiseOperationalError
            )

            assert await backfillModule.main([]) == 1
        finally:
            await engine.dispose()
        assert "FAIL" in capsys.readouterr().err
