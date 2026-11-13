"""kpi-catalog 手动同步端点（admin 门禁 + 409 语义 + 幂等 + 故障降级）。

⚠️ 本文件**不得**连真 Milvus(19530) / Neo4j(7687) / LLM：
Milvus 与 Neo4j 一律用 monkeypatch 打到模块属性，EmbeddingService 也换成假实现。

补丁点是**模块属性**（``app.services.kpi_vector_graph_sync`` 按名字 import 了
milvus / neo4j 客户端 ⇒ 只有模块属性可 patch；``sync.milvus.*`` 这种属性并不存在）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, PendingRollbackError

from app.domain.models import KpiCatalog
from app.services import kpi_vector_graph_sync as sync

pytestmark = pytest.mark.asyncio

_ADMIN_HEADERS = {
    "X-User-Id": "admin",
    "X-User-Roles": "admin",
    "X-User-Departments": "IT",
}
_USER_HEADERS = {
    "X-User-Id": "regular_user",
    # ⚠️ 必须显式给 user：缺 X-User-Roles 时用 DEFAULT_STUB_ROLES（含 admin）
    # ⇒ 默认放行 ⇒ 「非 admin ⇒ 403」用例会假绿。
    "X-User-Roles": "user",
    "X-User-Departments": "PROCUREMENT",
}

_SYNC_MISSING_URL = "/api/v1/kpi-catalog/embeddings/sync-missing"


class _FakeEmbedding:
    async def generateEmbedding(self, text: str) -> list[float]:
        return [0.01] * 8


@pytest.fixture()
def fakeKpiStores(monkeypatch: pytest.MonkeyPatch) -> dict:
    """fake Milvus / Neo4j / Embedding（补丁点是**模块属性**）。"""
    stores: dict = {
        "milvusPresent": set(),  # str(ontology_id)——fake Milvus 的**现状**
        "kpiIds": set(),  # 图上已有的 unified_id
        "vectorDeletes": set(),
        "vectorInserts": set(),
        # 每次 insert 调用都记一条（**允许重复**）⇒ 能证明「第二轮没有再写」，
        # 而 set 会把重复 id 折叠掉（那样断言恒真 ⇒ 假绿）。
        "vectorInsertCalls": [],
        "refreshCalls": 0,
    }

    class _FakeNeo4j:
        @staticmethod
        def getKpiCatalogIds() -> set[str]:
            return set(stores["kpiIds"])

        @staticmethod
        def upsertKpiCatalogNode(**kw) -> None:
            stores["kpiIds"].add(kw["unified_id"])

        @staticmethod
        def linkKpiMeasuredBy(*a, **k) -> None:
            pass

    def _fakeDelete(ontologyId: int, type: str) -> None:
        stores["vectorDeletes"].add(str(ontologyId))
        # 真 Milvus：delete 会让该向量从现状里消失 ⇒ fake 必须同步维护现状，
        # 否则 fake 是「只进不出」的替身 ⇒ 「补过之后复跑不再补」证明不了。
        stores["milvusPresent"].discard(str(ontologyId))

    def _fakeInsert(records: list[dict]) -> None:
        for record in records:
            stores["vectorInserts"].add(str(record.get("ontology_id")))
            stores["vectorInsertCalls"].append(str(record.get("ontology_id")))
            stores["milvusPresent"].add(str(record.get("ontology_id")))

    def _fakeQuery() -> list[dict]:
        return [{"ontology_id": int(i)} for i in stores["milvusPresent"]]

    def _fakeEnsureCollection() -> object:
        return object()

    def _fakeRefresh(collection: object) -> None:
        stores["refreshCalls"] += 1

    monkeypatch.setattr(sync, "neo4j", _FakeNeo4j)
    monkeypatch.setattr(sync, "insertEmbeddingsDual", _fakeInsert)
    monkeypatch.setattr(sync, "deleteByOntologyIdDual", _fakeDelete)
    monkeypatch.setattr(sync, "EmbeddingService", _FakeEmbedding)
    monkeypatch.setattr(sync, "queryKpiEmbeddings", _fakeQuery)
    # refresh 是「孤儿已删后的可见性刷新」⇒ release+load 会真碰 Milvus ⇒ 一并 fake
    monkeypatch.setattr(sync, "ensureKpiCollection", _fakeEnsureCollection)
    monkeypatch.setattr(sync, "refreshCollection", _fakeRefresh)
    return stores


@pytest_asyncio.fixture
async def seedPublishedKpi(dbSession):
    kpi = KpiCatalog(
        kpi_code="KPI_SYNC_PUB_TEST",
        kpi_name="同步端点-已发布",
        status="PUBLISHED",
        match_threshold=Decimal("0.75"),
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


@pytest_asyncio.fixture
async def seedDraftKpi(dbSession):
    kpi = KpiCatalog(
        kpi_code="KPI_SYNC_DRAFT_TEST",
        kpi_name="同步端点-草稿",
        status="DRAFT",
        match_threshold=Decimal("0.75"),
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


async def test_sync_missing_endpoint_idempotent(
    client, seedPublishedKpi, fakeKpiStores
):
    """复跑第二轮 counts 全 0（验收标准 3）。"""
    fakeKpiStores["milvusPresent"].add(str(seedPublishedKpi.id))
    fakeKpiStores["kpiIds"].add(f"obj:KPI:{seedPublishedKpi.id}")

    first = await client.post(_SYNC_MISSING_URL, headers=_ADMIN_HEADERS)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["vectorPublishedTotal"] == 1
    assert body["vectorMissingCount"] == 0
    assert body["graphMissingCount"] == 0
    # 第一轮也不该动向量（Milvus 里已有）也不该建图节点（图上已有）
    assert body["vectorSyncedCount"] == 0
    assert body["vectorOrphanCount"] == 0
    assert body["graphSyncedCount"] == 0
    assert body["failedCount"] == 0
    assert body["failures"] == []
    assert fakeKpiStores["vectorInserts"] == set()
    assert fakeKpiStores["vectorDeletes"] == set()

    second = await client.post(_SYNC_MISSING_URL, headers=_ADMIN_HEADERS)
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["vectorMissingCount"] == 0
    assert body2["vectorSyncedCount"] == 0
    assert body2["vectorOrphanCount"] == 0
    assert body2["graphSyncedCount"] == 0
    assert body2["graphMissingCount"] == 0
    assert body2["failedCount"] == 0


async def test_sync_missing_endpoint_syncs_absent_vector(
    client, seedPublishedKpi, fakeKpiStores
):
    """Milvus 里没有该 PUBLISHED KPI ⇒ 补一条向量（并只补这一条）。"""
    fakeKpiStores["kpiIds"].add(f"obj:KPI:{seedPublishedKpi.id}")

    resp = await client.post(_SYNC_MISSING_URL, headers=_ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vectorPublishedTotal"] == 1
    assert body["vectorMissingCount"] == 1
    assert body["vectorSyncedCount"] == 1
    assert body["vectorOrphanCount"] == 0
    assert body["failedCount"] == 0
    assert fakeKpiStores["vectorInserts"] == {str(seedPublishedKpi.id)}
    # DRAFT 不进向量期望集 ⇒ 见 test_sync_missing_skips_draft_kpi_vector（D3 专项）


async def test_sync_missing_endpoint_deletes_orphan_vectors(
    client, seedPublishedKpi, fakeKpiStores
):
    """Milvus 有、PG 非 PUBLISHED/不存在 ⇒ 孤儿被删，并刷新集合可见性。"""
    fakeKpiStores["milvusPresent"].add("999999")
    fakeKpiStores["kpiIds"].add(f"obj:KPI:{seedPublishedKpi.id}")

    resp = await client.post(_SYNC_MISSING_URL, headers=_ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vectorOrphanCount"] == 1
    assert "999999" in fakeKpiStores["vectorDeletes"]
    # 孤儿删了 ⇒ 刷一次集合可见性（release+load）
    assert fakeKpiStores["refreshCalls"] == 1


async def test_sync_missing_requires_admin(client):
    resp = await client.post(_SYNC_MISSING_URL, headers=_USER_HEADERS)
    assert resp.status_code in (401, 403)
    assert resp.status_code == 403


async def test_resync_endpoint_requires_admin(client, seedPublishedKpi, fakeKpiStores):
    resp = await client.post(
        f"/api/v1/kpi-catalog/{seedPublishedKpi.id}/embedding",
        headers=_USER_HEADERS,
    )
    assert resp.status_code == 403


async def test_resync_endpoint_409_for_draft(client, seedDraftKpi, fakeKpiStores):
    resp = await client.post(
        f"/api/v1/kpi-catalog/{seedDraftKpi.id}/embedding",
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 409
    assert "PUBLISHED" in resp.json()["detail"]
    # 409 ⇒ 不该有任何向量写入
    assert fakeKpiStores["vectorInserts"] == set()
    assert fakeKpiStores["vectorDeletes"] == set()


async def test_resync_endpoint_404_for_missing_kpi(client, fakeKpiStores):
    resp = await client.post(
        "/api/v1/kpi-catalog/987654321/embedding",
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 404


async def test_resync_endpoint_rebuilds_vector(client, seedPublishedKpi, fakeKpiStores):
    resp = await client.post(
        f"/api/v1/kpi-catalog/{seedPublishedKpi.id}/embedding",
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resynced"] is True
    # delete-first 后按 PUBLISHED 重插：先删后插各一次
    assert str(seedPublishedKpi.id) in fakeKpiStores["vectorDeletes"]
    assert str(seedPublishedKpi.id) in fakeKpiStores["vectorInserts"]


async def test_sync_missing_survives_neo4j_read_failure(
    client, seedPublishedKpi, fakeKpiStores, monkeypatch
):
    """Neo4j 不可达 ⇒ 端点 **200** + 段级失败明细（不 500）。"""

    def _boom() -> set[str]:
        raise RuntimeError("neo4j 不可达")

    monkeypatch.setattr(sync.neo4j, "getKpiCatalogIds", staticmethod(_boom))
    fakeKpiStores["milvusPresent"].add(str(seedPublishedKpi.id))

    resp = await client.post(_SYNC_MISSING_URL, headers=_ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["failedCount"] >= 1
    kpiFailures = [f for f in body["failures"] if f["entityType"] == "kpi"]
    assert kpiFailures, body["failures"]
    assert "neo4j 不可达" in kpiFailures[0]["error"]
    # 段级失败不归属单行 ⇒ entityId = 0（SECTION_FAILURE_ENTITY_ID）
    assert kpiFailures[0]["entityId"] == 0
    # 向量段不受图侧故障影响
    assert body["vectorMissingCount"] == 0


async def test_sync_missing_survives_milvus_read_failure(
    client, seedPublishedKpi, fakeKpiStores, monkeypatch
):
    """Milvus 读失败 ⇒ 端点 **200** + 一条 milvus 失败明细（不 500）。

    且**整个向量段跳过**（既不补缺失也不删孤儿）：读不到现状时若照
    「现状=空集」处理，会把全部 PUBLISHED KPI 当成缺失 ⇒ 批量重算 embedding
    （打真 LLM、零收益）。⇒ 一个向量都不许写。
    """

    def _boom() -> list[dict]:
        raise RuntimeError("milvus 读失败")

    monkeypatch.setattr(sync, "queryKpiEmbeddings", _boom)

    resp = await client.post(_SYNC_MISSING_URL, headers=_ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["failedCount"] >= 1
    milvusFailures = [f for f in body["failures"] if f["entityType"] == "milvus"]
    assert milvusFailures, body["failures"]
    assert "milvus 读失败" in milvusFailures[0]["error"]
    # 向量段整体跳过（读失败 ≠ 现状为空）
    assert body["vectorMissingCount"] == 0
    assert body["vectorOrphanCount"] == 0
    assert body["vectorSyncedCount"] == 0
    # 核心断言：一个向量都不许写（否则 Milvus 全挂时 LLM 费用照付、零收益）
    assert fakeKpiStores["vectorInserts"] == set()
    assert fakeKpiStores["vectorDeletes"] == set()
    assert fakeKpiStores["vectorInsertCalls"] == []


async def test_sync_missing_second_round_after_sync_is_noop(
    client, seedPublishedKpi, fakeKpiStores
):
    """真正的幂等：第 1 轮补了向量 ⇒ 第 2 轮**不再补**。

    与 ``test_sync_missing_endpoint_idempotent``（预置 milvusPresent ⇒ 一开始
    就没缺）不同：本用例第 1 轮**真的写入了向量**（fake Milvus 有增删语义），
    第 2 轮必须读到「刚写进去的那条」⇒ 缺失集为空 ⇒ 计数全 0。
    """
    # 注意：**不**预置 milvusPresent ⇒ 第 1 轮必然判缺失
    resp = await client.post(_SYNC_MISSING_URL, headers=_ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vectorPublishedTotal"] == 1
    assert body["vectorMissingCount"] == 1
    assert body["vectorSyncedCount"] == 1
    assert body["failedCount"] == 0
    assert str(seedPublishedKpi.id) in fakeKpiStores["vectorInserts"]
    insertCallsAfterFirst = list(fakeKpiStores["vectorInsertCalls"])
    assert len(insertCallsAfterFirst) == 1

    second = await client.post(_SYNC_MISSING_URL, headers=_ADMIN_HEADERS)
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["vectorMissingCount"] == 0
    assert body2["vectorSyncedCount"] == 0
    assert body2["vectorOrphanCount"] == 0
    assert body2["graphMissingCount"] == 0
    assert body2["graphSyncedCount"] == 0
    assert body2["failedCount"] == 0
    # 第 2 轮一次 insert 都不许发生（insertCalls 保留重复 ⇒ 能真的抓到重复写）
    assert fakeKpiStores["vectorInsertCalls"] == insertCallsAfterFirst


async def test_sync_missing_skips_draft_kpi_vector(
    client, seedDraftKpi, fakeKpiStores
):
    """D3：KPI 向量**只索引 PUBLISHED** ⇒ DRAFT 不建向量（但图节点保留）。

    DRAFT 不在向量期望集里 ⇒ 也不在缺失集里 ⇒ 不该产生任何向量写入。
    """
    resp = await client.post(_SYNC_MISSING_URL, headers=_ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vectorPublishedTotal"] == 0
    assert body["vectorMissingCount"] == 0
    assert body["vectorSyncedCount"] == 0
    assert str(seedDraftKpi.id) not in fakeKpiStores["vectorInserts"]
    assert fakeKpiStores["vectorInsertCalls"] == []
    assert fakeKpiStores["vectorDeletes"] == set()


async def test_reconcile_kpi_graph_rolls_back_so_session_stays_usable(
    dbSession, fakeKpiStores
):
    """C11：库侧读取失败 ⇒ 受保护的回滚 ⇒ session 之后**仍可用**。

    复现真实 aborted 事务：先让一次 flush 失败（NOT NULL 违例）⇒ PG 事务进入
    aborted 态 ⇒ 后续任何 SQL 都抛 PendingRollbackError。
    没有回滚时，reconcileKpiGraph 之后的查询会继续抛 PendingRollbackError
    （只断言端点 200 证明不了事务已恢复 ⇒ 这里断言 session 能继续查询）。
    """
    dbSession.add(KpiCatalog(kpi_code=None, kpi_name="坏行"))
    with pytest.raises(IntegrityError):
        await dbSession.flush()
    # 前置条件：未回滚 ⇒ 查询抛 PendingRollbackError
    with pytest.raises(PendingRollbackError):
        await dbSession.execute(select(func.count(KpiCatalog.id)))

    result = await sync.reconcileKpiGraph(dbSession)
    assert result["failedCount"] == 1
    assert result["failures"][0]["entityType"] == "kpi"
    assert result["failures"][0]["entityId"] == 0
    # 失败明细必须真的带上「事务已 aborted」的原因（不是空串/泛化文案）
    assert "rolled back" in result["failures"][0]["error"]
    assert "flush" in result["failures"][0]["error"]

    # 回滚后 session 仍可用（这才是「事务已恢复」的证据）
    total = (await dbSession.execute(select(func.count(KpiCatalog.id)))).scalar_one()
    assert total == 0
