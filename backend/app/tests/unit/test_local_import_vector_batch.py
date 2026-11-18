"""导入向导的向量同步纪律（2026-11-17 flush 配额事故的扇出源头）。

事故根因：导入向导对**类**抑制了逐条向量同步（``sync_embedding=False`` ⇒ 循环
结束后整批补齐，单次 flush），但对**属性**没有 ⇒ 每个属性各起一个后台同步任务
⇒ N 个任务并发 flush 同一集合 ⇒ 撞 Milvus 的集合级 flush 配额（0.1 qps ⇒
每 10 秒 1 次）⇒ pymilvus 重试耗尽 ⇒ 写路径「先删后插」⇒ **旧向量已删、
新向量没写回** ⇒ 属性向量彻底消失（语义召回失效）。

本文件钉死两件事：

1. 导入路径建属性时**抑制**逐条同步（传 ``sync_embedding=False``）；
2. 导入结束（哪怕只建了属性、没建类）都会触发**整批**补齐。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.schemas import ImportExecuteRequest, ProposedClass, ProposedProperty
from app.services.local_import_service import LocalImportService


class _FakeOntologyService:
    """记录导入向导对本体服务的调用（不碰 DB / Milvus / Neo4j）。"""

    def __init__(self) -> None:
        self.classSyncFlags: list[bool] = []
        self.propertySyncFlags: list[bool] = []
        self.batchSyncCount = 0

    async def createClass(
        self,
        session,
        dto,
        *,
        actor,
        actor_departments=None,
        **kwargs,
    ):
        self.classSyncFlags.append(bool(kwargs.get("sync_embedding", True)))
        return SimpleNamespace(id=101)

    async def createProperty(
        self,
        session,
        dto,
        *,
        actor,
        actor_departments=None,
        **kwargs,
    ):
        self.propertySyncFlags.append(bool(kwargs.get("sync_embedding", True)))
        return SimpleNamespace(id=201)

    async def listClasses(self, session):
        return []

    def syncMissingClassEmbeddingsBestEffort(self) -> None:
        self.batchSyncCount += 1


class _FakeSession:
    """最小会话替身：只回一个数据源行 + 支持 rollback。"""

    async def get(self, model, pk):
        return SimpleNamespace(id=pk)

    async def rollback(self) -> None:
        return None


def _proposedClass() -> ProposedClass:
    return ProposedClass(
        source_table="PORDER",
        class_name="采购订单",
        properties=[
            ProposedProperty(
                source_column="PTHNUM_0",
                property_name="订单号",
                data_type="varchar",
            ),
            ProposedProperty(
                source_column="QTYUOM_0",
                property_name="数量单位",
                data_type="varchar",
            ),
        ],
    )


def _request(*, syncEmbeddings: bool = True) -> ImportExecuteRequest:
    return ImportExecuteRequest(
        confirmed_classes=[_proposedClass()],
        confirmed_joins=[],
        conflict_resolutions=[],
        sync_embeddings=syncEmbeddings,
    )


def _actor():
    return SimpleNamespace(userId="tester", departments=())


@pytest.mark.asyncio
async def test_import_suppresses_per_property_embedding_sync() -> None:
    """导入路径建属性时**必须**抑制逐条后台同步（否则 N 个任务并发 flush）。"""
    fake = _FakeOntologyService()
    service = LocalImportService(ontology_service=fake)

    await service.execute_import(
        _FakeSession(), 1, _request(), created_by="tester", actor=_actor()
    )

    assert fake.propertySyncFlags == [False, False]


@pytest.mark.asyncio
async def test_import_triggers_batch_embedding_sync() -> None:
    """导入结束必须触发**整批**补齐（单次 flush），而不是靠逐条任务。"""
    fake = _FakeOntologyService()
    service = LocalImportService(ontology_service=fake)

    await service.execute_import(
        _FakeSession(), 1, _request(), created_by="tester", actor=_actor()
    )

    assert fake.batchSyncCount == 1


@pytest.mark.asyncio
async def test_import_without_sync_embeddings_does_not_batch_sync() -> None:
    """请求显式关掉向量同步 ⇒ 不批量补齐（尊重调用方意图）。"""
    fake = _FakeOntologyService()
    service = LocalImportService(ontology_service=fake)

    await service.execute_import(
        _FakeSession(),
        1,
        _request(syncEmbeddings=False),
        created_by="tester",
        actor=_actor(),
    )

    assert fake.batchSyncCount == 0
