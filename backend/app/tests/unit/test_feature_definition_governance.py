"""FeatureDefinition audit + history 单元测试（Phase 4.5 outbox 模式）。

覆盖：createFeature / updateFeature / deleteFeature 通过 outbox.enqueue 写入
audit_outbox，worker 消费后写 audit_log + feature_definition_history。

与 KpiCatalogService 的 outbox 测试设计对齐：不再 mock AuditService /
HistoryService，直接验证 OutboxService.enqueue 调用参数。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.dependencies import CurrentUser
from app.domain.schemas import FeatureDefinitionCreate, FeatureDefinitionUpdate
from app.services.feature_definition_service import FeatureDefinitionService


class _FakeOutboxService:
    """outbox 入队桩（记录所有 enqueue 调用）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enqueue(
        self,
        session,
        *,
        event_type: str,
        entity_type: str,
        entity_id: int,
        actor: str,
        payload: dict,
        actor_departments=None,
    ) -> None:
        self.calls.append({
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor": actor,
            "payload": payload,
            "actor_departments": actor_departments,
        })


class _FakeFeatureDefinition:
    """仿冒的 FeatureDefinition ORM 实体。"""

    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)

    @property
    def __table__(self):
        """支持 _entityToDict 的 entity.__table__.columns.keys() 访问。"""
        class _Columns:
            def keys(self):
                return ["feature_name", "entity_type", "status", "owner", "id"]
        return type("T", (), {"columns": _Columns()})()


async def _make_mock_session():
    """返回一个正确配置的 mock session（async chain 正确连接）。"""
    session = AsyncMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


def _make_mock_result(scalar_return_value):
    """返回一个 mock execute result。

    用 MagicMock 而非 AsyncMock：session.execute() 已经是 async（被 await），
    其返回值的 .scalar_one_or_none() 是普通同步调用，不是 coroutine。
    """
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar_return_value
    return result


