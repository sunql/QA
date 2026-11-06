"""backfill_milvus_external_id 的 delete 可见性回归测试（Milvus 用 fake，PG 必须真实）。

根因：Milvus 的 delete 走 ``Proxy → DML channel → DataNode → QueryNode`` 三段异步管道；
``flush()`` 只持久化（Growing → Sealed → 对象存储），**不保证** QueryNode 已装载并应用
delta binlog。脚本删掉旧行后紧接着扫描，仍能查到它 ⇒「第二次扫描找不到 external_id
为空的行」这个幂等前提**不成立** ⇒ 集成用例 ``test_backfill_is_idempotent`` 随机红。

本文件用 fake 复刻该语义：
- ``delete`` 只把主键放进 pending 集合，**不立刻**从可见集合消失；
- 只有 ``release`` + ``load``（重新装载 segment + delta binlog）才让 pending 生效。

于是：不刷新 ⇒ 复扫仍看得见旧行 ⇒ 脚本必须报错；刷新 ⇒ 旧行消失 ⇒ 幂等成立。

另有两条独立缺陷：
- 脚本原先用 ``ontology_id`` 定位待重插的行 ⇒ 同一 ontology_id 存在重复行时
  重插的是**另一行**的内容。本文件用「同 ontology_id 两行」的 fixture 钉住
  「按行自身 auto-id 定位」。
- 脚本原先不校验 insert 的返回条数 ⇒ 服务端写少了也会返回 written（撒谎）。
  本文件用「insert 条数不符 ⇒ 抛错」钉住。

⚠️ **PG 必须是真实的**（Harness/rules/测试规范.md）：本文件用真的 ``dbSession``
夹具（真实 PostgreSQL），不造假库。

共用替身在 ``app/tests/unit/_milvus_backfill_support.py``；安全护栏类用例在
``test_backfill_milvus_safety_guards.py``。
"""

from __future__ import annotations

import pytest

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

# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


async def test_backfill_fills_external_id_for_every_target_row(fakeMilvus, dbSession):
    """external_id 为空的行全部被回填，旧行确实消失，PG 占位行内容正确。"""
    seedOneRowPerCollection(fakeMilvus)

    written = await script.backfill(dbSession)

    assert written == 3
    for collectionName, expectedUid in (
        (script._CLASS_COLLECTION_NAME, "obj:class:5001"),
        (script._PROPERTY_COLLECTION_NAME, "obj:property:5002"),
        (script._METRIC_COLLECTION_NAME, "obj:metric:5003"),
    ):
        rows = fakeMilvus.visibleRows(collectionName)
        assert len(rows) == 1, f"{collectionName} 期望只剩 1 行，实际 {rows}"
        assert rows[0]["external_id"] == expectedUid
        assert rows[0]["name"].startswith(("Class", "Prop", "Metric"))

    # PG 侧必须真的落库，且内容正确（不只计数）。
    classRow = await readIdMapping(dbSession, "obj:class:5001")
    assert classRow is not None, "PG 占位行未写入"
    assert classRow.business_object == "class"
    assert classRow.external_id == "5001"
    assert classRow.milvus_collection == script._CLASS_COLLECTION_NAME


async def test_backfill_is_idempotent_across_two_runs(fakeMilvus, dbSession):
    """第二次运行必须 written=0。

    旧实现（delete 后不刷新）在这里必然红：第二跑仍能查到 external_id 为空的旧行
    ⇒ written2 == 3 ⇒ 幂等契约不成立。
    """
    seedOneRowPerCollection(fakeMilvus)

    written1 = await script.backfill(dbSession)
    written2 = await script.backfill(dbSession)

    assert written1 == 3
    assert written2 == 0, "第二次回填仍有行待写 ⇒ delete 可见性未保证"


