"""backfill_milvus_external_id 的**安全护栏**测试（Milvus 用 fake，PG 必须真实）。

这些用例覆盖的是「脚本不许做的事」，逐条对应安全评审的发现：

- HIGH-1：默认预演（不加 ``--apply``）不得写 PG、不得删/插 Milvus；
- HIGH-2：PG 占位行的**三态**判定（已存在 / 冲突 / 不可用）必须分清，
  不能只看「INSERT 有没有抛异常」；
- MEDIUM-1：delete **之前**的载荷体检（缺 embedding / 集合缺字段）；
- MEDIUM-6：delete 必须批量（``id in [...]``），把危险窗口压到一次往返；
- LOW-1：PG 原始错误必须进日志（不能只有异常类名）；
- LOW-2：统一 ID 必须走 SSOT 构造器；
- LOW-4：单集合失败不打断其它集合，且失败原因必须被记录；
- LOW-6：复扫必须用强一致（``consistency_level="Strong"``）。

⚠️ **PG 必须是真实的**（Harness/rules/测试规范.md）：用真的 ``dbSession``
夹具（真实 PostgreSQL）⇒ 冲突 / 主键冲突等路径都是**真约束**触发的。

共用替身在 ``app/tests/unit/_milvus_backfill_support.py``；delete 可见性与幂等
契约的用例在 ``test_backfill_milvus_delete_visibility.py``。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

import scripts.backfill_milvus_external_id as script
from app.tests.unit._milvus_backfill_support import (
    FakeStore,
    makeFakeMilvus,
    readIdMapping,
    seedOneRowPerCollection,
)


@pytest.fixture()
def fakeMilvus(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """假 Milvus（共享装配见 ``_milvus_backfill_support.makeFakeMilvus``）。"""
    return makeFakeMilvus(monkeypatch)


async def test_pg_primary_key_conflict_skips_row_without_deleting_milvus_row(
    fakeMilvus, dbSession
):
    """PG 主键冲突（真实 IntegrityError）⇒ 跳过该行，不删 Milvus 行、不计入 written。

    ``id_mapping.unified_id`` 是主键；脚本的 ``ON CONFLICT (business_object, external_id)``
    **覆盖不到**主键冲突 ⇒ 这条路径必然抛 IntegrityError。
    """
    seedOneRowPerCollection(fakeMilvus)
    # 预置一行：unified_id 相同、但 (business_object, external_id) 不同 ⇒ 主键冲突。
    await dbSession.execute(
        text(
            "INSERT INTO id_mapping "
            "(unified_id, business_object, external_id, milvus_collection, "
            "created_time, updated_time) "
            "VALUES (:uid, :bo, :ext, :mc, now(), now())"
        ),
        {
            "uid": "obj:class:5001",
            "bo": "property",
            "ext": "9999",
            "mc": script._PROPERTY_COLLECTION_NAME,
        },
    )
    await dbSession.commit()

    report = await script.runBackfill(dbSession, apply=True)

    assert report.written == 2, "主键冲突的那一行不应计入 written"
    assert report.skipped == 1
    classRows = fakeMilvus.visibleRows(script._CLASS_COLLECTION_NAME)
    assert len(classRows) == 1, "PG 失败时不得删除 Milvus 行"
    assert classRows[0]["id"] == 1
    assert classRows[0]["external_id"] == "", "PG 失败时不得重插该行"


async def test_pg_placeholder_is_durable_when_milvus_mutation_fails(
    fakeMilvus, dbSession, seedEngine
):
    """Milvus 变更失败时，PG 占位行**已经落盘**（PG 先 commit ⇒ 不会留下半截状态）。"""
    seedOneRowPerCollection(fakeMilvus)
    fakeMilvus.insertCountOverride = 0

    with pytest.raises(RuntimeError):
        await script.backfill(dbSession)

    # 用**另一个** session 读 ⇒ 证明已 commit（不是只在内存事务里）。
    factory, _ = seedEngine
    async with factory() as freshSession:
        row = await readIdMapping(freshSession, "obj:class:5001")
    assert row is not None, "Milvus 失败时 PG 占位行必须已经落盘"
    assert row.external_id == "5001"


# ---------------------------------------------------------------------------
# PG 占位行的三态（安全评审 HIGH-2）
# ---------------------------------------------------------------------------


async def test_pg_placeholder_already_exists_is_treated_as_ready(fakeMilvus, dbSession):
    """PG 里已有**同 unified_id + 同 (bo, ext)** 的占位行 ⇒ 不是失败，照常回填。

    这是**生产常态**：`scripts/reconcile_milvus_id_mapping.py` 的职责就是给
    「仅 Milvus 有」的行写占位行 ⇒ 本脚本要处理的行在 PG 里通常**已经存在**。

    旧实现只看「有没有抛 IntegrityError」：主键冲突（`unified_id` 是主键，
    而 `ON CONFLICT (business_object, external_id)` 覆盖不到它）⇒ 整行跳过
    ⇒ **向量一行都修不了**，运维只会看到「PG 写失败」的误导性日志。
    """
    seedOneRowPerCollection(fakeMilvus)
    await dbSession.execute(
        text(
            "INSERT INTO id_mapping "
            "(unified_id, business_object, external_id, milvus_collection, "
            "created_time, updated_time) "
            "VALUES (:uid, :bo, :ext, :mc, now(), now())"
        ),
        {
            "uid": "obj:class:5001",
            "bo": "class",
            "ext": "5001",
            "mc": script._CLASS_COLLECTION_NAME,
        },
    )
    await dbSession.commit()

    report = await script.runBackfill(dbSession, apply=True)

    assert report.written == 3, "PG 占位行已存在时不得跳过该行"
    assert report.skipped == 0
    rows = fakeMilvus.visibleRows(script._CLASS_COLLECTION_NAME)
    assert len(rows) == 1
    assert rows[0]["external_id"] == "obj:class:5001"


async def test_pg_placeholder_with_conflicting_unified_id_is_reported_as_conflict(
    fakeMilvus, dbSession
):
    """PG 里 (bo, ext) 已被**另一个** unified_id 占用 ⇒ 判为冲突，不得改写。

    `ON CONFLICT (business_object, external_id) DO NOTHING` 会**静默放过**
    这种行 ⇒ 旧实现照样把 Milvus 的 external_id 写成期望值 ⇒
    「Milvus.external_id ↔ PG.unified_id」的契约其实没建立（悬垂引用），
    且事后按 external_id 反查 PG 查不到。
    """
    seedOneRowPerCollection(fakeMilvus)
    await dbSession.execute(
        text(
            "INSERT INTO id_mapping "
            "(unified_id, business_object, external_id, milvus_collection, "
            "created_time, updated_time) "
            "VALUES (:uid, :bo, :ext, :mc, now(), now())"
        ),
        {
            "uid": "obj:class:SOMEONE-ELSE",
            "bo": "class",
            "ext": "5001",
            "mc": script._CLASS_COLLECTION_NAME,
        },
    )
    await dbSession.commit()

    report = await script.runBackfill(dbSession, apply=True)

    assert report.written == 2, "冲突的那一行不应被写进 Milvus"
    assert report.pgConflict == 1, "冲突必须与「写失败」分开计数"
    assert report.skipped == 1
    # PG 里那一行不得被改写
    row = await readIdMapping(dbSession, "obj:class:SOMEONE-ELSE")
    assert row is not None and row.business_object == "class"
    # Milvus 行不得被删（否则内容就丢了）
    classRows = fakeMilvus.visibleRows(script._CLASS_COLLECTION_NAME)
    assert len(classRows) == 1
    assert classRows[0]["external_id"] == "", "冲突时不得重插该行"


# ---------------------------------------------------------------------------
# delete 之前的载荷体检（安全评审 MEDIUM-1）
# ---------------------------------------------------------------------------


async def test_row_with_missing_embedding_is_skipped_before_delete(fakeMilvus, dbSession):
    """待重插的行缺 embedding ⇒ 排除出本批次，**且不得删它**。

    旧实现的 ``full.get("embedding") or []`` 会把缺失的向量悄悄变成空列表
    ⇒ 整批 insert 因维度不匹配失败 ⇒ **同批已 delete 的行全部丢失**
    （Milvus 无自动备份 ⇒ 不可逆）。
    """
    fakeMilvus.seed(
        script._CLASS_COLLECTION_NAME,
        [
            {"id": 1, "ontology_id": 5001, "type": "class", "name": "GOOD",
             "alias": "", "description": "", "embedding": [0.1, 0.1], "external_id": ""},
            {"id": 2, "ontology_id": 5002, "type": "class", "name": "NOEMBED",
             "alias": "", "description": "", "embedding": [], "external_id": ""},
        ],
    )

    report = await script.runBackfill(dbSession, apply=True)

    assert report.written == 1, "只有载荷完好的那一行可以进本批次"
    assert report.invalid == 1
    rows = fakeMilvus.visibleRows(script._CLASS_COLLECTION_NAME)
    kept = {r["id"]: r for r in rows}
    assert 2 in kept, "载荷不合格的行不得被删除"
    assert kept[2]["external_id"] == "", "载荷不合格的行不得被重插"
    assert 1 not in kept, "载荷完好的那一行应当被替换"


async def test_row_with_missing_embedding_is_skipped_even_without_known_dimension(
    fakeMilvus, dbSession
):
    """维度未知时，缺 embedding 的行**仍然**必须被排除。

    变异取证 M13 暴露的盲区：原来那条用例（``embedding: []``）会被**另一条**
    守卫（维度不符：0 ≠ 集合维度 2）挡住 ⇒ 「embedding 为空」这一支**从未被
    单独验证**。把集合的 embedding 维度设成未知（读不到 dim）后，
    「embedding 为空」就是唯一能拦住这行的检查 ⇒ 它必须自己会红。
    """
    fakeMilvus.embeddingDim = None  # 集合读不出向量维度 ⇒ 只剩「为空」这一条判据
    fakeMilvus.seed(
        script._CLASS_COLLECTION_NAME,
        [
            {"id": 1, "ontology_id": 5001, "type": "class", "name": "GOOD",
             "alias": "", "description": "", "embedding": [0.1, 0.1], "external_id": ""},
            {"id": 2, "ontology_id": 5002, "type": "class", "name": "NOEMBED",
             "alias": "", "description": "", "embedding": [], "external_id": ""},
        ],
    )

    report = await script.runBackfill(dbSession, apply=True)

    assert report.written == 1, "缺 embedding 的行不得进本批次"
    assert report.invalid == 1, "缺 embedding 必须被单独判为 invalid"
    rows = fakeMilvus.visibleRows(script._CLASS_COLLECTION_NAME)
    kept = {r["id"]: r for r in rows}
    assert 2 in kept, "载荷不合格的行不得被删除"


async def test_raises_when_insert_primary_keys_count_does_not_match(fakeMilvus, dbSession):
    """insert 返回的 ``primary_keys`` 数量对不上 ⇒ 抛错（无法逐行校验 ⇒ 拒绝返回 written）。

    与「insert 条数不符」是**两条不同的守卫**：条数对得上但主键列表被截断时，
    后置条件就配不出「哪一行是新行」⇒ 只能整体拒绝。
    """
    seedOneRowPerCollection(fakeMilvus)
    fakeMilvus.primaryKeysCountOverride = 0

    with pytest.raises(RuntimeError, match="primary_keys"):
        await script.backfill(dbSession)


async def test_collection_missing_required_field_fails_before_any_delete(
    fakeMilvus, dbSession
):
    """集合 schema 缺我们要写的字段 ⇒ 在 **delete 之前**就抛错。

    少一个字段 ⇒ 整批 insert 失败 ⇒ 若已经 delete 过了，同批的行全部丢失。
    """
    seedOneRowPerCollection(fakeMilvus)
    fakeMilvus.missingFields = {"alias"}

    with pytest.raises(RuntimeError, match="字段|schema"):
        await script.runBackfill(dbSession, apply=True)

    deletes = [call for _, call in fakeMilvus.calls if call == "delete"]
    assert deletes == [], "字段体检必须在任何 delete 之前"
    rows = fakeMilvus.visibleRows(script._CLASS_COLLECTION_NAME)
    assert len(rows) == 1, "不得删除任何行"


# ---------------------------------------------------------------------------
# 预演（默认只读）—— 安全评审 HIGH-1
# ---------------------------------------------------------------------------


async def test_dry_run_does_not_write_anything(fakeMilvus, dbSession):
    """默认预演：既不写 PG，也不删/插 Milvus。"""
    seedOneRowPerCollection(fakeMilvus)

    report = await script.runBackfill(dbSession, apply=False)

    assert report.written == 0, "预演不得写任何行"
    assert report.planned == 3, "预演必须报告「将要写几行」"
    mutations = [
        call for _, call in fakeMilvus.calls
        if call in ("delete", "insert", "flush", "release")
    ]
    assert mutations == [], f"预演不得有写操作，实际：{mutations}"
    assert await readIdMapping(dbSession, "obj:class:5001") is None, "预演不得写 PG"


async def test_dry_run_reports_pg_conflict_without_writing(fakeMilvus, dbSession):
    """预演也要暴露 PG 冲突（否则运维看不到「这行永远修不了」）。"""
    seedOneRowPerCollection(fakeMilvus)
    await dbSession.execute(
        text(
            "INSERT INTO id_mapping "
            "(unified_id, business_object, external_id, milvus_collection, "
            "created_time, updated_time) "
            "VALUES (:uid, :bo, :ext, :mc, now(), now())"
        ),
        {
            "uid": "obj:class:SOMEONE-ELSE",
            "bo": "class",
            "ext": "5001",
            "mc": script._CLASS_COLLECTION_NAME,
        },
    )
    await dbSession.commit()

    report = await script.runBackfill(dbSession, apply=False)

    assert report.planned == 2, "冲突的行不计入「将要写」"
    assert report.pgConflict == 1


# ---------------------------------------------------------------------------
# 单集合失败不打断其它集合（安全评审 LOW-4）
# ---------------------------------------------------------------------------


async def test_one_failing_collection_does_not_abort_the_others(fakeMilvus, dbSession):
    """一个集合失败 ⇒ 继续处理其余集合，最后统一汇报（不是裸 traceback）。"""
    seedOneRowPerCollection(fakeMilvus)
    original = script._backfillCollection

    async def _explodeForClass(session, type_, collectionName, *, apply):
        if type_ == "class":
            raise RuntimeError("模拟 class 集合失败")
        return await original(session, type_, collectionName, apply=apply)

    script._backfillCollection = _explodeForClass
    try:
        report = await script.runBackfill(dbSession, apply=True)
    finally:
        script._backfillCollection = original

    assert report.failedCollections == 1
    assert report.written == 2, "其它两个集合必须照常回填"
    assert report.errors, "失败原因必须被记录下来（不是静默吞掉）"


# ---------------------------------------------------------------------------
# 统一 ID 必须走 SSOT 构造器（安全评审 LOW-2）
# ---------------------------------------------------------------------------


async def test_external_id_is_built_by_the_unified_id_ssot(fakeMilvus, dbSession):
    """external_id 必须等于 ``buildUnifiedId(business_object, external_id)``。

    手工拼 ``f"obj:{type_}:{ontologyId}"`` 绕过了 SSOT 的去空白 / NFC 归一 /
    非法字符 / 长度校验 —— 一旦 type 变成外部输入就失守。
    """
    from app.domain.id_mapping import buildUnifiedId

    seedOneRowPerCollection(fakeMilvus)

    await script.runBackfill(dbSession, apply=True)

    rows = fakeMilvus.visibleRows(script._CLASS_COLLECTION_NAME)
    assert rows[0]["external_id"] == buildUnifiedId("class", "5001")


# ---------------------------------------------------------------------------
# delete 必须批量（安全评审 MEDIUM-6）
# ---------------------------------------------------------------------------


async def test_delete_is_batched_into_one_in_expression(fakeMilvus, dbSession):
    """N 行 ⇒ 一次 ``id in [...]`` delete（不是 N 次 RPC）。

    逐行 delete 会把「delete 已发出、insert 还没发出」的危险窗口拉长到
    N 次串行往返（生产 property 集合 3743 行 ⇒ 分钟级）。
    """
    fakeMilvus.seed(
        script._CLASS_COLLECTION_NAME,
        [
            {"id": 1, "ontology_id": 5001, "type": "class", "name": "A",
             "alias": "", "description": "", "embedding": [0.1, 0.1], "external_id": ""},
            {"id": 2, "ontology_id": 5002, "type": "class", "name": "B",
             "alias": "", "description": "", "embedding": [0.2, 0.2], "external_id": ""},
            {"id": 3, "ontology_id": 5003, "type": "class", "name": "C",
             "alias": "", "description": "", "embedding": [0.3, 0.3], "external_id": ""},
        ],
    )

    await script.runBackfill(dbSession, apply=True)

    deletes = [call for name, call in fakeMilvus.calls
               if call == "delete" and name == script._CLASS_COLLECTION_NAME]
    assert len(deletes) == 1, "3 行应当一次批量 delete"
    exprs = [expr for name, expr in fakeMilvus.deleteExprs
             if name == script._CLASS_COLLECTION_NAME]
    assert exprs == ["id in [1, 2, 3]"], f"应当是一条批量 delete 表达式，实际：{exprs}"


# ---------------------------------------------------------------------------
# 复扫必须用强一致（安全评审 LOW-6）
# ---------------------------------------------------------------------------


async def test_rescan_query_uses_strong_consistency(fakeMilvus, dbSession):
    """复扫校验的 query 必须显式 ``consistency_level="Strong"``。

    集合默认 Bounded（5s 容忍窗口）⇒ 复扫可能读到旧视图 ⇒ 后置条件形同虚设。
    release+load 之后数据已封段，Strong 不会误判。
    """
    seedOneRowPerCollection(fakeMilvus)

    await script.runBackfill(dbSession, apply=True)

    classQueries = [
        call for call in fakeMilvus.queryCalls
        if call["collection"] == script._CLASS_COLLECTION_NAME
    ]
    assert classQueries, "至少要查一次"
    assert classQueries[-1]["consistency_level"] == "Strong", (
        f"复扫查询必须是强一致：{classQueries[-1]}"
    )


# ---------------------------------------------------------------------------
# PG 原始错误必须进日志（安全评审 LOW-1）
# ---------------------------------------------------------------------------


async def test_pg_integrity_error_logs_the_database_cause(
    fakeMilvus, dbSession, caplog
):
    """PG 冲突时日志里必须有 **数据库的原始错误**，不能只有异常类名。

    只有类名的话，运维无法区分「占位行已存在（正常）」与「真冲突（要处理）」。
    """
    seedOneRowPerCollection(fakeMilvus)
    await dbSession.execute(
        text(
            "INSERT INTO id_mapping "
            "(unified_id, business_object, external_id, milvus_collection, "
            "created_time, updated_time) "
            "VALUES (:uid, :bo, :ext, :mc, now(), now())"
        ),
        {
            "uid": "obj:class:5001",
            "bo": "property",
            "ext": "9999",
            "mc": script._PROPERTY_COLLECTION_NAME,
        },
    )
    await dbSession.commit()

    with caplog.at_level("WARNING"):
        report = await script.runBackfill(dbSession, apply=True)

    assert report.pgConflict == 1
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "duplicate key" in logged or "unique constraint" in logged, (
        f"日志里必须有 PG 原始错误，实际：{logged}"
    )
