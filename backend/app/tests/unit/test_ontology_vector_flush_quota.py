"""本体向量写路径的 flush 配额纪律（2026-11-17 生产事故）。

事故：Milvus 每集合 flush 配额 0.1 qps（每 10 秒 1 次）⇒ 并发写者互相抢令牌
⇒ 有人把 pymilvus 的 75 次重试预算耗光 ⇒ 硬失败
（``Harness/changes/2026-11-17-fix-milvus-flush-quota/summary.md``）。

本文件钉死三件事：

1. 单条写入的**步骤顺序**（delete → insert）与「不引入第三次 flush」；
2. 同集合的写入**串行**（客户端排队，而不是各自重试互相抢令牌）；
   不同集合**并行**（服务端令牌桶本来就是每集合的）；
3. 属性侧的抑制开关：批量导入时属性**不得**逐条起后台同步任务。

「两次 flush 为什么不能合并成一次」的语义理由见
``app/tests/unit/test_milvus_write_order_guard.py``。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass, OntologyProperty
from app.domain.schemas import OntologyPropertyCreate
from app.services import ontology_service as ontologyServiceModule
from app.services.ontology_service import OntologyService


# ---------------------------------------------------------------------------
# 1. 单条写入的步骤顺序（delete → insert）
# ---------------------------------------------------------------------------

def test_sync_embedding_deletes_then_inserts(monkeypatch: pytest.MonkeyPatch) -> None:
    """写路径 = delete → insert（两者各自自带一次 flush ⇒ 同集合 2 个令牌）。

    **不合并成一次 flush**：Milvus 不保证同一次 flush 里 delete 与 insert 的
    先后 ⇒ delete 表达式可能把同一次 flush 里新插入的行一起删掉 ⇒ 向量静默
    消失。两次 flush 的顺序守卫见 test_milvus_write_order_guard.py。
    """
    calls: list[str] = []

    def _delete(ontologyId: int, type_: str) -> None:
        calls.append(f"delete:{type_}")

    def _insert(records: list[dict]) -> None:
        calls.append("insert")

    monkeypatch.setattr(
        ontologyServiceModule.milvus, "deleteByOntologyIdDual", _delete
    )
    monkeypatch.setattr(ontologyServiceModule.milvus, "insertEmbeddingsDual", _insert)

    OntologyService().syncEmbedding(
        ontologyId=7707,
        type="property",
        name="creator_name",
        alias=None,
        description=None,
        embedding=[0.1],
    )

    assert calls == ["delete:property", "insert"]


# ---------------------------------------------------------------------------
# 2. 同集合串行 / 跨集合并行
# ---------------------------------------------------------------------------

def test_sync_embedding_async_serializes_same_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一集合的两个写入临界区不得重叠（否则互相抢服务端的 flush 令牌）。"""
    service = OntologyService()
    events: list[str] = []

    def _fakeSync(**kwargs) -> None:
        events.append("in")
        time.sleep(0.01)
        events.append("out")

    monkeypatch.setattr(service, "syncEmbedding", _fakeSync)

    async def scenario() -> None:
        await asyncio.gather(
            service.syncEmbeddingAsync(
                ontologyId=1, type="property", name="a",
                alias=None, description=None, embedding=[0.1],
            ),
            service.syncEmbeddingAsync(
                ontologyId=2, type="property", name="b",
                alias=None, description=None, embedding=[0.1],
            ),
        )

    asyncio.run(scenario())

    assert events == ["in", "out", "in", "out"]


def test_sync_embedding_async_different_collections_do_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同集合的写入**同时**在飞（服务端令牌桶是每集合的）。"""
    service = OntologyService()
    events: list[str] = []

    def _fakeSync(**kwargs) -> None:
        events.append(f"{kwargs['type']}-in")
        time.sleep(0.01)
        events.append(f"{kwargs['type']}-out")

    monkeypatch.setattr(service, "syncEmbedding", _fakeSync)

    async def scenario() -> None:
        await asyncio.gather(
            service.syncEmbeddingAsync(
                ontologyId=1, type="property", name="a",
                alias=None, description=None, embedding=[0.1],
            ),
            service.syncEmbeddingAsync(
                ontologyId=2, type="class", name="b",
                alias=None, description=None, embedding=[0.1],
            ),
        )

    asyncio.run(scenario())

    assert [e.endswith("-in") for e in events] == [True, True, False, False]


# ---------------------------------------------------------------------------
# 3. 属性侧的抑制开关（批量导入用）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_property_can_suppress_background_embedding_sync(
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sync_embedding=False`` ⇒ 不起后台向量同步任务（批量导入路径）。"""
    # Neo4j 是**生产数据面**（本地 7687）⇒ 测试里一律打桩，绝不写真图库。
    for name in (
        "upsertPropertyNode",
        "linkClassHasProperty",
        "linkPropertyReferences",
    ):
        monkeypatch.setattr(
            ontologyServiceModule.neo4j, name, lambda *args, **kwargs: None
        )

    owner = OntologyClass(class_name="测试类", created_by="tester")
    dbSession.add(owner)
    await dbSession.commit()
    await dbSession.refresh(owner)

    service = OntologyService()
    scheduled: list[int] = []

    async def _spy(entity) -> None:
        scheduled.append(entity.id)

    monkeypatch.setattr(service, "_syncPropertyEmbeddingBestEffort", _spy)

    dto = OntologyPropertyCreate(
        class_id=owner.id,
        property_name="订单号",
        data_type="STRING",
    )
    await service.createProperty(
        dbSession, dto, actor="tester", sync_embedding=False
    )

    assert scheduled == []


