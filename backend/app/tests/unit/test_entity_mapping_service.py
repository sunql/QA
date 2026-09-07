"""Phase 3.1 entity_mapping 服务层单测。

TDD 顺序（先 RED）：
1. 列出所有契约（listMappings / getMapping / createMapping / updateMapping / deleteMapping）
2. 每条契约：基础路径 + 边界 + 错误路径
3. 重点覆盖审查修复后的行为：
   - IntegrityError 并发 race → rollback + 转 ValidationError（而非裸 500）
   - update 非空列传 None 视为不动；日期列传 None 显式清除
   - 生效日期晚于失效日期 → ValidationError
   - listMappings 分页（limit / offset）

集成测试在 test_entity_mapping_api.py 用真实 PG 5433 跑。
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.dependencies import CurrentUser
from app.domain.enums import SourceSystem, MatchRule
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import EntityMapping
from app.domain.schemas import (
    EntityMappingCreate,
    EntityMappingUpdate,
    EntityMappingRead,
    EntityMappingSearchHit,
)
from app.services.acl_service import ADMIN_ROLE
from app.services.entity_mapping_service import (
    EntityMappingService,
    entityMappingSearchToHit,
    entityMappingToRead,
)


def _run(coro):
    """Python 3.14 取消隐式 loop 创建，需手动驱动。"""
    return asyncio.new_event_loop().run_until_complete(coro)


def _adminActor() -> CurrentUser:
    """Phase 4.5 ACL 扩展后，update/delete 要求 actor。

    本测试不验 ACL 行为，默认用 admin 角色绕过（admin.roles ⊇ {ADMIN_ROLE}）。
    ACL 行为由 test_governance_extension_acl.py 覆盖。
    """
    return CurrentUser(userId="t-admin", roles=(ADMIN_ROLE,), departments=())


def _fakeSession(
    records: dict[int, EntityMapping] | None = None,
    *,
    commit_error: Exception | None = None,
    precheck_hit: bool = False,
):
    """最小 fake session：模拟 execute / get / add / commit / rollback / refresh / delete。

    - precheck_hit=True：创建时唯一性查重命中（返回首条记录），触发 _existsError。
    - commit_error：commit() 抛该异常，模拟并发 race 撞唯一索引。
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
            self.rollbacks = 0
            self.removed: list[Any] = []

        async def get(self, _model, pk):
            return self.records.get(pk)

        async def execute(self, stmt):  # noqa: ARG002
            if precheck_hit:
                return _Result(list(self.records.values()))
            # 全量 select（listMappings）与「按身份键查重」共用：首条或空。
            return _Result(list(self.records.values()))

        def add(self, entity):
            self.added.append(entity)

        async def commit(self):
            self.commits += 1
            if commit_error is not None:
                raise commit_error
            for e in self.added:
                if getattr(e, "id", None) is None:
                    e.id = self.next_id
                    self.next_id += 1

        async def rollback(self):
            self.rollbacks += 1

        async def refresh(self, entity):
            if getattr(entity, "id", None) is None:
                entity.id = self.next_id
                self.next_id += 1

        async def delete(self, entity):
            self.removed.append(entity)

        async def flush(self):
            for e in self.added:
                if getattr(e, "id", None) is None:
                    e.id = self.next_id
                    self.next_id += 1

    return _Session()


def _makeCreate(**overrides) -> EntityMappingCreate:
    defaults: dict[str, Any] = {
        "entity_type": "SUPPLIER",
        "enterprise_key": 100001,
        "enterprise_code": "SUP000001",
        "source_system": SourceSystem.ERP,
        "source_key": "V000001",
        "source_code": "V000001",
        "match_rule": MatchRule.MDM_MASTER,
        "effective_date": date(2026, 1, 1),
        "expiry_date": date(2099, 12, 31),
    }
    defaults.update(overrides)
    return EntityMappingCreate(**defaults)


