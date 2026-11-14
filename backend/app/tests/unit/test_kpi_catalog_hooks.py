"""kpi_catalog_service CRUD 提交后的向量/图同步钩子（全部打 stub，不碰真库外存储）。

被测行为（feat-kpi-vector-graph-sync，设计 §3/§4）：

- create：图同步（全状态）；仅 PUBLISHED 才同步向量（D3）；
- update：图同步；PUBLISHED ⇒ 同步向量；PUBLISHED → DRAFT/DEPRECATED ⇒
  **删向量、图节点保留**（status 属性更新）；
- delete：删向量 + 删图节点；
- 开关关闭（``isKpiSyncEnabled`` ⇒ False）⇒ 一个钩子都不触发。

⚠️ 本地 Milvus(19530) / Neo4j(7687) 是生产，**禁止连接** ⇒ 4 个
``*BestEffort`` 入口全部 monkeypatch 成记录器（不落到真 Milvus/Neo4j）。

⚠️ unit/ 目录的 conftest **没有** ``seed_data_source``（只有 integration/ 有），
故本文件自带 DataSource seed（照抄 test_supplier_risk_service._seedDataSource）。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.models import DataSource, KpiCatalog
from app.domain.schemas import KpiCatalogCreate, KpiCatalogUpdate
from app.services import kpi_catalog_service as svc_module
from app.services.kpi_catalog_service import KpiCatalogService

pytestmark = pytest.mark.asyncio


def _actor() -> CurrentUser:
    """默认 CurrentUser：roles 含 admin ⇒ ACL 通过（既有先例见
    test_kpi_catalog_datasource_validation）。"""
    return CurrentUser()


def _createDto(
    dsId: int, code: str = "KPI_HOOK", status: str = "PUBLISHED"
) -> KpiCatalogCreate:
    """构造 create DTO（``dsId`` = 真实 DataSource id ⇒ createKpi 的存在性校验通过）。"""
    return KpiCatalogCreate(
        kpi_code=code,
        kpi_name="钩子测试",
        business_definition="d",
        formula="f",
        numerator="n",
        denominator="d",
        grain="g",
        unit="u",
        status=status,
        datasource_id=dsId,
    )


class _Recorder:
    """记录 4 个 best-effort 入口的调用（替代真 Milvus/Neo4j）。"""

    def __init__(self) -> None:
        self.graphSynced: list[int] = []
        self.vectorSynced: list[int] = []
        self.vectorDeleted: list[int] = []
        self.graphDeleted: list[int] = []

    def reset(self) -> None:
        self.graphSynced.clear()
        self.vectorSynced.clear()
        self.vectorDeleted.clear()
        self.graphDeleted.clear()


@pytest.fixture()
async def seedDataSource(dbSession: AsyncSession) -> DataSource:
    """最小可用的活跃 DataSource（createKpi 会校验存在 + is_active）。"""
    ds = DataSource(
        name="HOOK_TEST_DS",
        type="mysql",
        host="x",
        port=3306,
        database_name="x",
        username="x",
        password_encrypted="x",
        is_active=True,
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """stub 掉开关与 4 个 best-effort 函数（挂进 kpi_catalog_service 命名空间）。"""
    rec = _Recorder()

    async def _on(session):
        return True

    async def _graph(kpiId):
        rec.graphSynced.append(kpiId)

    async def _vector(kpiId):
        rec.vectorSynced.append(kpiId)

    async def _delVector(kpiId):
        rec.vectorDeleted.append(kpiId)

    async def _delGraph(kpiId):
        rec.graphDeleted.append(kpiId)

    monkeypatch.setattr(svc_module, "isKpiSyncEnabled", _on)
    monkeypatch.setattr(svc_module.kpi_sync, "syncKpiGraphBestEffort", _graph)
    monkeypatch.setattr(svc_module.kpi_sync, "syncKpiVectorBestEffort", _vector)
    monkeypatch.setattr(svc_module.kpi_sync, "deleteKpiVectorBestEffort", _delVector)
    monkeypatch.setattr(svc_module.kpi_sync, "deleteKpiGraphBestEffort", _delGraph)
    return rec


async def test_create_published_syncs_graph_and_vector(
    dbSession: AsyncSession, seedDataSource: DataSource, recorder: _Recorder
) -> None:
    dto = _createDto(seedDataSource.id, status="PUBLISHED")
    kpi = await KpiCatalogService().createKpi(dbSession, dto, _actor())
    assert recorder.graphSynced == [kpi.id]
    assert recorder.vectorSynced == [kpi.id]
    assert recorder.vectorDeleted == []
    assert recorder.graphDeleted == []


async def test_create_draft_syncs_graph_only(
    dbSession: AsyncSession, seedDataSource: DataSource, recorder: _Recorder
) -> None:
    """D3：向量只索引 PUBLISHED；DRAFT 仍建图节点（全状态）。"""
    dto = _createDto(seedDataSource.id, status="DRAFT")
    kpi = await KpiCatalogService().createKpi(dbSession, dto, _actor())
    assert recorder.graphSynced == [kpi.id]
    assert recorder.vectorSynced == []
    assert recorder.vectorDeleted == []


async def test_update_publish_draft_syncs_vector(
    dbSession: AsyncSession, seedDataSource: DataSource, recorder: _Recorder
) -> None:
    service = KpiCatalogService()
    dto = _createDto(seedDataSource.id, status="DRAFT")
    kpi = await service.createKpi(dbSession, dto, _actor())
    recorder.reset()
    await service.updateKpi(
        dbSession, kpi.id, KpiCatalogUpdate(status="PUBLISHED"), _actor()
    )
    assert recorder.vectorSynced == [kpi.id]
    assert recorder.graphSynced == [kpi.id]
    assert recorder.vectorDeleted == []


async def test_update_unpublish_deletes_vector_keeps_graph(
    dbSession: AsyncSession, seedDataSource: DataSource, recorder: _Recorder
) -> None:
    """D3：PUBLISHED → DEPRECATED ⇒ 删向量、**图节点保留**（status 属性更新）。

    P5 状态矩阵后 PUBLISHED → DRAFT 非法；退市路径经 DEPRECATED 仍可删向量
    （``kpi_catalog_service._syncKpiVectorAndGraph``: status != PUBLISHED 且
    statusBefore == PUBLISHED ⇒ delete）。
    """
    service = KpiCatalogService()
    dto = _createDto(seedDataSource.id, status="PUBLISHED")
    kpi = await service.createKpi(dbSession, dto, _actor())
    recorder.reset()
    await service.updateKpi(
        dbSession, kpi.id, KpiCatalogUpdate(status="DEPRECATED"), _actor()
    )
    assert recorder.vectorDeleted == [kpi.id]
    assert recorder.vectorSynced == []
    assert recorder.graphSynced == [kpi.id]  # 图节点保留，status 更新
    assert recorder.graphDeleted == []


async def test_update_other_field_keeps_published_vector(
    dbSession: AsyncSession, seedDataSource: DataSource, recorder: _Recorder
) -> None:
    """改非 status 字段（仍是 PUBLISHED）⇒ 重新同步向量，不删向量。"""
    service = KpiCatalogService()
    dto = _createDto(seedDataSource.id, status="PUBLISHED")
    kpi = await service.createKpi(dbSession, dto, _actor())
    recorder.reset()
    await service.updateKpi(
        dbSession, kpi.id, KpiCatalogUpdate(kpi_name="改名"), _actor()
    )
    assert recorder.vectorSynced == [kpi.id]
    assert recorder.vectorDeleted == []


async def test_delete_deletes_vector_and_graph(
    dbSession: AsyncSession, seedDataSource: DataSource, recorder: _Recorder
) -> None:
    service = KpiCatalogService()
    dto = _createDto(seedDataSource.id, status="PUBLISHED")
    kpi = await service.createKpi(dbSession, dto, _actor())
    recorder.reset()
    await service.deleteKpi(dbSession, kpi.id, _actor())
    assert recorder.vectorDeleted == [kpi.id]
    assert recorder.graphDeleted == [kpi.id]
    assert recorder.vectorSynced == []
    assert recorder.graphSynced == []


async def test_switch_off_disables_hooks(
    dbSession: AsyncSession,
    seedDataSource: DataSource,
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env/灰度开关关闭 ⇒ 一个钩子都不触发。"""

    async def _off(session):
        return False

    # monkeypatch 叠加：覆盖 recorder fixture 里的 _on，pytest 自动还原。
    monkeypatch.setattr(svc_module, "isKpiSyncEnabled", _off)
    dto = _createDto(seedDataSource.id, status="PUBLISHED")
    await KpiCatalogService().createKpi(dbSession, dto, _actor())
    assert recorder.graphSynced == []
    assert recorder.vectorSynced == []
    assert recorder.vectorDeleted == []
    assert recorder.graphDeleted == []


