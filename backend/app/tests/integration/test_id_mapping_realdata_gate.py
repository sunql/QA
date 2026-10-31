"""A1 真实数据门禁（`scripts/id_mapping_realdata.py`）集成测试。

门禁的价值在于「断言的是语义，不是写完了」：
- 覆盖/悬空/派生三项只读断言能抓出漏类、换集、坏锚；
- 真实库的口径不是「总行数 == 存活类数」（PROPERTY/METRIC 行与墓碑都在表里）；
- 全量模式的重跑存活探针**必须命中回填真的会写的行**，且哨兵不落盘。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from scripts import backfill_id_mapping as backfillModule
from scripts.backfill_id_mapping import backfillIdMappings
from scripts.id_mapping_realdata import VerifyFailure, verifyFull, verifyReadOnly

pytestmark = pytest.mark.asyncio

_INSERT_CLASS_SQL = text(
    "INSERT INTO ontology_class "
    "(id, class_name, version, valid_from, valid_to, created_time, updated_time) "
    "VALUES (:id, :name, 1, now(), :validTo, now(), now())"
)
_INSERT_MAPPING_SQL = text(
    "INSERT INTO id_mapping "
    "(unified_id, business_object, external_id, created_time, updated_time) "
    "VALUES (:uid, :bo, :ext, now(), now())"
)


@pytest.fixture(autouse=True)
async def _cleanTables(db_session: AsyncSession) -> None:
    await db_session.execute(
        text("TRUNCATE TABLE id_mapping, ontology_class RESTART IDENTITY CASCADE")
    )
    await db_session.commit()


async def _insertClass(
    session: AsyncSession, classId: int, name: str, *, deleted: bool = False
) -> None:
    await session.execute(
        _INSERT_CLASS_SQL,
        {
            "id": classId,
            "name": name,
            "validTo": datetime.now(UTC) if deleted else None,
        },
    )
    await session.commit()


async def _insertMapping(
    session: AsyncSession, uid: str, businessObject: str, externalId: str
) -> None:
    await session.execute(
        _INSERT_MAPPING_SQL,
        {"uid": uid, "bo": businessObject, "ext": externalId},
    )
    await session.commit()


async def _seedBackfilled(session: AsyncSession) -> None:
    await _insertClass(session, 101, "Supplier")
    await _insertClass(session, 102, "Customer")
    await backfillIdMappings(session)


async def test_readonly_passes_on_consistent_table(db_session: AsyncSession) -> None:
    await _seedBackfilled(db_session)

    report = await verifyReadOnly(db_session)

    assert report == {"alive_classes": 2, "class_mappings": 2, "derived_ok": 2}


async def test_readonly_ignores_other_business_objects_and_tombstones(
    db_session: AsyncSession,
) -> None:
    """真实库口径：`id_mapping` 还有 PROPERTY/METRIC 行，软删类也留着映射行。

    只读门禁必须对这张「混装表」判定通过 —— 否则上线第 1 步就恒红
    （历史实现用「总行数 == 存活类数」，生产上永远不成立）。
    """
    await _seedBackfilled(db_session)
    await _insertClass(db_session, 90, "SupplierOld", deleted=True)
    await _insertMapping(db_session, "obj:CLASS:90", "CLASS", "90")
    # 第二个墓碑**没有**映射行：覆盖断言若忘了 `valid_to IS NULL`，这里就会报「漏类」
    await _insertClass(db_session, 91, "CustomerOld", deleted=True)
    await _insertMapping(db_session, "obj:PROPERTY:7", "PROPERTY", "7")
    await _insertMapping(db_session, "obj:METRIC:8", "METRIC", "8")

    report = await verifyReadOnly(db_session)

    assert report["alive_classes"] == 2
    assert report["class_mappings"] == 3  # 2 个存活 + 1 个墓碑
    assert report["derived_ok"] == 5


async def test_readonly_flags_missing_alive_class(db_session: AsyncSession) -> None:
    """漏类：存活类没有映射必须报。"""
    await _seedBackfilled(db_session)
    await db_session.execute(
        text("DELETE FROM id_mapping WHERE unified_id = 'obj:CLASS:102'")
    )
    await db_session.commit()

    with pytest.raises(VerifyFailure) as excInfo:
        await verifyReadOnly(db_session)
    assert "漏类" in str(excInfo.value)
    assert "102:Customer" in str(excInfo.value)


async def test_readonly_flags_swapped_set_not_just_count(
    db_session: AsyncSession,
) -> None:
    """计数相等也不放过「漏 102、多 999」——按集合覆盖断言，不是按行数。"""
    await _seedBackfilled(db_session)
    await db_session.execute(
        text(
            "UPDATE id_mapping SET unified_id = 'obj:CLASS:999', external_id = '999' "
            "WHERE unified_id = 'obj:CLASS:102'"
        )
    )
    await db_session.commit()

    with pytest.raises(VerifyFailure) as excInfo:
        await verifyReadOnly(db_session)
    message = str(excInfo.value)
    assert "漏类" in message  # 102 没映射
    assert "102" in message


async def test_readonly_flags_dangling_mapping(db_session: AsyncSession) -> None:
    """悬空：映射指向不存在的本体类。"""
    await _seedBackfilled(db_session)
    await _insertMapping(db_session, "obj:CLASS:999", "CLASS", "999")

    with pytest.raises(VerifyFailure) as excInfo:
        await verifyReadOnly(db_session)
    assert "悬空映射" in str(excInfo.value)
    assert "obj:CLASS:999" in str(excInfo.value)


async def test_readonly_flags_derived_mismatch(db_session: AsyncSession) -> None:
    """unified_id 与两段不一致：三库唯一锚被破坏，必须报。"""
    await _seedBackfilled(db_session)
    await db_session.execute(
        text(
            "UPDATE id_mapping SET unified_id = 'obj:CLASS:777' "
            "WHERE unified_id = 'obj:CLASS:101'"
        )
    )
    await db_session.commit()

    with pytest.raises(VerifyFailure) as excInfo:
        await verifyReadOnly(db_session)
    assert "派生不一致" in str(excInfo.value)


async def test_readonly_reports_illegal_segments_as_failure_not_crash(
    db_session: AsyncSession,
) -> None:
    """老数据含非法分段时，门禁给 FAIL，而不是自己的校验异常冒成 traceback。"""
    await _seedBackfilled(db_session)
    await _insertMapping(db_session, "obj:CLASS:A:B", "CLASS", "A:B")

    with pytest.raises(VerifyFailure) as excInfo:
        await verifyReadOnly(db_session)
    assert "分段不合法" in str(excInfo.value)


async def test_full_mode_preserves_cross_store_columns(db_session: AsyncSession) -> None:
    """全量模式会打哨兵再重跑回填；跑完哨兵必须还原成原值（且从不落盘）。"""
    await _seedBackfilled(db_session)
    await db_session.execute(
        text(
            "UPDATE id_mapping SET neo4j_node_id = 'node-101', "
            "milvus_collection = 'ontology_embeddings', milvus_id = 'vec-101' "
            "WHERE unified_id = 'obj:CLASS:101'"
        )
    )
    await db_session.commit()

    report = await verifyFull(db_session)

    assert report["alive_classes"] == 2
    raw = (
        await db_session.execute(
            text(
                "SELECT neo4j_node_id, milvus_collection, milvus_id "
                "FROM id_mapping WHERE unified_id = 'obj:CLASS:101'"
            )
        )
    ).one()
    assert tuple(raw) == ("node-101", "ontology_embeddings", "vec-101")


async def test_probe_failure_propagates_and_persists_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探针中途炸掉时：原异常不能被回滚遮蔽，哨兵也不能落盘。

    这正是旧实现（先 commit 哨兵、失败时再 restore）会留下假坐标的那条路径。
    """
    from scripts import id_mapping_realdata as gateModule

    await _seedBackfilled(db_session)
    await db_session.execute(
        text(
            "UPDATE id_mapping SET neo4j_node_id = 'node-101', "
            "milvus_collection = 'ontology_embeddings', milvus_id = 'vec-101' "
            "WHERE unified_id = 'obj:CLASS:101'"
        )
    )
    await db_session.commit()

    class Boom(RuntimeError):
        pass

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise Boom("backfill exploded")

    monkeypatch.setattr(gateModule, "backfillIdMappings", _boom)

    with pytest.raises(Boom):
        await gateModule._checkRerunSurvives(db_session)

    from app.tests._pg_support import _newEngine

    engine = await _newEngine()
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT neo4j_node_id, milvus_collection, milvus_id "
                        "FROM id_mapping WHERE unified_id = 'obj:CLASS:101'"
                    )
                )
            ).one()
    finally:
        await engine.dispose()
    assert tuple(row) == ("node-101", "ontology_embeddings", "vec-101")