class TestFeatureOutboxEvents:
    """验证 createFeature / updateFeature / deleteFeature 调用 outbox.enqueue。"""

    @pytest.mark.asyncio
    async def test_createFeature_enqueues_event_type_feature_created(self) -> None:
        """createFeature → outbox.enqueue(event_type='feature_created')。"""
        fake_outbox = _FakeOutboxService()
        svc = FeatureDefinitionService(outbox=fake_outbox)
        session = await _make_mock_session()
        session.execute = AsyncMock(return_value=_make_mock_result(None))

        dto = FeatureDefinitionCreate(
            featureName="TEST_3M",
            featureAlias="测试特征",
            entityType="SUPPLIER",
            calculationLogic="SELECT 1",
            datasourceId=1,
        )
        actor = CurrentUser(userId="alice", departments=("采购部",))

        with patch.object(svc, "_assertDatasourceExists", new_callable=AsyncMock):
            try:
                await svc.createFeature(session, dto, actor)
            except Exception:
                pass  # entity.id may be None after rollback

        assert any(c["event_type"] == "feature_created" for c in fake_outbox.calls)

    @pytest.mark.asyncio
    async def test_createFeature_enqueues_payload_with_after(self) -> None:
        """createFeature → payload 含 after 快照。"""
        fake_outbox = _FakeOutboxService()
        svc = FeatureDefinitionService(outbox=fake_outbox)
        session = await _make_mock_session()
        session.execute = AsyncMock(return_value=_make_mock_result(None))

        dto = FeatureDefinitionCreate(
            featureName="TEST_3M",
            featureAlias="测试特征",
            entityType="SUPPLIER",
            calculationLogic="SELECT 1",
            datasourceId=1,
        )
        actor = CurrentUser(userId="alice", departments=("采购部",))

        with patch.object(svc, "_assertDatasourceExists", new_callable=AsyncMock):
            try:
                await svc.createFeature(session, dto, actor)
            except Exception:
                pass

        call = next(
            (c for c in fake_outbox.calls if c["event_type"] == "feature_created"), None
        )
        assert call is not None
        assert "after" in call["payload"]
        assert call["payload"]["after"]["feature_name"] == "TEST_3M"

    @pytest.mark.asyncio
    async def test_createFeature_carries_actor_info(self) -> None:
        """createFeature → actor=alice, actor_departments 传入 enqueue。"""
        fake_outbox = _FakeOutboxService()
        svc = FeatureDefinitionService(outbox=fake_outbox)
        session = await _make_mock_session()
        session.execute = AsyncMock(return_value=_make_mock_result(None))

        dto = FeatureDefinitionCreate(
            featureName="TEST_3M",
            featureAlias="测试特征",
            entityType="SUPPLIER",
            calculationLogic="SELECT 1",
            datasourceId=1,
        )
        actor = CurrentUser(userId="alice", departments=("采购部",))

        with patch.object(svc, "_assertDatasourceExists", new_callable=AsyncMock):
            try:
                await svc.createFeature(session, dto, actor)
            except Exception:
                pass

        call = next(
            (c for c in fake_outbox.calls if c["event_type"] == "feature_created"), None
        )
        assert call is not None
        assert call["actor"] == "alice"
        assert call["actor_departments"] == ("采购部",)

    @pytest.mark.asyncio
    async def test_updateFeature_enqueues_event_type_feature_updated(self) -> None:
        """updateFeature → outbox.enqueue(event_type='feature_updated')。"""
        fake_outbox = _FakeOutboxService()
        svc = FeatureDefinitionService(outbox=fake_outbox)
        session = await _make_mock_session()

        existing = _FakeFeatureDefinition(
            id=1,
            feature_name="OLD_NAME",
            entity_type="SUPPLIER",
            status="DRAFT",
            owner="采购部",
        )

        async def fake_get(*args, **kwargs):
            return existing

        with patch.object(svc, "getFeature", side_effect=fake_get):
            with patch.object(svc, "_assertDatasourceExists", new_callable=AsyncMock):
                dto = FeatureDefinitionUpdate(featureAlias="新别名")
                actor = CurrentUser(userId="alice", departments=("采购部",))
                try:
                    await svc.updateFeature(session, 1, dto, actor)
                except Exception:
                    pass

        assert any(c["event_type"] == "feature_updated" for c in fake_outbox.calls)

    @pytest.mark.asyncio
    async def test_updateFeature_enqueues_before_and_after(self) -> None:
        """updateFeature → payload 含 before + after。"""
        fake_outbox = _FakeOutboxService()
        svc = FeatureDefinitionService(outbox=fake_outbox)
        session = await _make_mock_session()

        existing = _FakeFeatureDefinition(
            id=1,
            feature_name="OLD_NAME",
            entity_type="SUPPLIER",
            status="DRAFT",
            owner="采购部",
        )

        async def fake_get(*args, **kwargs):
            return existing

        with patch.object(svc, "getFeature", side_effect=fake_get):
            with patch.object(svc, "_assertDatasourceExists", new_callable=AsyncMock):
                dto = FeatureDefinitionUpdate(featureAlias="新别名")
                actor = CurrentUser(userId="alice", departments=("采购部",))
                try:
                    await svc.updateFeature(session, 1, dto, actor)
                except Exception:
                    pass

        call = next(
            (c for c in fake_outbox.calls if c["event_type"] == "feature_updated"), None
        )
        assert call is not None
        assert "before" in call["payload"]
        assert "after" in call["payload"]
        assert call["payload"]["before"]["feature_name"] == "OLD_NAME"

    @pytest.mark.asyncio
    async def test_deleteFeature_enqueues_event_type_feature_deleted(self) -> None:
        """deleteFeature → outbox.enqueue(event_type='feature_deleted')。"""
        fake_outbox = _FakeOutboxService()
        svc = FeatureDefinitionService(outbox=fake_outbox)
        session = await _make_mock_session()

        existing = _FakeFeatureDefinition(
            id=1,
            feature_name="TO_DELETE",
            entity_type="SUPPLIER",
            status="DRAFT",
            owner="采购部",
        )

        async def fake_get(*args, **kwargs):
            return existing

        with patch.object(svc, "getFeature", side_effect=fake_get):
            with patch.object(svc, "_assertDatasourceExists", new_callable=AsyncMock):
                actor = CurrentUser(userId="alice", departments=("采购部",))
                try:
                    await svc.deleteFeature(session, 1, actor)
                except Exception:
                    pass

        assert any(c["event_type"] == "feature_deleted" for c in fake_outbox.calls)

    @pytest.mark.asyncio
    async def test_deleteFeature_enqueues_before_only(self) -> None:
        """deleteFeature → payload 只含 before，无 after。"""
        fake_outbox = _FakeOutboxService()
        svc = FeatureDefinitionService(outbox=fake_outbox)
        session = await _make_mock_session()

        existing = _FakeFeatureDefinition(
            id=1,
            feature_name="TO_DELETE",
            entity_type="SUPPLIER",
            status="DRAFT",
            owner="采购部",
        )

        async def fake_get(*args, **kwargs):
            return existing

        with patch.object(svc, "getFeature", side_effect=fake_get):
            with patch.object(svc, "_assertDatasourceExists", new_callable=AsyncMock):
                actor = CurrentUser(userId="alice", departments=("采购部",))
                try:
                    await svc.deleteFeature(session, 1, actor)
                except Exception:
                    pass

        call = next(
            (c for c in fake_outbox.calls if c["event_type"] == "feature_deleted"), None
        )
        assert call is not None
        assert "before" in call["payload"]
        assert call["payload"].get("after") is None

    @pytest.mark.asyncio
    async def test_multi_department_actor_carried_to_enqueue(self) -> None:
        """多部门 actor → actor_departments 传入 enqueue（tuple）。"""
        fake_outbox = _FakeOutboxService()
        svc = FeatureDefinitionService(outbox=fake_outbox)
        session = await _make_mock_session()

        existing = _FakeFeatureDefinition(
            id=1, feature_name="X", entity_type="SUPPLIER", status="DRAFT", owner="采购部"
        )

        async def fake_get(*args, **kwargs):
            return existing

        with patch.object(svc, "getFeature", side_effect=fake_get):
            with patch.object(svc, "_assertDatasourceExists", new_callable=AsyncMock):
                actor_multi = CurrentUser(
                    userId="bob", departments=("采购部", "财务部")
                )
                dto = FeatureDefinitionUpdate(featureAlias="Y")
                try:
                    await svc.updateFeature(session, 1, dto, actor_multi)
                except Exception:
                    pass

        call = next(
            (c for c in fake_outbox.calls if c["event_type"] == "feature_updated"), None
        )
        assert call is not None
        assert call["actor"] == "bob"
        assert call["actor_departments"] == ("采购部", "财务部")