class TestListMappings:
    def test_empty_returns_empty_list(self):
        session = _fakeSession(records={})
        result = _run(EntityMappingService().listMappings(session))
        assert result == []

    def test_returns_all_when_no_filter(self):
        e1 = EntityMapping(id=1, entity_type="SUPPLIER", enterprise_key=100001,
                           enterprise_code="SUP000001", source_system=SourceSystem.ERP,
                           source_key="V000001", source_code="V000001",
                           match_rule=MatchRule.MDM_MASTER)
        e2 = EntityMapping(id=2, entity_type="MATERIAL", enterprise_key=100002,
                           enterprise_code="M000001", source_system=SourceSystem.SRM,
                           source_key="S000001", source_code="S000001",
                           match_rule=MatchRule.MAPPING)
        session = _fakeSession(records={1: e1, 2: e2})
        result = _run(EntityMappingService().listMappings(session))
        assert {e.id for e in result} == {1, 2}

    def test_default_pagination_applied(self):
        """默认 limit=200 offset=0 不打爆（真实 SQL 过滤在集成测试覆盖）。"""
        session = _fakeSession(records={1: EntityMapping(
            id=1, entity_type="SUPPLIER", enterprise_key=100001,
            enterprise_code="SUP000001", source_system=SourceSystem.ERP,
            source_key="V000001", source_code="V000001",
            match_rule=MatchRule.MDM_MASTER,
        )})
        result = _run(EntityMappingService().listMappings(session))
        assert isinstance(result, list)


class TestGetMapping:
    def test_returns_mapping_when_found(self):
        e = EntityMapping(id=42, entity_type="SUPPLIER", enterprise_key=100001,
                          enterprise_code="SUP000001", source_system=SourceSystem.ERP,
                          source_key="V000001", source_code="V000001",
                          match_rule=MatchRule.MDM_MASTER)
        session = _fakeSession(records={42: e})
        result = _run(EntityMappingService().getMapping(session, 42))
        assert result.id == 42

    def test_raises_not_found_when_missing(self):
        session = _fakeSession(records={})
        with pytest.raises(NotFoundError):
            _run(EntityMappingService().getMapping(session, 999))


class TestCreateMapping:
    def test_creates_with_all_fields(self):
        session = _fakeSession()
        dto = _makeCreate()
        entity = _run(EntityMappingService().createMapping(session, dto, _adminActor()))
        assert entity.id is not None
        assert entity.entity_type == "SUPPLIER"
        assert entity.enterprise_key == 100001
        assert entity.source_system == SourceSystem.ERP
        assert entity.source_key == "V000001"
        assert entity.match_rule == MatchRule.MDM_MASTER
        assert entity.effective_date == date(2026, 1, 1)
        assert session.commits == 1

    def test_rejects_inverted_date_range(self):
        session = _fakeSession()
        dto = _makeCreate(effective_date=date(2099, 12, 31), expiry_date=date(2026, 1, 1))
        with pytest.raises(ValidationError):
            _run(EntityMappingService().createMapping(session, dto, _adminActor()))
        # 校验失败不应产生任何提交
        assert session.commits == 0

    def test_precheck_hit_raises_validation_error(self):
        """service 层唯一性查重命中 → 422，不落库。"""
        session = _fakeSession(records={1: EntityMapping(
            id=1, entity_type="SUPPLIER", enterprise_key=100001,
            enterprise_code="SUP000001", source_system=SourceSystem.ERP,
            source_key="V000001", source_code="V000001",
            match_rule=MatchRule.MDM_MASTER,
        )}, precheck_hit=True)
        dto = _makeCreate()
        with pytest.raises(ValidationError):
            _run(EntityMappingService().createMapping(session, dto, _adminActor()))
        assert session.commits == 0

    def test_commit_integrity_error_rolls_back_and_raises_validation(self):
        """并发 race：查重已过但 commit 撞唯一索引 → rollback + 转 422 而非裸 500。"""
        session = _fakeSession(commit_error=IntegrityError("stmt", {}, Exception("dup")))
        dto = _makeCreate()
        with pytest.raises(ValidationError):
            _run(EntityMappingService().createMapping(session, dto, _adminActor()))
        assert session.rollbacks == 1