async def test_delete_targets_the_rows_own_auto_id_when_ontology_id_duplicated(
    fakeMilvus, dbSession
):
    """同一 ontology_id 存在两行时，必须按「external_id 为空的那一行自己的 auto-id」定位。

    真实损坏形态：旧行（external_id 空）auto-id **更小**，新行（已回填）auto-id 更大
    ⇒ query 按主键升序返回时，旧行在前。
    若改用 ontology_id 建索引，后写的「新行」会覆盖旧行 ⇒ 重插时取到**错行**的内容
    ⇒ 集合里出现两条内容相同、external_id 相同的重复向量。
    """
    fakeMilvus.seed(
        script._CLASS_COLLECTION_NAME,
        [
            # 旧行：auto-id 更小、external_id 为空 —— 这才是要删的那一行
            {"id": 10, "ontology_id": 5001, "type": "class", "name": "STALE",
             "alias": "stale", "description": "", "embedding": [0.2, 0.2], "external_id": ""},
            # 已回填的新行：auto-id 更大、内容正确
            {"id": 11, "ontology_id": 5001, "type": "class", "name": "GOOD",
             "alias": "good", "description": "", "embedding": [0.1, 0.1],
             "external_id": "obj:class:5001"},
        ],
    )

    written = await script.backfill(dbSession)

    assert written == 1
    rows = fakeMilvus.visibleRows(script._CLASS_COLLECTION_NAME)
    ids = {r["id"] for r in rows}
    assert 10 not in ids, "待回填的旧行（id=10）必须被删掉"
    assert 11 in ids, "已回填的新行（id=11）必须原样保留"
    # 重插的行必须沿用**被替换那一行**的内容（STALE），不是同 key 的另一行（GOOD）
    staleCopies = [r for r in rows if r["name"] == "STALE"]
    assert len(staleCopies) == 1, f"重插行内容取错行：{rows}"
    assert staleCopies[0]["embedding"] == [0.2, 0.2]
    assert staleCopies[0]["external_id"] == "obj:class:5001"


async def test_raises_when_deleted_rows_stay_visible_after_refresh(fakeMilvus, dbSession):
    """刷新之后旧行仍可见 ⇒ 抛错（绝不返回可能撒谎的 written）。"""
    seedOneRowPerCollection(fakeMilvus)
    fakeMilvus.applyDeletesOnLoad = False

    with pytest.raises(RuntimeError, match="delete 未生效|回填未生效"):
        await script.backfill(dbSession)


async def test_refresh_happens_after_insert_and_flush(fakeMilvus, dbSession):
    """顺序必须是 delete → insert → flush → release → load → 复扫 query。

    release+load 放在 flush 之后：先落盘再重装，QueryNode 才会同时看到
    「旧行已删」与「新行已插」。
    """
    seedOneRowPerCollection(fakeMilvus)

    await script.backfill(dbSession)

    classCalls = [call for name, call in fakeMilvus.calls if name == script._CLASS_COLLECTION_NAME]
    assert classCalls == [
        # ① 元数据扫描（找待回填行）
        "load", "query",
        # ② 全量扫描（取 embedding）
        "load", "query",
        # ③ 删旧行 → 插新行 → 落盘
        "delete", "insert", "flush",
        # ④ 强制重装（release + load）⇒ delete 对 query 可见
        "release", "load",
        # ⑤ 复扫校验
        "load", "query",
    ], f"class 集合上的调用序列不符：{classCalls}"

    # 共享原语确实被调用，且带上了集合名与超时。
    assert [call["collection"] for call in fakeMilvus.waitCalls] == [
        script._CLASS_COLLECTION_NAME,
        script._PROPERTY_COLLECTION_NAME,
        script._METRIC_COLLECTION_NAME,
    ]
    assert all(call["timeout"] is not None for call in fakeMilvus.waitCalls)
    assert all(call["using"] == "fake_alias" for call in fakeMilvus.waitCalls)


async def test_no_writes_when_nothing_needs_backfill(fakeMilvus, dbSession):
    """没有待回填行 ⇒ 零写、零 delete、零 refresh。"""
    fakeMilvus.seed(
        script._CLASS_COLLECTION_NAME,
        [{"id": 20, "ontology_id": 5001, "type": "class", "name": "OK",
          "alias": "", "description": "", "embedding": [0.1, 0.1],
          "external_id": "obj:class:5001"}],
    )

    written = await script.backfill(dbSession)

    assert written == 0
    mutations = [
        call for _, call in fakeMilvus.calls
        if call in ("delete", "insert", "flush", "release")
    ]
    assert mutations == [], "无待回填行 ⇒ 不得有任何写/删/刷新动作"
    rows = fakeMilvus.visibleRows(script._CLASS_COLLECTION_NAME)
    assert len(rows) == 1
    assert await readIdMapping(dbSession, "obj:class:5001") is None


async def test_row_vanishing_between_the_two_scans_is_skipped(fakeMilvus, dbSession):
    """两次扫描之间消失的行必须跳过（不能插一行 name/embedding 为空的数据）。"""
    seedOneRowPerCollection(fakeMilvus)

    def _removeClassRow(collectionName: str, queryIndex: int) -> None:
        # class 集合的第 2 次 query 是「带 embedding 的全量扫描」。
        if collectionName == script._CLASS_COLLECTION_NAME and queryIndex == 1:
            fakeMilvus.rows[collectionName] = [
                r for r in fakeMilvus.rows[collectionName] if r["id"] != 1
            ]

    fakeMilvus.onQuery = _removeClassRow

    written = await script.backfill(dbSession)

    assert written == 2, "消失的那一行不应计入 written"
    classRows = fakeMilvus.visibleRows(script._CLASS_COLLECTION_NAME)
    assert classRows == []