@pytest.mark.asyncio
async def test_create_property_syncs_embedding_by_default(
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认（不传开关）仍走后台同步 —— 抑制只对批量导入路径生效。"""
    for name in (
        "upsertPropertyNode",
        "linkClassHasProperty",
        "linkPropertyReferences",
    ):
        monkeypatch.setattr(
            ontologyServiceModule.neo4j, name, lambda *args, **kwargs: None
        )

    owner = OntologyClass(class_name="测试类2", created_by="tester")
    dbSession.add(owner)
    await dbSession.commit()
    await dbSession.refresh(owner)

    service = OntologyService()
    scheduled: list[int] = []

    async def _spy(entity) -> None:
        scheduled.append(entity.id)

    monkeypatch.setattr(service, "_syncPropertyEmbeddingBestEffort", _spy)

    dto = OntologyPropertyCreate(
        class_id=owner.id,
        property_name="订单号",
        data_type="STRING",
    )
    await service.createProperty(dbSession, dto, actor="tester")

    assert len(scheduled) == 1


# ---------------------------------------------------------------------------
# 4. 对账（syncMissingClassEmbeddings）：锁内重判 + 墓碑过滤
# ---------------------------------------------------------------------------

class _FakeEmbedding:
    async def generateEmbedding(self, text: str) -> list[float]:
        return [0.1, 0.2]


def _stubReconcileDeps(
    service: OntologyService, monkeypatch: pytest.MonkeyPatch
) -> list[list[dict]]:
    """打桩对账的外部依赖（embedding 服务 + v2 双写），返回 insert 调用记录。"""
    monkeypatch.setattr(service, "_ensureEmbedding", lambda: _FakeEmbedding())

    async def _noopV2(records, session=None, **kwargs) -> None:
        return None

    monkeypatch.setattr(
        ontologyServiceModule, "syncRegistryV2Embeddings", _noopV2
    )
    inserted: list[list[dict]] = []
    monkeypatch.setattr(
        ontologyServiceModule.milvus,
        "insertEmbeddingsDual",
        lambda records: inserted.append(list(records)),
    )
    return inserted


@pytest.mark.asyncio
async def test_reconcile_skips_rows_written_while_embeddings_were_generated(
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「读-判-写」跨了整段 embedding 生成 ⇒ 必须在**锁内**重判存在性。

    否则两个并发对账（或对账与在线 syncEmbedding 交错）会各自认为「缺失」
    ⇒ 同一 ontology_id 写两行；对账只补不删 ⇒ 重复向量长期滞留
    （本仓已有 698 组重复向量的历史）。
    """
    owner = OntologyClass(class_name="并发对账类", created_by="tester")
    dbSession.add(owner)
    await dbSession.commit()
    await dbSession.refresh(owner)

    service = OntologyService()
    inserted = _stubReconcileDeps(service, monkeypatch)

    snapshots = [
        [],  # 第一次读：确实缺失
        [{"ontology_id": owner.id, "type": "class", "name": "并发对账类"}],  # 锁内重读：已有
    ]

    def _listRows() -> list[dict]:
        return snapshots.pop(0) if snapshots else snapshots[-1]

    monkeypatch.setattr(
        ontologyServiceModule.milvus, "listEmbeddingsAcross3Collections", _listRows
    )

    await service.syncMissingClassEmbeddings(dbSession)

    # 只针对本用例的类断言：库里还有 seed 出来的其它类，不能断言「一次都没插」。
    insertedClassIds = [
        r["ontology_id"]
        for batch in inserted
        for r in batch
        if r["type"] == "class"
    ]
    assert owner.id not in insertedClassIds, (
        "锁内已存在 ⇒ 不得再插一行（会产生重复向量）"
    )


@pytest.mark.asyncio
async def test_reconcile_ignores_properties_of_tombstoned_class(
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """墓碑类（valid_to 非空）的属性**不得**被对账补回向量。

    OntologyProperty 是硬删、没有 valid_to；类删除只删类向量、不删其属性向量
    ⇒ 属性查询若不过滤父类存活状态，对账会把已删类的属性向量重新写回 ⇒
    墓碑实体在召回层面复活。
    """
    from datetime import UTC, datetime

    tombstoned = OntologyClass(
        class_name="已删类",
        created_by="tester",
        valid_to=datetime.now(UTC),
    )
    alive = OntologyClass(class_name="存活类", created_by="tester")
    dbSession.add_all([tombstoned, alive])
    await dbSession.commit()
    await dbSession.refresh(tombstoned)
    await dbSession.refresh(alive)

    dbSession.add_all([
        OntologyProperty(
            class_id=tombstoned.id, property_name="墓碑属性", data_type="STRING"
        ),
        OntologyProperty(
            class_id=alive.id, property_name="存活属性", data_type="STRING"
        ),
    ])
    await dbSession.commit()

    service = OntologyService()
    inserted = _stubReconcileDeps(service, monkeypatch)
    monkeypatch.setattr(
        ontologyServiceModule.milvus,
        "listEmbeddingsAcross3Collections",
        lambda: [],
    )

    summary = await service.syncMissingClassEmbeddings(dbSession)

    insertedNames = [r["name"] for batch in inserted for r in batch]
    assert "墓碑属性" not in insertedNames, "墓碑类的属性不得被对账补回"
    assert "存活属性" in insertedNames
    # 墓碑类的属性连「对账范围」都不该进：totalProperties 里不能计入它。
    alivePropertyCount = (
        await dbSession.execute(
            select(func.count())
            .select_from(OntologyProperty)
            .join(OntologyClass, OntologyProperty.class_id == OntologyClass.id)
            .where(OntologyClass.valid_to.is_(None))
        )
    ).scalar_one()
    assert summary["totalProperties"] == alivePropertyCount
