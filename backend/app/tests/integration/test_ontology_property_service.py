"""本体属性 business_aliases/description 字段（2-2）CRUD 持久化测试。

覆盖：创建持久化、显式 None 清空（exclude_unset 语义）、空列表写入。
边界校验（纯 pydantic）留在 unit/test_ontology_property.py。

【迁移：真实 PG】由 unit/ 迁至 integration/（第三批），dbSession 走 integration/conftest.py
的真实 PostgreSQL + 每测试 TRUNCATE 隔离（Harness/rules/测试规范.md）；Neo4j 为外部依赖
仍 mock（best-effort），embedding 用 None 桩，其余数据层全真实。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from sqlalchemy.exc import MissingGreenlet

import app.infrastructure.neo4j_client as neo4j
from app.dependencies import CurrentUser
from app.domain.schemas import (
    OntologyClassCreate,
    OntologyMetricCreate,
    OntologyPropertyCreate,
    OntologyPropertyUpdate,
)
from app.services.acl_service import ADMIN_ROLE
from app.services.ontology_service import OntologyService

# Phase 4.5 ACL 扩展后 createClass 要求 actor。本测试不验 ACL，用 admin 绕过。
_ADMIN = CurrentUser(userId="t-admin", roles=(ADMIN_ROLE,), departments=())


@pytest.fixture()
def _neutralizeNeo4j(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neo4j 同步为 best-effort：mock 掉写入函数，避免依赖外部服务。"""
    monkeypatch.setattr(neo4j, "upsertClassNode", lambda **kw: None)
    monkeypatch.setattr(neo4j, "upsertPropertyNode", lambda **kw: None)
    monkeypatch.setattr(neo4j, "reconcileClassSubclassOf", lambda *a, **kw: None)
    monkeypatch.setattr(neo4j, "linkClassHasProperty", lambda *a, **kw: None)
    monkeypatch.setattr(neo4j, "linkPropertyReferences", lambda *a, **kw: None)
    monkeypatch.setattr(neo4j, "deleteNode", lambda *a, **kw: None)


@pytest.fixture()
def service() -> OntologyService:
    return OntologyService(embeddingService=None)


async def _createProp(
    dbSession,
    service: OntologyService,
    *,
    syncClassEmbedding: bool = True,
    **extra,
) -> object:
    cls = await service.createClass(
        dbSession,
        OntologyClassCreate(class_name="PRECEIPT", source_table="ZJTH.PRECEIPT"),
        actor=_ADMIN.userId,
        actor_departments=",".join(_ADMIN.departments) if _ADMIN.departments else None,
        sync_embedding=syncClassEmbedding,
    )
    return await service.createProperty(
        dbSession,
        OntologyPropertyCreate(
            class_id=cls.id,
            property_name="AMT_0",
            data_type="DECIMAL",
            source_column="AMT_0",
            **extra,
        ),
        actor=_ADMIN.userId,
        actor_departments=",".join(_ADMIN.departments) if _ADMIN.departments else None,
    )