async def test_test_env_disables_kpi_sync_hook_by_default() -> None:
    """守卫：测试环境必须强制关掉 KPI 同步钩子。

    钩子的后台 best-effort 任务用 **app 侧配置**自开 session + 连 Neo4j
    （``settings.neo4jUri`` 默认 = 生产 7687）+ 写 Milvus ⇒ 不关掉就可能污染
    生产图/向量库。app/tests/conftest.py 统一置 ``KPI_SYNC_ENABLED=false``；
    本用例防止有人把那一行删掉。

    （实测证据：去掉该行后，一次 createKpi 的后台任务会去连 app 侧默认的
    DATABASE_URL 5432 —— 失败后才轮不到 Neo4j。）
    """
    assert os.environ.get("KPI_SYNC_ENABLED") == "false"


async def test_hook_failure_does_not_break_create(
    dbSession: AsyncSession,
    seedDataSource: DataSource,
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4：钩子抛异常**不得**影响主流程。

    - 钩子入口 ``syncKpiGraphBestEffort`` 抛异常；
    - 断言 1：``createKpi`` **正常返回**（不抛）；
    - 断言 2：**业务行确实落库**（真 PG 里查得到该 KPI）；
    - 断言 3：钩子确实被调用过（调用计数 = 1）。

    另断言「后续入口被短路」：钩子的 try/except 包住的是**整个钩子体** ⇒
    第一个入口抛异常后，后面的向量同步**不会**再被调用（否则失败会继续传播）。
    """
    calls: list[int] = []

    async def _boom(kpiId):
        calls.append(kpiId)
        raise RuntimeError("boom")

    # 覆盖 recorder fixture 里那个「正常版」的图同步入口（monkeypatch 自动还原）
    monkeypatch.setattr(svc_module.kpi_sync, "syncKpiGraphBestEffort", _boom)

    dto = _createDto(seedDataSource.id, status="PUBLISHED")
    kpi = await KpiCatalogService().createKpi(dbSession, dto, _actor())

    # 断言 3：钩子确实被调用过
    assert calls == [kpi.id]
    # 钩子体整体被 try/except 包住 ⇒ 第一个入口抛异常后，后续入口短路
    assert recorder.vectorSynced == []
    assert recorder.vectorDeleted == []
    assert recorder.graphDeleted == []
    # 断言 2：业务行确实落库（真 PG）
    row = (
        await dbSession.execute(
            select(KpiCatalog).where(KpiCatalog.id == kpi.id)
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.kpi_code == dto.kpi_code
    assert row.status == "PUBLISHED"