async def test_readonly_flags_case_variant_drift(db_session: AsyncSession) -> None:
    """只读门禁是上线第 1 步：大小写漂移必须在这里 FAIL。

    否则 canonical `CLASS` 行满足覆盖、小写行自身自洽且不是悬空，门禁 PASS，
    直到 `--apply` 才被 `CaseVariantMappingError` 拒 —— 白跑一轮上线窗口。
    """
    from scripts.backfill_id_mapping import CorruptMappingError

    await _seedBackfilled(db_session)
    await _insertMapping(db_session, "obj:class:101", "class", "101")

    with pytest.raises(CorruptMappingError) as excInfo:
        await verifyReadOnly(db_session)
    assert "reconcile_milvus_id_mapping" in str(excInfo.value)


async def test_full_mode_catches_set_that_clobbers_cross_store_columns(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探针必须命中「回填真的会写的行」——否则 `set_` 被放宽也全绿。

    这里同时埋两条坑：
    1. 表里有一行 `obj:AGENT:0`，按 unified_id 排序会排在 `obj:CLASS:*` 之前；
       探针若按文本序取首行就永远不会被回填触碰，断言空转。
    2. 把 `_OWNED_COLUMNS` 放宽到 neo4j_node_id，模拟「有人把跨库列加进 set_」。
    探针只要命中 101，这条回归就必须被抓出来。
    """
    await _seedBackfilled(db_session)
    await _insertMapping(db_session, "obj:AGENT:0", "AGENT", "0")
    # 墓碑 id 更小且**有**映射行：探针若漏了存活谓词，就会钉到 90 上——
    # 回填不碰墓碑，哨兵永远不会被抹，断言空转。
    await _insertClass(db_session, 90, "SupplierOld", deleted=True)
    await _insertMapping(db_session, "obj:CLASS:90", "CLASS", "90")
    monkeypatch.setattr(
        backfillModule, "_OWNED_COLUMNS", ("pg_table", "pg_id", "neo4j_node_id")
    )

    with pytest.raises(VerifyFailure) as excInfo:
        await verifyFull(db_session)
    assert "重跑抹掉了跨库落点" in str(excInfo.value)
    assert "obj:CLASS:101" in str(excInfo.value)


async def test_full_mode_requires_rows_to_probe(db_session: AsyncSession) -> None:
    """空表无法验证重跑存活：全量模式必须明说，而不是静默跳过。"""
    with pytest.raises(VerifyFailure) as excInfo:
        await verifyFull(db_session)
    assert "id_mapping 为空" in str(excInfo.value)


async def test_idempotent_check_rolls_back_instead_of_committing(
    db_session: AsyncSession,
) -> None:
    """`--full` 承诺「结束即回滚」：门禁里的回填不得 commit。

    可观测证据：把映射行的 `pg_table`/`pg_id` 置成陈旧值。回填会把它改写成
    `ontology_class` / 类 id —— 若 `_checkIdempotent` 用了默认 `commit=True`，
    这些改写就落在磁盘上（生产上等于一次未获批准写库），从另一个连接读得到；
    正确实现回滚后，另一连接读到的仍是陈旧值。
    """
    from scripts import id_mapping_realdata as gateModule

    await _seedBackfilled(db_session)
    await db_session.execute(
        text(
            "UPDATE id_mapping SET pg_table = 'stale_table', pg_id = '999' "
            "WHERE unified_id = 'obj:CLASS:101'"
        )
    )
    await db_session.commit()

    await gateModule._checkIdempotent(db_session)

    from app.tests._pg_support import _newEngine

    engine = await _newEngine()
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT pg_table, pg_id FROM id_mapping "
                        "WHERE unified_id = 'obj:CLASS:101'"
                    )
                )
            ).one()
    finally:
        await engine.dispose()
    assert tuple(row) == ("stale_table", "999")


async def test_discard_probe_turns_rollback_failure_into_gate_fail(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回滚失败时若只告警，后续断言会在坏会话上报出无关错误 —— 要短路成 FAIL。"""
    from scripts import id_mapping_realdata as gateModule

    async def _boom() -> None:
        raise RuntimeError("connection gone")

    monkeypatch.setattr(db_session, "rollback", _boom)

    with pytest.raises(VerifyFailure) as excInfo:
        await gateModule._discardProbe(db_session)
    assert "回滚失败" in str(excInfo.value)


async def _raiseOperationalError(_session: AsyncSession) -> dict[str, int]:
    from sqlalchemy.exc import OperationalError

    raise OperationalError("SELECT 1", {}, Exception("connection reset"))


async def test_main_reports_database_errors_as_fail(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """数据库异常要落成可读 FAIL，而不是 traceback（上线窗口里最怕这个）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.infrastructure import database as dbModule
    from app.tests._pg_support import _newEngine
    from scripts import id_mapping_realdata as gateModule

    engine = await _newEngine()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(dbModule, "getSessionFactory", lambda: factory)
        monkeypatch.setattr(gateModule, "verifyReadOnly", _raiseOperationalError)

        assert await gateModule.main([]) == 1
    finally:
        await engine.dispose()
    assert "FAIL" in capsys.readouterr().err