class TestCreatePersists:
    async def test_create_persists_aliases_and_description(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """创建属性时 business_aliases/description 落库，重读一致。"""
        prop = await _createProp(
            dbSession, service,
            property_alias="金额",
            business_aliases=["营业额", "收入"],
            description="订单实收金额",
        )
        reloaded = await service.getProperty(dbSession, prop.id)
        assert reloaded.property_alias == "金额"
        assert reloaded.business_aliases == ["营业额", "收入"]
        assert reloaded.description == "订单实收金额"


class TestUpdateClears:
    async def test_update_explicit_none_clears_fields(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """显式传 None 清空别名/描述，且不影响其他字段（exclude_unset 语义回归点）。"""
        prop = await _createProp(dbSession, service, business_aliases=["营业额"], description="金额")
        await service.updateProperty(
            dbSession, prop.id, OntologyPropertyUpdate(business_aliases=None, description=None),
            actor=_ADMIN.userId,
            actor_departments=",".join(_ADMIN.departments) if _ADMIN.departments else None,
        )
        reloaded = await service.getProperty(dbSession, prop.id)
        assert reloaded.business_aliases is None
        assert reloaded.description is None
        assert reloaded.property_name == "AMT_0"
        assert reloaded.source_column == "AMT_0"

    async def test_update_empty_list_sets_empty(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """显式传空列表写入 []；渲染侧按 falsy 省略，不产生装饰文本。"""
        prop = await _createProp(dbSession, service, business_aliases=["营业额"])
        await service.updateProperty(
            dbSession, prop.id, OntologyPropertyUpdate(business_aliases=[]),
            actor=_ADMIN.userId,
            actor_departments=",".join(_ADMIN.departments) if _ADMIN.departments else None,
        )
        reloaded = await service.getProperty(dbSession, prop.id)
        assert reloaded.business_aliases == []

class _FakeEmbedding:
    async def generateEmbedding(self, text: str) -> list[float]:
        return [float(len(text))] * 4


class _FakeMilvus:
    def __init__(self) -> None:
        self.deleted: list[tuple[int, str]] = []
        self.inserted: list[dict] = []
        # 跨 delete/insert 的**有序**操作流水（只记 ontology_id）——竞态用例靠
        # 「该实体的最后一个操作是 delete 还是 insert」判定是否复活。
        self.ops: list[tuple[str, int]] = []

    def deleteByOntologyId(self, ontologyId: int, type: str) -> None:
        self.deleted.append((ontologyId, type))
        self.ops.append(("delete", ontologyId))

    def deleteByOntologyIdDual(self, ontologyId: int, type: str) -> None:
        """71 红 A1 流 RC-β 修（fix/ontology-milvus-dim-mismatch）：prod 在
        ``milvus_dual_write.py:182`` 引入 dual-write 双删；ontology_service.py
        的 embedding 同步路径已切到 ``deleteByOntologyIdDual``。原 ``_FakeMilvus``
        缺此方法 ⇒ AttributeError 串到 embedding 同步失败 → 测试 assert
        ``fakeMilvus.deleted`` 为空失败。
        修法：加同形方法（与 ``deleteByOntologyId`` 同样记入 ``self.deleted``）。
        """
        self.deleted.append((ontologyId, type))
        self.ops.append(("delete", ontologyId))

    def insertEmbeddingsDual(self, records: list[dict]) -> None:
        """71 红 A1 流 RC-γ 修（fix/ontology-milvus-dim-mismatch）：属性同步路径
        ``_syncPropertyEmbeddingNow`` → ``syncEmbedding`` → ``insertEmbeddingsDual``
        （ontology_service.py:1529）。原 ``_FakeMilvus`` 只暴露 ``insertEmbeddings``
        （旧名），prod 不再调 ⇒ ``self.inserted`` 永远空 ⇒ 测试 assert 假绿失败。
        修法：加同形方法。
        """
        self.inserted.extend(records)
        self.ops.extend(("insert", r["ontology_id"]) for r in records)

    def insertEmbeddings(self, records: list[dict]) -> None:
        self.inserted.extend(records)
        self.ops.extend(("insert", r["ontology_id"]) for r in records)


class TestUpdateResyncsPropertyEmbedding:
    """修改 description/business_aliases 后必须重刷属性向量（description 进向量文本，
    不重刷则语义检索永远拿旧含义——2026-09-18 用户报障「没办法生成向量信息」）。"""

    async def test_update_description_resyncs_property_vector(
        self, _neutralizeNeo4j, dbSession, monkeypatch
    ) -> None:
        import asyncio

        import app.services.ontology_service as mod

        fakeMilvus = _FakeMilvus()
        monkeypatch.setattr(mod, "milvus", fakeMilvus)
        service = OntologyService(embeddingService=_FakeEmbedding())  # type: ignore[arg-type]

        prop = await _createProp(dbSession, service)
        await service.updateProperty(
            dbSession,
            prop.id,
            OntologyPropertyUpdate(description="采购单含税金额"),
            actor=_ADMIN.userId,
        )

        # 等待 best-effort 后台同步任务结束
        await asyncio.gather(*mod._PENDING_SYNC_TASKS)

        assert (prop.id, "property") in fakeMilvus.deleted
        propertyRecords = [r for r in fakeMilvus.inserted if r["type"] == "property"]
        # createProperty 也触发一次后台同步（create+update 流下至少 2 条 property 记录）
        # ⇒ 只断言 ontology_id 正确 + **最终**向量带新描述。
        # 为什么只看最后一条：create 的后台任务在 update 提交**之前**读到旧行时，
        # 写出的就是旧描述（合法的中间态）。真正要保证的是**最终**向量是新的——
        # 同步路径按实体加锁串行化 + 锁内重读 ⇒ 最后写入的必是最新已提交状态
        # （见 ``_vectorSyncLock`` 与 ``TestBackgroundSyncWritesLatestState``）。
        assert len(propertyRecords) >= 1
        for rec in propertyRecords:
            assert rec["ontology_id"] == prop.id
        finalRecord = propertyRecords[-1]
        assert "采购单含税金额" in (finalRecord.get("description") or "")


class _GateEmbedding:
    """可控闸门的 embedding 桩：后台任务停在 generateEmbedding 里，由测试决定何时放行。

    用于**确定性**制造「请求先 rollback、后台任务后恢复」的次序——真实 embedding
    是网络调用，天然比本地 PG 事务慢；用闸门代替 sleep 免掉时序抖动。
    """

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def generateEmbedding(self, text: str) -> list[float]:
        await self._gate.wait()
        return [float(len(text))] * 4


class TestBackgroundSyncSurvivesRequestRollback:
    """请求失败（会话 rollback）后，后台向量同步任务不得因 MissingGreenlet 死掉。

    生产事故（2026-11-16）：`ontology_service.py:2392` 抛
    ``sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called``。

    根因链：
    1. 后台 asyncio 任务持有**请求级 ORM 实例**（`OntologyProperty`）；
    2. 请求失败 ⇒ `getDb` 走 `await session.rollback()`（database.py:84）⇒
       SQLAlchemy `_restore_snapshot` 把 identity map 里**所有** state 标记 expired
       （session.py:1126-1128），实体属性从 `__dict__` 清空；
    3. 后台任务恢复后访问 `entity.id` ⇒ 过期属性触发**同步懒加载** ⇒
       async 上下文里做 IO（无 `greenlet_spawn`）⇒ MissingGreenlet；
    4. `except` 分支再访问一次同样的属性 ⇒ 二次 MissingGreenlet ⇒
       原始异常被 "During handling of the above exception" 盖掉。

    修复口径：后台任务**自开会话重读实体**，永不触碰请求会话的 ORM 状态
    （与 ``kpi_vector_graph_sync`` 的 KPI 向量同步同款）。
    """

    async def test_property_sync_task_survives_session_rollback(
        self, _neutralizeNeo4j, dbSession, monkeypatch
    ) -> None:
        import app.services.ontology_service as mod

        fakeMilvus = _FakeMilvus()
        monkeypatch.setattr(mod, "milvus", fakeMilvus)
        gate = asyncio.Event()
        service = OntologyService(embeddingService=_GateEmbedding(gate))  # type: ignore[arg-type]

        before = set(mod._PENDING_SYNC_TASKS)
        prop = await _createProp(dbSession, service, syncClassEmbedding=False)
        propId = prop.id  # ⚠️ rollback 后 prop 的属性会 expired ⇒ 先取出来
        tasks = set(mod._PENDING_SYNC_TASKS) - before
        assert tasks, "createProperty 应登记一个后台向量同步任务"

        # 模拟请求失败：getDb 的 `await session.rollback()`（database.py:84）
        # ⇒ identity map 全量 expire，实体属性变 expired（实体仍挂在未关闭的会话上）
        await dbSession.rollback()

        # 前置条件断言：rollback 必须真的把实体 expire 掉，否则本用例会**假绿**。
        # `Session.rollback()` 在「无进行中事务」时是文档化的 pass-through
        # （sqlalchemy/orm/session.py:1993-1996）；此处能 expire 只是因为会话恰好
        # 有一个 autobegin 事务（当前由 IdMappingService.register 末尾的
        # session.refresh 保证）。若那条 refresh 消失，rollback 变 no-op、实体不
        # expire，本用例就会在**有 bug 的实现上通过**——所以这里必须自己校验。
        with pytest.raises(MissingGreenlet):
            _ = prop.id

        # 放行后台任务：它此刻恢复，不得触碰请求会话的 ORM 状态
        # （任务自开会话重读 ⇒ 与请求事务彻底解耦）
        gate.set()
        # 不加 return_exceptions ⇒ 任务内异常会原样抛出（本用例就是要它红）
        await asyncio.gather(*tasks)

        propertyRecords = [r for r in fakeMilvus.inserted if r["type"] == "property"]
        assert len(propertyRecords) == 1, "rollback 后后台向量同步仍须落 Milvus"
        assert propertyRecords[0]["ontology_id"] == propId


class TestBackgroundSyncSkipsMissingEntities:
    """后台向量同步在目标实体已消失时必须**跳过**，不得抛异常、不得写回脏向量。

    覆盖 2026-11-16 事故修复新增的三条分支：
      - 类已软删除（墓碑 ``valid_to``）⇒ 跳过 —— ``deleteClass`` 已删过向量，
        此时写回就是「墓碑复活成向量孤儿」。
      - 属性已硬删除 ⇒ ``NotFoundError`` ⇒ 跳过。
      - 指标已硬删除 ⇒ ``NotFoundError`` ⇒ 跳过。

    为什么直接调私有方法：这些分支的触发条件是「实体在后台任务**读取之前**就没了」，
    靠公开入口（create/update）只能靠时序碰运气（读在删之前就抓不到分支），
    而时序用例会变成间歇性红。直接调用后台入口 = 确定性覆盖。
    """

    async def test_class_sync_skips_tombstoned_class(
        self, _neutralizeNeo4j, dbSession, monkeypatch
    ) -> None:
        import app.services.ontology_service as mod

        fakeMilvus = _FakeMilvus()
        monkeypatch.setattr(mod, "milvus", fakeMilvus)
        service = OntologyService(embeddingService=_FakeEmbedding())  # type: ignore[arg-type]

        cls = await service.createClass(
            dbSession,
            OntologyClassCreate(class_name="PRECEIPT", source_table="ZJTH.PRECEIPT"),
            actor=_ADMIN.userId,
            actor_departments=",".join(_ADMIN.departments) if _ADMIN.departments else None,
            sync_embedding=False,
        )
        clsId = cls.id
        await service.deleteClass(dbSession, clsId, actor=_ADMIN)

        await service._syncClassEmbeddingNow(clsId)

        classRecords = [r for r in fakeMilvus.inserted if r["type"] == "class"]
        assert not classRecords, "墓碑类的向量不得被后台任务写回"

    async def test_class_sync_skips_physically_deleted_class(
        self, _neutralizeNeo4j, dbSession, monkeypatch
    ) -> None:
        """类行被**物理删除**时（``scripts/collapse_ontology_versions.py`` 会硬删
        重复版本行）⇒ 后台同步读到「类不存在」⇒ 跳过，不得抛异常。"""
        import app.services.ontology_service as mod

        fakeMilvus = _FakeMilvus()
        monkeypatch.setattr(mod, "milvus", fakeMilvus)
        service = OntologyService(embeddingService=_FakeEmbedding())  # type: ignore[arg-type]

        cls = await service.createClass(
            dbSession,
            OntologyClassCreate(class_name="PRECEIPT", source_table="ZJTH.PRECEIPT"),
            actor=_ADMIN.userId,
            actor_departments=",".join(_ADMIN.departments) if _ADMIN.departments else None,
            sync_embedding=False,
        )
        clsId = cls.id
        await dbSession.delete(cls)
        await dbSession.commit()

        await service._syncClassEmbeddingNow(clsId)

        classRecords = [r for r in fakeMilvus.inserted if r["type"] == "class"]
        assert not classRecords, "已物理删除的类不得被后台任务写回向量"

    async def test_property_sync_skips_deleted_property(
        self, _neutralizeNeo4j, dbSession, monkeypatch
    ) -> None:
        import app.services.ontology_service as mod

        fakeMilvus = _FakeMilvus()
        monkeypatch.setattr(mod, "milvus", fakeMilvus)
        service = OntologyService(embeddingService=_FakeEmbedding())  # type: ignore[arg-type]

        prop = await _createProp(dbSession, service, syncClassEmbedding=False)
        propId = prop.id
        await service.deleteProperty(dbSession, propId, actor=_ADMIN.userId)
        # 让 create 的后台任务先跑完（它可能写入 0 或 1 条，取决于时序）⇒ 只断言增量
        await asyncio.gather(*mod._PENDING_SYNC_TASKS, return_exceptions=True)
        before = len([r for r in fakeMilvus.inserted if r["type"] == "property"])

        await service._syncPropertyEmbeddingNow(propId)

        after = len([r for r in fakeMilvus.inserted if r["type"] == "property"])
        assert after == before, "已删除属性的向量不得被后台任务写回"

    async def test_metric_sync_skips_deleted_metric(
        self, _neutralizeNeo4j, dbSession, monkeypatch
    ) -> None:
        import app.services.ontology_service as mod

        fakeMilvus = _FakeMilvus()
        monkeypatch.setattr(mod, "milvus", fakeMilvus)
        service = OntologyService(embeddingService=_FakeEmbedding())  # type: ignore[arg-type]

        metric = await service.createMetric(
            dbSession,
            OntologyMetricCreate(
                metric_name="sales_amount",
                metric_alias="销售总额",
                formula="SUM(order_amount)",
                agg_function="SUM",
            ),
            actor=_ADMIN.userId,
            actor_departments=",".join(_ADMIN.departments) if _ADMIN.departments else None,
        )
        metricId = metric.id
        await service.deleteMetric(dbSession, metricId, actor=_ADMIN.userId)
        await asyncio.gather(*mod._PENDING_SYNC_TASKS, return_exceptions=True)
        before = len([r for r in fakeMilvus.inserted if r["type"] == "metric"])

        await service._syncMetricEmbeddingNow(metricId)

        after = len([r for r in fakeMilvus.inserted if r["type"] == "metric"])
        assert after == before, "已删除指标的向量不得被后台任务写回"


class _OrderedEmbedding:
    """按文本内容分闸门的 embedding 桩：旧描述 / 新描述各一个闸门 + 完成信号。

    用于**确定性**构造「读旧行的任务最后写」的竞争次序：
    - ``gates["new"]`` 先放行、``gates["old"]`` 后放行 ⇒ 无串行化时旧向量后写。
    - ``finished[...]`` 让测试能等到（或确认等不到）某个任务的写入完成。
    """

    def __init__(self) -> None:
        self.gates = {"old": asyncio.Event(), "new": asyncio.Event()}
        self.finished = {"old": asyncio.Event(), "new": asyncio.Event()}
        self.entered: list[str] = []
        self.completed: list[str] = []

    @staticmethod
    def _keyOf(text: str) -> str:
        return "new" if "新描述" in text else "old"

    async def generateEmbedding(self, text: str) -> list[float]:
        key = self._keyOf(text)
        self.entered.append(key)
        await self.gates[key].wait()
        self.completed.append(key)
        self.finished[key].set()
        return [float(len(text))] * 4


class TestBackgroundSyncWritesLatestState:
    """create / update 连着来时，**最终**向量必须是新描述。

    回归的是「后台任务各自读自己那一版行」引入的新风险（不是 MissingGreenlet
    那条）：create 的任务读旧的、update 的任务读新的，两者并发写 Milvus；
    若慢的那个（读旧的）后写，最终向量就是旧描述 ⇒ 语义检索永远拿旧含义
    （2026-09-18 报障的形状，且 ``syncMissingClassEmbeddings`` 只补缺失向量，
    永远修不回陈旧向量）。

    修复口径：同一实体的向量写入按 ``(type, id)`` 串行化，并在**锁内重读**
    实体 ⇒ 最后写入的一定是当时最新的已提交状态。
    """

    async def test_final_vector_carries_updated_description(
        self, _neutralizeNeo4j, dbSession, monkeypatch
    ) -> None:
        import app.services.ontology_service as mod

        fakeMilvus = _FakeMilvus()
        monkeypatch.setattr(mod, "milvus", fakeMilvus)
        embed = _OrderedEmbedding()
        service = OntologyService(embeddingService=embed)  # type: ignore[arg-type]

        before = set(mod._PENDING_SYNC_TASKS)
        prop = await _createProp(
            dbSession, service, syncClassEmbedding=False, description="旧描述"
        )
        assert set(mod._PENDING_SYNC_TASKS) - before, "createProperty 应登记后台向量任务"
        propId = prop.id
        # 等 create 的后台任务真正**进入** embedding（此刻它已读过旧行）
        for _ in range(400):
            if "old" in embed.entered:
                break
            await asyncio.sleep(0.005)
        assert "old" in embed.entered, "create 的后台任务应已按旧描述进入 embedding"

        await service.updateProperty(
            dbSession,
            propId,
            OntologyPropertyUpdate(description="新描述"),
            actor=_ADMIN.userId,
        )

        # 先放行「新」，再放行「旧」⇒ 没有串行化时旧向量会**后写**覆盖新向量
        embed.gates["new"].set()
        # 有串行化时 update 的任务还在等锁，不会完成 ⇒ 这里必然超时（用 suppress 吞掉）
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(embed.finished["new"].wait(), timeout=0.5)
        embed.gates["old"].set()
        await asyncio.gather(*mod._PENDING_SYNC_TASKS)

        propertyRecords = [r for r in fakeMilvus.inserted if r["type"] == "property"]
        assert propertyRecords, "至少有后台任务写入属性向量"
        assert propertyRecords[-1]["ontology_id"] == propId
        assert "新描述" in (propertyRecords[-1].get("description") or ""), (
            "最终向量必须是 update 后的描述（旧向量后写 = 回归）"
        )


class _ParkedEmbedding:
    """进入 embedding 即置信号、随后等待放行——确定性复刻「已读完存活行、停在
    embedding 调用里」的在途写入（真实 embedding 是 10-25s 的网络调用）。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def generateEmbedding(self, text: str) -> list[float]:
        self.entered.set()
        await self.gate.wait()
        return [float(len(text))] * 4


class TestDeleteDoesNotResurrectVector:
    """删除与在途后台同步的竞态：最终态必须是「已删」，不得把向量写回。

    生产形状：update 提交 → 后台任务已读到存活行、停在 embedding 调用
    → 用户删除该属性并提交（删除自身会删 Milvus 行）→ 后台任务恢复并
    ``syncEmbedding``（delete+insert）⇒ 已删实体的向量被**复活**，语义召回
    永久命中一个不存在的属性；``syncMissingClassEmbeddings`` 只补缺失、
    不删多余 ⇒ 无人修复。

    修复口径：删除路径与后台同步**共用同一把实体锁**
    （``_deleteMilvusVectorUnderLock``）⇒ 删除必然落在在途写入之后。
    """

    async def test_delete_waits_for_in_flight_sync_and_vector_stays_deleted(
        self, _neutralizeNeo4j, dbSession, monkeypatch
    ) -> None:
        import app.services.ontology_service as mod

        fakeMilvus = _FakeMilvus()
        monkeypatch.setattr(mod, "milvus", fakeMilvus)

        # 1) 用「快」embedding 建数据，并等 create 的后台任务落库
        prop = await _createProp(
            dbSession,
            OntologyService(embeddingService=_FakeEmbedding()),  # type: ignore[arg-type]
            syncClassEmbedding=False,
        )
        propId = prop.id
        await asyncio.gather(*mod._PENDING_SYNC_TASKS)

        # 2) 换成「停在 embedding」的桩，制造一次在途写入
        embed = _ParkedEmbedding()
        service = OntologyService(embeddingService=embed)  # type: ignore[arg-type]
        await service.updateProperty(
            dbSession,
            propId,
            OntologyPropertyUpdate(description="删除前的描述"),
            actor=_ADMIN.userId,
        )
        await asyncio.wait_for(embed.entered.wait(), timeout=5.0)

        # 3) 在途写入未结束时删除：修复后它必须在实体锁上排队
        fakeMilvus.ops.clear()  # 只观察删除阶段的操作顺序
        deleteTask = asyncio.create_task(
            service.deleteProperty(dbSession, propId, actor=_ADMIN.userId)
        )
        for _ in range(200):  # 最多等 2s 观察「删除是否越过在途写入」（未修复时会）
            if ("delete", propId) in fakeMilvus.ops:
                break
            await asyncio.sleep(0.01)
        assert ("delete", propId) not in fakeMilvus.ops, (
            "删除不得越过在途向量写入（否则随后恢复的写入会把向量复活）"
        )

        # 4) 放行在途写入 → 删除随后拿到锁并清掉向量
        embed.gate.set()
        await asyncio.wait_for(deleteTask, timeout=10.0)
        await asyncio.gather(*mod._PENDING_SYNC_TASKS)

        propOps = [op for op in fakeMilvus.ops if op[1] == propId]
        assert propOps, "删除阶段应有 Milvus 操作"
        assert propOps[-1] == ("delete", propId), (
            f"该实体的最后一个 Milvus 操作必须是 delete（当前 {propOps[-1]}）"
            " ⇒ 在途写入复活了已删实体的向量"
        )


class _FailingEmbedding:
    """必失败的 embedding 桩：验证向量同步的 best-effort 契约。"""

    async def generateEmbedding(self, text: str) -> list[float]:
        raise RuntimeError("embedding 服务不可用")


class TestBackgroundSyncFailureIsIsolated:
    """向量同步失败必须**只告警、不冒泡**（best-effort 契约）。

    事故修复后后台任务自带 try/except：embedding 挂了、Milvus 挂了、DB 读挂了，
    都不得把异常抛回请求链路，也不得让任务异常变成「never retrieved」噪声。
    """

    async def test_embedding_failure_is_swallowed_for_all_three_types(
        self, _neutralizeNeo4j, dbSession, monkeypatch
    ) -> None:
        import app.services.ontology_service as mod

        fakeMilvus = _FakeMilvus()
        monkeypatch.setattr(mod, "milvus", fakeMilvus)
        service = OntologyService(embeddingService=_FailingEmbedding())  # type: ignore[arg-type]

        before = set(mod._PENDING_SYNC_TASKS)
        prop = await _createProp(dbSession, service, syncClassEmbedding=False)
        propId = prop.id
        metric = await service.createMetric(
            dbSession,
            OntologyMetricCreate(
                metric_name="sales_amount",
                metric_alias="销售总额",
                formula="SUM(order_amount)",
                agg_function="SUM",
            ),
            actor=_ADMIN.userId,
            actor_departments=",".join(_ADMIN.departments) if _ADMIN.departments else None,
        )
        metricId = metric.id
        tasks = set(mod._PENDING_SYNC_TASKS) - before
        # 不加 return_exceptions ⇒ 任务内异常若冒泡，这里直接红
        await asyncio.gather(*tasks)

        # 三条「同步入口」各自失败也必须只告警
        await service._syncClassEmbeddingNow(prop.class_id)
        await service._syncPropertyEmbeddingNow(propId)
        await service._syncMetricEmbeddingNow(metricId)

        assert not fakeMilvus.inserted, "embedding 失败时不得写入向量"
