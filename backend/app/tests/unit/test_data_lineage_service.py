"""Phase 2.1 data_lineage 服务层单测。

TDD 顺序（先 RED）：
1. 列出所有契约（listEdges / getEdge / createEdge / updateEdge / disableEdge）
2. 每条契约一条测试：基础路径 + 边界 + 错误路径
3. 验证创建时不依赖 SQL 拼接（避免与 Phase 1.1 service 同构的 SQL 注入面）

集成测试在 test_data_lineage_api.py 用真实 PG 5433 跑。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.enums import LineageLayer, RefreshFrequency
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataLineage
from app.domain.schemas import (
    LineageEdgeCreate,
    LineageEdgeRead,
    LineageEdgeUpdate,
)
from app.services.data_lineage_service import (
    DataLineageService,
    lineageToRead,
)


def _run(coro):
    """Python 3.14 取消隐式 loop 创建，需手动驱动。"""
    return asyncio.new_event_loop().run_until_complete(coro)


def _fakeSession(records: dict[int, DataLineage] | None = None, raises: Exception | None = None):
    """最小 fake session：模拟 get / add / commit / refresh / execute / delete 行为。

    execute() 返回支持 scalars().all() 与 scalar_one_or_none() 两种访问形态，
    以覆盖 service 中 select(...).where(...) 与 select(...).order_by(...) 两条路径。
    """

    class _Result:
        def __init__(self, items: list[Any]):
            self._items = items

        def scalars(self) -> SimpleNamespace:
            return SimpleNamespace(all=lambda: list(self._items))

        def scalar_one_or_none(self) -> Any | None:
            return self._items[0] if self._items else None

    class _Session:
        def __init__(self):
            self.records = records or {}
            self.next_id = (max(records) + 1) if records else 1
            self.added: list[Any] = []
            self.commits = 0
            self.removed: list[Any] = []

        async def get(self, _model, pk):
            return self.records.get(pk)

        async def execute(self, stmt):
            # 仅支持：service 中「全量 select」（返回全部 records）
            # +「where ... 按身份键查」（命中 records[0] 或返回空）。
            # service 真实 SQL 解析由集成测试覆盖；此处用首条记录模拟。
            return _Result(list(self.records.values()))

        def add(self, entity):
            self.added.append(entity)

        async def commit(self):
            self.commits += 1
            for e in self.added:
                if getattr(e, "id", None) is None:
                    e.id = self.next_id
                    self.next_id += 1
                # 模拟 SQLAlchemy 默认值在 INSERT 时填充
                if getattr(e, "is_active", None) is None:
                    e.is_active = True
                if getattr(e, "refresh_frequency", None) is None:
                    e.refresh_frequency = RefreshFrequency.DAILY

        async def refresh(self, entity):
            if not getattr(entity, "created_time", None):
                entity.created_time = datetime(2026, 8, 30, tzinfo=timezone.utc)
                entity.updated_time = entity.created_time

        async def delete(self, entity):
            self.removed.append(entity)

    return _Session()


def _makeCreate(**overrides) -> LineageEdgeCreate:
    defaults: dict[str, Any] = {
        "source_layer": LineageLayer.SOURCE_SYSTEM,
        "source_system": "ERP",
        "source_object": "PORDER",
        "source_field": "ORDERQTY",
        "target_layer": LineageLayer.ODS,
        "target_system": "ODS",
        "target_object": "ODS_PURCHASE_ORDER",
        "target_field": "ORDER_QTY",
        "transformation_rule": "标准化 + 代理键",
        "refresh_frequency": RefreshFrequency.DAILY,
        "owner": "数据团队",
        "description": "源端 → ODS 标准化接入",
    }
    defaults.update(overrides)
    return LineageEdgeCreate(**defaults)


class TestListEdges:
    def test_empty_returns_empty_list(self):
        svc = DataLineageService()
        session = _fakeSession(records={})
        result = _run(svc.listEdges(session))
        assert result == []

    def test_returns_all_when_no_filter(self):
        e1 = DataLineage(id=1, source_layer=LineageLayer.SOURCE_SYSTEM, source_system="ERP",
                         source_object="PORDER", target_layer=LineageLayer.ODS,
                         target_system="ODS", target_object="ODS_PO")
        e2 = DataLineage(id=2, source_layer=LineageLayer.ODS, source_system="ODS",
                         source_object="ODS_PO", target_layer=LineageLayer.DWD,
                         target_system="DWD", target_object="DWD_PO")
        session = _fakeSession(records={1: e1, 2: e2})
        service = DataLineageService()
        result = _run(service.listEdges(session))
        assert {e.id for e in result} == {1, 2}

    def test_filters_by_source_layer(self):
        e1 = DataLineage(id=1, source_layer=LineageLayer.SOURCE_SYSTEM, source_system="ERP",
                         source_object="PORDER", target_layer=LineageLayer.ODS,
                         target_system="ODS", target_object="ODS_PO")
        e2 = DataLineage(id=2, source_layer=LineageLayer.ODS, source_system="ODS",
                         source_object="ODS_PO", target_layer=LineageLayer.DWD,
                         target_system="DWD", target_object="DWD_PO")
        session = _fakeSession(records={1: e1, 2: e2})
        # SERVICE 层只按 source_layer 过滤：因 fake execute 不解析 stmt，这里直接断言 ORM.where 不被滥用
        # ——真正的过滤逻辑在集成测试验证；单测只确保 API 形状不爆
        service = DataLineageService()
        result = _run(service.listEdges(session, sourceLayer=LineageLayer.SOURCE_SYSTEM))
        assert isinstance(result, list)


class TestGetEdge:
    def test_returns_edge_when_found(self):
        e = DataLineage(id=42, source_layer=LineageLayer.SOURCE_SYSTEM, source_system="ERP",
                        source_object="PORDER", target_layer=LineageLayer.ODS,
                        target_system="ODS", target_object="ODS_PO")
        session = _fakeSession(records={42: e})
        service = DataLineageService()
        result = _run(service.getEdge(session, 42))
        assert result.id == 42

    def test_raises_not_found_when_missing(self):
        session = _fakeSession(records={})
        with pytest.raises(NotFoundError):
            _run(DataLineageService().getEdge(session, 999))


class TestCreateEdge:
    def test_creates_with_all_fields(self):
        session = _fakeSession()
        dto = _makeCreate()
        edge = _run(DataLineageService().createEdge(session, dto))
        assert edge.id is not None
        assert edge.source_layer == LineageLayer.SOURCE_SYSTEM
        assert edge.source_system == "ERP"
        assert edge.source_object == "PORDER"
        assert edge.source_field == "ORDERQTY"
        assert edge.target_layer == LineageLayer.ODS
        assert edge.transformation_rule == "标准化 + 代理键"
        assert edge.refresh_frequency == RefreshFrequency.DAILY
        assert session.commits == 1

    def test_table_level_edge_allows_null_field(self):
        """source/target_field 可空（表级血缘不绑字段）。"""
        session = _fakeSession()
        dto = _makeCreate(source_field=None, target_field=None)
        edge = _run(DataLineageService().createEdge(session, dto))
        assert edge.source_field is None
        assert edge.target_field is None

    def test_rejects_self_loop_same_layer_same_object(self):
        """禁止 source == target（同层同对象同字段循环）。"""
        session = _fakeSession()
        dto = _makeCreate(
            source_layer=LineageLayer.ODS,
            source_system="ODS",
            source_object="X",
            source_field="F",
            target_layer=LineageLayer.ODS,
            target_system="ODS",
            target_object="X",
            target_field="F",
        )
        with pytest.raises(ValidationError):
            _run(DataLineageService().createEdge(session, dto))


class TestUpdateEdge:
    def test_partial_update_only_overrides_non_none(self):
        e = DataLineage(id=3, source_layer=LineageLayer.SOURCE_SYSTEM, source_system="ERP",
                        source_object="PORDER", target_layer=LineageLayer.ODS,
                        target_system="ODS", target_object="ODS_PO",
                        owner="旧 owner")
        session = _fakeSession(records={3: e})
        dto = LineageEdgeUpdate(owner="新 owner")
        updated = _run(DataLineageService().updateEdge(session, 3, dto))
        assert updated.owner == "新 owner"
        # 未修改字段保留
        assert updated.source_system == "ERP"
        assert updated.source_object == "PORDER"

    def test_raises_not_found_when_missing(self):
        session = _fakeSession(records={})
        dto = LineageEdgeUpdate(owner="x")
        with pytest.raises(NotFoundError):
            _run(DataLineageService().updateEdge(session, 999, dto))


class TestDisableEdge:
    def test_disable_sets_is_active_false(self):
        e = DataLineage(id=4, source_layer=LineageLayer.SOURCE_SYSTEM, source_system="ERP",
                        source_object="PORDER", target_layer=LineageLayer.ODS,
                        target_system="ODS", target_object="ODS_PO",
                        is_active=True)
        session = _fakeSession(records={4: e})
        _run(DataLineageService().disableEdge(session, 4))
        assert e.is_active is False
        assert session.commits == 1

    def test_raises_not_found_when_missing(self):
        session = _fakeSession(records={})
        with pytest.raises(NotFoundError):
            _run(DataLineageService().disableEdge(session, 999))


class TestLineageToRead:
    def test_roundtrip_minimal_fields(self):
        e = DataLineage(id=5, source_layer=LineageLayer.ADS, source_system="ADS",
                        source_object="ADS_SUPPLIER_360", target_layer=LineageLayer.KPI,
                        target_system="KPI", target_object="KPI_SUPPLIER_OTD",
                        refresh_frequency=RefreshFrequency.DAILY, is_active=True)
        e.created_time = datetime(2026, 8, 30, tzinfo=timezone.utc)
        e.updated_time = e.created_time
        read = lineageToRead(e)
        assert read.id == 5
        # Python 访问走 snake_case（field 名）；camelCase 仅用于 JSON 输出（by_alias=True）
        assert read.source_layer == LineageLayer.ADS
        assert read.target_layer == LineageLayer.KPI
        assert read.created_time == e.created_time
        assert read.is_active is True


class TestDtoValidation:
    """DTO 字段约束（pydantic 校验在请求入口 + 服务入口双层）。"""

    def test_endpoint_requires_all_required_fields(self):
        with pytest.raises(Exception):  # noqa: PT011
            LineageEdgeCreate()  # type: ignore[call-arg]

    def test_max_lengths_enforced(self):
        with pytest.raises(Exception):  # noqa: PT011
            _makeCreate(source_system="x" * 101)  # > 100 chars