async def test_raises_when_insert_count_does_not_match(fakeMilvus, dbSession):
    """insert 返回条数与预期不符 ⇒ 抛错（不返回撒谎的 written）。"""
    seedOneRowPerCollection(fakeMilvus)
    fakeMilvus.insertCountOverride = 0

    with pytest.raises(RuntimeError, match="insert 条数不符"):
        await script.backfill(dbSession)


async def test_raises_when_inserted_rows_are_not_visible(fakeMilvus, dbSession):
    """插入的行不可见 ⇒ 抛错（不能认为回填已生效）。"""
    seedOneRowPerCollection(fakeMilvus)
    fakeMilvus.hideInsertedRows = True

    with pytest.raises(RuntimeError, match="回填未生效"):
        await script.backfill(dbSession)


async def test_raises_when_inserted_rows_carry_wrong_external_id(fakeMilvus, dbSession):
    """新行可见但**内容错位**（两行的 external_id 互换）⇒ 抛错。

    这条用例是「后置条件按插入顺序逐行配对」的判别用例：只看「期望的 uid 是否都
    可见」的弱校验会**放行**（两个 uid 都在，只是贴错了行）⇒ 必须逐行配对才抓得住。
    """
    fakeMilvus.seed(
        script._CLASS_COLLECTION_NAME,
        [
            {"id": 30, "ontology_id": 5001, "type": "class", "name": "A",
             "alias": "", "description": "", "embedding": [0.1, 0.1], "external_id": ""},
            {"id": 31, "ontology_id": 5002, "type": "class", "name": "B",
             "alias": "", "description": "", "embedding": [0.2, 0.2], "external_id": ""},
        ],
    )
    fakeMilvus.swapInsertedExternalIds = True

    with pytest.raises(RuntimeError, match="external_id 不符"):
        await script.backfill(dbSession)


async def test_raises_when_query_hits_milvus_page_limit(fakeMilvus, dbSession):
    """读到的行数撞上 Milvus 查询上限 ⇒ 抛错（静默截断 ⇒ 契约成了空话）。"""
    from scripts.milvus_external_id_apply import _MILVUS_QUERY_PAGE

    seedOneRowPerCollection(fakeMilvus)
    fakeMilvus.truncateTo = _MILVUS_QUERY_PAGE

    with pytest.raises(RuntimeError, match="查询上限"):
        await script.backfill(dbSession)


async def test_rows_with_unusable_ids_are_skipped_and_reported(fakeMilvus, dbSession):
    """id / ontology_id 不可用的行 ⇒ 跳过、计数上报，且不写 PG。"""
    fakeMilvus.seed(
        script._CLASS_COLLECTION_NAME,
        [
            {"id": 1, "ontology_id": 5001, "type": "class", "name": "OK",
             "alias": "", "description": "", "embedding": [0.1, 0.1], "external_id": ""},
            # id 不是整数 ⇒ 无法定位 ⇒ 必须跳过
            {"id": "not-an-int", "ontology_id": 5002, "type": "class", "name": "BADID",
             "alias": "", "description": "", "embedding": [0.2, 0.2], "external_id": ""},
        ],
    )

    report = await script.runBackfill(dbSession, apply=True)

    assert report.written == 1
    assert report.skipped == 1
    assert await readIdMapping(dbSession, "obj:class:5001") is not None
    assert await readIdMapping(dbSession, "obj:class:5002") is None


async def test_raises_when_new_row_invisible_but_uid_visible_elsewhere(
    fakeMilvus, dbSession
):
    """新行不可见、但同一个 uid 在**别的行**上可见 ⇒ 仍必须抛错。

    这是「新行不可见」这条后置条件的**判别用例**：
    若只查「期望的 uid 是否可见」，本例会放行（uid 确实可见 —— 在另一行上），
    而真实情况是新行根本没落地 ⇒ 幂等契约不成立。
    """
    fakeMilvus.seed(
        script._CLASS_COLLECTION_NAME,
        [
            # 同一 ontology_id 的**已回填**行（uid 可见）
            {"id": 40, "ontology_id": 5001, "type": "class", "name": "DONE",
             "alias": "", "description": "", "embedding": [0.1, 0.1],
             "external_id": "obj:class:5001"},
            # 待回填行（uid 为空）
            {"id": 41, "ontology_id": 5001, "type": "class", "name": "TODO",
             "alias": "", "description": "", "embedding": [0.2, 0.2], "external_id": ""},
        ],
    )
    fakeMilvus.hideInsertedRows = True

    with pytest.raises(RuntimeError, match="新行不可见"):
        await script.runBackfill(dbSession, apply=True)