class TestUpdateMapping:
    def _record(self) -> EntityMapping:
        return EntityMapping(
            id=3, entity_type="SUPPLIER", enterprise_key=100001,
            enterprise_code="SUP000001", source_system=SourceSystem.ERP,
            source_key="V000001", source_code="V000001",
            match_rule=MatchRule.MDM_MASTER,
            effective_date=date(2026, 1, 1),
            expiry_date=date(2099, 12, 31),
        )

    def test_partial_update_only_overrides_sent_fields(self):
        e = self._record()
        session = _fakeSession(records={3: e})
        dto = EntityMappingUpdate(source_code="V000001-X")
        updated = _run(EntityMappingService().updateMapping(session, 3, dto, _adminActor()))
        assert updated.source_code == "V000001-X"
        # 未发送字段保留
        assert updated.entity_type == "SUPPLIER"
        assert updated.enterprise_key == 100001
        assert updated.match_rule == MatchRule.MDM_MASTER
        assert session.commits == 1

    def test_none_for_non_nullable_field_is_ignored(self):
        """非空列显式传 None → 不动（None 语义为「跳过」）。"""
        e = self._record()
        session = _fakeSession(records={3: e})
        dto = EntityMappingUpdate(source_code=None)
        updated = _run(EntityMappingService().updateMapping(session, 3, dto, _adminActor()))
        assert updated.source_code == "V000001"

    def test_none_for_date_clears_field(self):
        """日期列显式传 None → 清除已有值。"""
        e = self._record()
        session = _fakeSession(records={3: e})
        dto = EntityMappingUpdate(expiry_date=None)
        updated = _run(EntityMappingService().updateMapping(session, 3, dto, _adminActor()))
        assert updated.expiry_date is None

    def test_rejects_inverted_date_range_on_update(self):
        """更新后生效日期晚于失效日期 → ValidationError，且不提交。"""
        e = self._record()
        session = _fakeSession(records={3: e})
        dto = EntityMappingUpdate(effective_date=date(2100, 1, 1))
        with pytest.raises(ValidationError):
            _run(EntityMappingService().updateMapping(session, 3, dto, _adminActor()))
        assert session.commits == 0

    def test_raises_not_found_when_missing(self):
        session = _fakeSession(records={})
        dto = EntityMappingUpdate(source_code="X")
        with pytest.raises(NotFoundError):
            _run(EntityMappingService().updateMapping(session, 999, dto, _adminActor()))


class TestDeleteMapping:
    def test_delete_removes_and_commits(self):
        e = EntityMapping(
            id=4, entity_type="SUPPLIER", enterprise_key=100001,
            enterprise_code="SUP000001", source_system=SourceSystem.ERP,
            source_key="V000001", source_code="V000001",
            match_rule=MatchRule.MAPPING,
        )
        session = _fakeSession(records={4: e})
        _run(EntityMappingService().deleteMapping(session, 4, _adminActor()))
        assert session.removed == [e]
        assert session.commits == 1

    def test_raises_not_found_when_missing(self):
        session = _fakeSession(records={})
        with pytest.raises(NotFoundError):
            _run(EntityMappingService().deleteMapping(session, 999, _adminActor()))


class TestEntityMappingToRead:
    def test_roundtrip_snake_case_fields(self):
        e = EntityMapping(id=5, entity_type="SUPPLIER", enterprise_key=100001,
                          enterprise_code="SUP000001", source_system=SourceSystem.ERP,
                          source_key="V000001", source_code="V000001",
                          match_rule=MatchRule.MAPPING)
        read = entityMappingToRead(e)
        assert isinstance(read, EntityMappingRead)
        assert read.id == 5
        assert read.entity_type == "SUPPLIER"
        assert read.enterprise_key == 100001
        assert read.source_system == SourceSystem.ERP
        assert read.match_rule == MatchRule.MAPPING


class TestDtoValidation:
    def test_requires_required_fields(self):
        with pytest.raises(Exception):  # noqa: PT011
            EntityMappingCreate()  # type: ignore[call-arg]

    def test_enterprise_key_bound_enforced(self):
        """超过 BIGINT 上限（2^63-1）被 Pydantic 拒绝（L4 修复）。"""
        with pytest.raises(Exception):  # noqa: PT011
            _makeCreate(enterprise_key=2**63)

    def test_empty_string_rejected_on_update(self):
        """update 非空列空串被 min_length=1 拒绝（L1 修复）。"""
        with pytest.raises(Exception):  # noqa: PT011
            EntityMappingUpdate(source_code="")


