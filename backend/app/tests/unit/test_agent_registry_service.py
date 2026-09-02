"""Phase 6.1 Agent Registry 服务层单测。

TDD 顺序（先 RED → 实现 → GREEN）：
- 验证 actor → owner 派生规则（admin / 普通用户）
- ACL：owner 不匹配 + 非 admin → PermissionDeniedError（403）
- ACL：admin / owner 匹配 → 通过
- create 重复 agent_code → ConflictError
- get 未知 code → NotFoundError
- deprecate → status=deprecated
- 嵌套策略：add 重复 (data_object, data_layer) → ConflictError
- 嵌套策略：delete 未知 id → NotFoundError

集成测试在 test_agent_registry_api.py 用真实 PG 5433 跑。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.dependencies import CurrentUser
from app.domain.enums import (
    AgentPermission,
    AgentResponseLatency,
    AgentStatus,
    AgentTriggerType,
)
from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.models import AgentAccessPolicy, AgentDefinition
from app.domain.schemas import (
    AgentAccessPolicyCreate,
    AgentAccessPolicyUpdate,
    AgentDefinitionCreate,
    AgentDefinitionUpdate,
)
from app.services.acl_service import ADMIN_ROLE
from app.services.agent_registry_service import (
    AgentRegistryService,
    _policyToRead,
    agentToRead,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _admin() -> CurrentUser:
    return CurrentUser(userId="t-admin", roles=(ADMIN_ROLE,), departments=())


def _ownerActor(dept: str = "采购部") -> CurrentUser:
    return CurrentUser(
        userId="t-procurement", roles=("user",), departments=(dept,)
    )


def _stranger() -> CurrentUser:
    return CurrentUser(
        userId="t-stranger", roles=("user",), departments=("财务部",)
    )


def _noDeptActor() -> CurrentUser:
    """部门为空 → owner=None 派生路径（仅 admin 可改）。"""
    return CurrentUser(userId="t-bare", roles=("user",), departments=())


def _fakeSession(
    records: dict[int, AgentDefinition] | None = None,
    *,
    commit_error: Exception | None = None,
):
    """最小 fake session：execute / get / add / commit / rollback / refresh / delete。"""

    class _Result:
        def __init__(self, items: list[Any]):
            self._items = items

        def scalars(self) -> SimpleNamespace:
            return SimpleNamespace(all=lambda: list(self._items))

        def scalar_one_or_none(self) -> Any | None:
            return self._items[0] if self._items else None

    class _Session:
        def __init__(self):
            self.records: dict[int, AgentDefinition] = records or {}
            self.next_id = (
                (max(records) + 1) if records else 1
            )
            self.added: list[Any] = []
            self.commits = 0
            self.rollbacks = 0
            self.removed: list[Any] = []

        async def get(self, model, pk):
            return self.records.get(pk)

        async def execute(self, stmt):  # noqa: ARG002
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

        async def refresh(self, entity, attribute_names=None):
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


def _makeCreate(**overrides) -> AgentDefinitionCreate:
    defaults: dict[str, Any] = {
        "agent_code": "TEST_AGENT_001",
        "agent_name": "测试 Agent",
        "description": "单测示例",
        "trigger_type": AgentTriggerType.USER_QUESTION,
        "response_latency": AgentResponseLatency.REALTIME,
        "data_domains": ["PROCUREMENT"],
        "data_layers": ["FEATURE"],
        "status": AgentStatus.DRAFT,
        "version": "v1.0",
        "policies": [],
    }
    defaults.update(overrides)
    return AgentDefinitionCreate(**defaults)


class TestOwnerDerivation:
    def test_create_owner_derived_from_first_department(self):
        """create 时 owner = actor.departments[0]（entity_mapping 同模式）。"""
        session = _fakeSession()
        service = AgentRegistryService()
        entity = _run(
            service.createAgent(
                session, _makeCreate(), _ownerActor("采购部")
            )
        )
        assert entity.owner == "采购部"
        assert entity.agent_code == "TEST_AGENT_001"

    def test_create_owner_none_when_actor_has_none(self):
        """actor.departments 为空 → owner=None（仅 admin 可改）。"""
        session = _fakeSession()
        service = AgentRegistryService()
        entity = _run(
            service.createAgent(session, _makeCreate(), _noDeptActor())
        )
        assert entity.owner is None

    def test_create_duplicate_code_raises_conflict(self):
        """重复 agent_code → ConflictError（DB 唯一索引兜底）。"""
        existing = AgentDefinition(
            id=1,
            agent_code="DUP_AGENT",
            agent_name="已存在",
            trigger_type="user_question",
            response_latency="realtime",
            data_domains=[],
            data_layers=[],
            status="draft",
            version="v1.0",
        )
        session = _fakeSession(
            records={1: existing},
            commit_error=__import__("sqlalchemy").exc.IntegrityError(
                "stmt", {}, Exception("duplicate")
            ),
        )
        service = AgentRegistryService()
        from app.domain.schemas import AgentDefinitionCreate

        with __import__("pytest").raises(ConflictError):
            _run(
                service.createAgent(
                    session,
                    _makeCreate(agent_code="DUP_AGENT"),
                    _ownerActor(),
                )
            )


class TestGetAgent:
    def test_get_unknown_code_raises_not_found(self):
        session = _fakeSession(records={})
        service = AgentRegistryService()
        with __import__("pytest").raises(NotFoundError):
            _run(service.getAgent(session, "UNKNOWN"))


class TestUpdateAcl:
    def _setup_existing(self, owner: str | None = "采购部") -> AgentDefinition:
        entity = AgentDefinition(
            id=10,
            agent_code="EXIST_AGENT",
            agent_name="既有",
            trigger_type="user_question",
            response_latency="realtime",
            data_domains=[],
            data_layers=[],
            status="draft",
            owner=owner,
            version="v1.0",
        )
        return entity

    def test_update_admin_succeeds_even_when_owner_mismatch(self):
        entity = self._setup_existing(owner="采购部")
        session = _fakeSession(records={10: entity})
        service = AgentRegistryService()
        result = _run(
            service.updateAgent(
                session,
                "EXIST_AGENT",
                AgentDefinitionUpdate(agent_name="新名"),
                _admin(),  # admin 绕过 owner 检查
            )
        )
        assert result.agent_name == "新名"

    def test_update_owner_match_succeeds(self):
        entity = self._setup_existing(owner="采购部")
        session = _fakeSession(records={10: entity})
        service = AgentRegistryService()
        result = _run(
            service.updateAgent(
                session,
                "EXIST_AGENT",
                AgentDefinitionUpdate(agent_name="新名"),
                _ownerActor("采购部"),
            )
        )
        assert result.agent_name == "新名"

    def test_update_non_admin_owner_mismatch_raises_permission_denied(self):
        from app.domain.exceptions import PermissionDeniedError

        entity = self._setup_existing(owner="采购部")
        session = _fakeSession(records={10: entity})
        service = AgentRegistryService()
        with __import__("pytest").raises(PermissionDeniedError):
            _run(
                service.updateAgent(
                    session,
                    "EXIST_AGENT",
                    AgentDefinitionUpdate(agent_name="新名"),
                    _stranger(),
                )
            )


class TestDeprecate:
    def test_deprecate_sets_status_to_deprecated(self):
        entity = AgentDefinition(
            id=20,
            agent_code="DEP_AGENT",
            agent_name="待停用",
            trigger_type="user_question",
            response_latency="realtime",
            data_domains=[],
            data_layers=[],
            status="active",
            owner="采购部",
            version="v1.0",
        )
        session = _fakeSession(records={20: entity})
        service = AgentRegistryService()
        result = _run(
            service.deprecateAgent(session, "DEP_AGENT", _admin())
        )
        assert result.status == AgentStatus.DEPRECATED.value


class TestPolicies:
    def test_add_policy_duplicate_raises_conflict(self):
        agent = AgentDefinition(
            id=30,
            agent_code="POLICY_AGENT",
            agent_name="策略测试",
            trigger_type="user_question",
            response_latency="realtime",
            data_domains=[],
            data_layers=[],
            status="active",
            owner="采购部",
            version="v1.0",
        )
        agent.policies.append(
            AgentAccessPolicy(
                data_object="SUPPLIER",
                permission="read",
                data_layer=None,
                notes=None,
            )
        )
        session = _fakeSession(
            records={30: agent},
            commit_error=__import__("sqlalchemy").exc.IntegrityError(
                "stmt", {}, Exception("duplicate policy")
            ),
        )
        service = AgentRegistryService()
        with __import__("pytest").raises(ConflictError):
            _run(
                service.addPolicy(
                    session,
                    "POLICY_AGENT",
                    AgentAccessPolicyCreate(
                        data_object="SUPPLIER",
                        permission=AgentPermission.READ,
                    ),
                    _admin(),
                )
            )

    def test_delete_policy_not_found_raises_not_found(self):
        agent = AgentDefinition(
            id=40,
            agent_code="NO_POLICY",
            agent_name="空策略",
            trigger_type="user_question",
            response_latency="realtime",
            data_domains=[],
            data_layers=[],
            status="active",
            owner=None,
            version="v1.0",
        )
        # session.get(AgentAccessPolicy, 999) → None
        session = _fakeSession(records={40: agent})
        service = AgentRegistryService()
        with __import__("pytest").raises(NotFoundError):
            _run(service.deletePolicy(session, "NO_POLICY", 999, _admin()))


class TestPolicyDataLayerNormalization:
    """data_layer 边界归一化（strip + upper，空串 → None 通配）。

    工具声明规范大写层（DIM/DWD/FEATURE），策略写入时若大小写/空白不一致会静默
    fail-closed（看似授权实则 403，难排查）。在系统边界归一化后比较才良定义。
    """

    def test_create_normalizes_data_layer(self):
        dto = AgentAccessPolicyCreate(
            data_object="SUPPLIER",
            permission=AgentPermission.READ,
            data_layer=" feature ",
        )
        assert dto.data_layer == "FEATURE"

    def test_create_empty_string_becomes_wildcard(self):
        dto = AgentAccessPolicyCreate(
            data_object="SUPPLIER",
            permission=AgentPermission.READ,
            data_layer="",
        )
        assert dto.data_layer is None

    def test_create_none_stays_wildcard(self):
        dto = AgentAccessPolicyCreate(
            data_object="SUPPLIER",
            permission=AgentPermission.READ,
            data_layer=None,
        )
        assert dto.data_layer is None

    def test_update_normalizes_data_layer(self):
        dto = AgentAccessPolicyUpdate(data_layer="dim")
        assert dto.data_layer == "DIM"


class TestDtoConversion:
    def test_agent_to_read_roundtrip(self):
        now = datetime.now(UTC)
        agent = AgentDefinition(
            id=1,
            agent_code="DTO_AGENT",
            agent_name="DTO 测试",
            description="desc",
            trigger_type="user_question",
            response_latency="realtime",
            data_domains=["PROCUREMENT"],
            data_layers=["FEATURE"],
            status="active",
            owner="采购部",
            version="v1.0",
            created_time=now,
            updated_time=now,
        )
        agent.policies.append(
            AgentAccessPolicy(
                id=99,
                data_object="SUPPLIER",
                permission="read",
                data_layer="DWS",
                notes="ok",
                created_time=now,
            )
        )
        read = agentToRead(agent)
        assert read.agent_code == "DTO_AGENT"
        assert read.data_domains == ["PROCUREMENT"]
        assert len(read.policies) == 1
        assert read.policies[0].data_object == "SUPPLIER"
        assert read.policies[0].permission == AgentPermission.READ

    def test_policy_to_read_roundtrip(self):
        p = AgentAccessPolicy(
            id=5,
            data_object="PO",
            permission="forbidden_write",
            data_layer=None,
            notes="只写不可读",
            created_time=datetime.now(UTC),
        )
        read = _policyToRead(p)
        assert read.data_object == "PO"
        assert read.permission == AgentPermission.FORBIDDEN_WRITE
        assert read.notes == "只写不可读"