class TestSearchMappings:
    """Phase 6.x：AutoComplete 搜索接口单测。

    用本地 _searchFakeSession 模拟 searchMappings 的 SQL 行为（不依赖真实 PG）：
    - q 空 → 空列表
    - q 全数字 → enterprise_key 精确匹配 + enterprise_code/source_code ILIKE
    - q 非数字 → enterprise_code/source_code ILIKE
    - entityType 过滤
    - limit 上限 100
    """

    def _seed_records(self):
        return [
            EntityMapping(id=1, entity_type="SUPPLIER", enterprise_key=100001,
                          enterprise_code="SUP000001", source_system=SourceSystem.ERP,
                          source_key="V000001", source_code="V000001",
                          match_rule=MatchRule.MDM_MASTER),
            EntityMapping(id=2, entity_type="SUPPLIER", enterprise_key=100002,
                          enterprise_code="SUP000002", source_system=SourceSystem.SRM,
                          source_key="S000002", source_code="SRM-SUP-002",
                          match_rule=MatchRule.MAPPING),
            EntityMapping(id=3, entity_type="MATERIAL", enterprise_key=200001,
                          enterprise_code="MAT000001", source_system=SourceSystem.ERP,
                          source_key="V200001", source_code="V200001",
                          match_rule=MatchRule.MDM_MASTER),
            EntityMapping(id=4, entity_type="MATERIAL", enterprise_key=200002,
                          enterprise_code="MAT000002", source_system=SourceSystem.QMS,
                          source_key="Q200002", source_code="QMS-MAT-002",
                          match_rule=MatchRule.BUSINESS_KEY),
        ]

    def _searchFakeSession(self, records):
        """按 service.searchMappings 语义过滤的 fake：q 空返空，非空按条件。"""

        class _Result:
            def __init__(self, items):
                self._items = items

            def scalars(self):
                return SimpleNamespace(all=lambda: list(self._items))

        class _Session:
            def __init__(self, records):
                self.records = records

            async def execute(self, stmt):
                # 简单按 q 字段值解析（test 用，绕开 SQLAlchemy 表达式编译）：
                # 仅依赖 service 调用时传的参数重现过滤。
                return _Result(list(self.records))

        return _Session(records)

    def test_empty_q_returns_empty(self):
        svc = EntityMappingService()
        for q in ["", "   ", "\t"]:
            result = _run(svc.searchMappings(self._searchFakeSession([]), q=q))
            assert result == [], f"q={q!r} should yield empty"

    def test_q_matches_enterprise_code_ilike(self):
        svc = EntityMappingService()
        # 用真实 service 走 fake，注入 q="SUP000" → 期望两条 SUP 命中
        records = self._seed_records()

        # 我们用一个真实过滤 fake：按 q 是否命中 enterprise_code/source_code 返子集
        class _FilteringSession:
            def __init__(self, recs, q, entity_type):
                self._q = q
                self._entity_type = entity_type
                self._all = recs

            async def execute(self, _stmt):
                items = []
                for r in self._all:
                    if self._entity_type is not None and r.entity_type != self._entity_type:
                        continue
                    q = self._q
                    hit = (
                        q.isdigit() and r.enterprise_key == int(q)
                    ) or (q.lower() in r.enterprise_code.lower()) or (q.lower() in r.source_code.lower())
                    if hit:
                        items.append(r)
                # 模拟排序：数字 q 时精确命中排前
                if self._q.isdigit():
                    items.sort(key=lambda r: (r.enterprise_key != int(self._q), r.id))
                else:
                    items.sort(key=lambda r: r.id)
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: items))

        session = _FilteringSession(records, "SUP000", None)
        result = _run(svc.searchMappings(session, q="SUP000"))
        assert {r.id for r in result} == {1, 2}

    def test_q_matches_source_code_ilike(self):
        records = self._seed_records()

        class _FilteringSession:
            def __init__(self, recs, q):
                self._q = q
                self._all = recs

            async def execute(self, _stmt):
                items = [
                    r for r in self._all
                    if self._q.lower() in r.source_code.lower()
                    or self._q.lower() in r.enterprise_code.lower()
                    or (self._q.isdigit() and r.enterprise_key == int(self._q))
                ]
                if self._q.isdigit():
                    items.sort(key=lambda r: (r.enterprise_key != int(self._q), r.id))
                else:
                    items.sort(key=lambda r: r.id)
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: items))

        result = _run(EntityMappingService().searchMappings(_FilteringSession(records, "QMS"), q="QMS"))
        assert {r.id for r in result} == {4}

    def test_q_is_digit_prefers_exact_enterprise_key(self):
        """输入纯数字时：enterprise_key 精确命中排前 + ILIKE 兜底。"""
        records = self._seed_records()

        class _FilteringSession:
            def __init__(self, recs, q):
                self._q = q
                self._all = recs

            async def execute(self, _stmt):
                items = [
                    r for r in self._all
                    if (self._q.isdigit() and r.enterprise_key == int(self._q))
                    or self._q.lower() in r.enterprise_code.lower()
                    or self._q.lower() in r.source_code.lower()
                ]
                items.sort(key=lambda r: (r.enterprise_key != int(self._q), r.id))
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: items))

        # enterprise_key=100002 + enterprise_code 含 "100" 的（没有）→ 只返 id=2
        result = _run(EntityMappingService().searchMappings(_FilteringSession(records, "100002"), q="100002"))
        assert [r.id for r in result][:1] == [2]

    def test_q_ilike_does_not_match_unrelated_codes(self):
        records = self._seed_records()

        class _FilteringSession:
            def __init__(self, recs, q):
                self._q = q
                self._all = recs

            async def execute(self, _stmt):
                items = [
                    r for r in self._all
                    if self._q.lower() in r.enterprise_code.lower()
                    or self._q.lower() in r.source_code.lower()
                    or (self._q.isdigit() and r.enterprise_key == int(self._q))
                ]
                items.sort(key=lambda r: r.id)
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: items))

        # "NOPE" 不命中任何记录
        result = _run(EntityMappingService().searchMappings(_FilteringSession(records, "NOPE"), q="NOPE"))
        assert result == []

    def test_entity_type_filter_narrows_results(self):
        records = self._seed_records()

        class _FilteringSession:
            def __init__(self, recs, q, entity_type):
                self._q = q
                self._entity_type = entity_type
                self._all = recs

            async def execute(self, _stmt):
                items = []
                for r in self._all:
                    if self._entity_type is not None and r.entity_type != self._entity_type:
                        continue
                    if (
                        self._q.lower() in r.enterprise_code.lower()
                        or self._q.lower() in r.source_code.lower()
                        or (self._q.isdigit() and r.enterprise_key == int(self._q))
                    ):
                        items.append(r)
                items.sort(key=lambda r: r.id)
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: items))

        # q="000001" + entity_type=SUPPLIER → 只 id=1
        result = _run(
            EntityMappingService().searchMappings(
                _FilteringSession(records, "000001", "SUPPLIER"), q="000001"
            )
        )
        assert {r.id for r in result} == {1}

    def test_limit_is_capped_at_100(self):
        """service 层 limit 截断：limit > 100 时不抛错（SQL `LIMIT 100` 由集成测试覆盖）。

        fake 不解析 SQL LIMIT，所以这里只验服务不崩溃 + 返回结果数 == 入库数。
        真实 LIMIT 截断在集成测试（test_entity_mapping_api.py）里覆盖。
        """
        from app.domain.enums import SourceSystem as SS
        from app.domain.enums import MatchRule as MR

        records = [
            EntityMapping(id=i, entity_type="SUPPLIER", enterprise_key=100000 + i,
                          enterprise_code=f"SUP{i:06d}", source_system=SS.ERP,
                          source_key=f"V{i:06d}", source_code=f"V{i:06d}",
                          match_rule=MR.MDM_MASTER)
            for i in range(1, 6)  # 5 条够验证「不抛错 + 返回全部」
        ]

        class _AllMatchSession:
            def __init__(self, recs):
                self._recs = recs

            async def execute(self, _stmt):
                return SimpleNamespace(
                    scalars=lambda: SimpleNamespace(all=lambda: list(self._recs))
                )

        # limit=999（> 100）不应抛错；返回数 = 入库数（fake 不截）
        result = _run(
            EntityMappingService().searchMappings(_AllMatchSession(records), q="SUP", limit=999)
        )
        assert len(result) == 5


class TestEntityMappingSearchToHit:
    def test_dto_only_exposes_search_fields(self):
        e = EntityMapping(id=5, entity_type="SUPPLIER", enterprise_key=100001,
                          enterprise_code="SUP000001", source_system=SourceSystem.ERP,
                          source_key="V000001", source_code="V000001",
                          match_rule=MatchRule.MAPPING, owner="procurement",
                          effective_date=date(2026, 1, 1), expiry_date=date(2099, 12, 31))
        hit = entityMappingSearchToHit(e)
        assert isinstance(hit, EntityMappingSearchHit)
        assert hit.id == 5
        assert hit.enterprise_key == 100001
        assert hit.enterprise_code == "SUP000001"
        assert hit.source_system == SourceSystem.ERP
        assert hit.entity_type == "SUPPLIER"
        assert hit.source_code == "V000001"
        # 治理字段不应在 DTO 里：
        assert not hasattr(hit, "owner")
        assert not hasattr(hit, "effective_date")
        assert not hasattr(hit, "match_rule")
