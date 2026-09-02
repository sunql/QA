"""ACL 扩展单测（Phase 4.5 后续：3 张表 owner-based ACL）。

覆盖范围：entity_mapping / data_quality_rule / ontology_class 的 update/delete
走 AclService.assertCanModify：admin 通过；owner 部门通过；其他部门 403。

GREEN 阶段：service.updateX / service.deleteX 已注入 acl.assertCanModify。
ORM 模型上的 owner 字段直接由测试构造（不依赖 DTO，DTO 已移除 owner 字段）。

ASCII department tokens（HTTP header 安全；同 Phase 4.5 kpi_catalog 模式）：
PROCUREMENT / FINANCE / QUALITY。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.dependencies import CurrentUser
from app.domain.exceptions import PermissionDeniedError
from app.domain.models import DataQualityRule, EntityMapping, OntologyClass
from app.services.acl_service import ADMIN_ROLE


# ASCII department tokens — 与 Phase 4.5 governance integration 测试一致
PROCUREMENT = "procurement"
FINANCE = "finance"
QUALITY = "quality"


def _user(roles=("user",), departments=()):
    """构造 CurrentUser；与 test_acl_service 同模式。"""
    return CurrentUser(userId="u1", roles=roles, departments=departments)


def _run(coro):
    """Python 3.14 需手动驱动 event loop。"""
    return asyncio.new_event_loop().run_until_complete(coro)


def _makeFakeSession(record: Any):
    """最小 fake session：get / execute 返回 record；commit / refresh / delete no-op。

    get 接受任意 kwargs（e.g. options=[selectinload(...)]）以兼容 OntologyService.
    getClass 等用 selectinload 预加载关系的场景。
    execute 返回空结果（无重名）。
    """
    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Session:
        async def get(self, _model, pk, **_kwargs):
            return record if getattr(record, "id", None) == pk else None

        async def execute(self, _stmt):
            return _Result()

        async def flush(self):
            pass

        async def add(self, entity):
            pass

        async def commit(self):
            pass

        async def rollback(self):
            pass

        async def refresh(self, entity):
            pass

        async def delete(self, entity):
            pass

    return _Session()


# ===========================================================================
# EntityMappingService ACL
# ===========================================================================

class TestEntityMappingAcl:
    """entity_mapping.updateMapping / deleteMapping 走 AclService。"""

    def _mapping(self, owner: str | None, mapping_id: int = 1) -> EntityMapping:
        m = EntityMapping(
            id=mapping_id,
            entity_type="SUPPLIER",
            enterprise_key=100001,
            enterprise_code="SUP000001",
            source_system="ERP",
            source_key="V000001",
            source_code="V000001",
            match_rule="MAPPING",
            owner=owner,
        )
        return m

    def test_update_admin_any_owner_passes(self):
        """admin 角色可改任意 owner 的 mapping。"""
        from app.services.entity_mapping_service import EntityMappingService
        from app.domain.schemas import EntityMappingUpdate

        mapping = self._mapping(owner=PROCUREMENT)
        svc = EntityMappingService()
        # 此调用当前不传 actor → 即使实现后也需 service 接受 actor 参数
        _run(
            svc.updateMapping(
                session=_makeFakeSession(mapping),
                id=1,
                dto=EntityMappingUpdate(enterpriseCode="SUP-NEW"),
                actor=_user(roles=(ADMIN_ROLE,)),
            )
        )

    def test_update_owner_dept_passes(self):
        """owner=procurement + user.departments=procurement → 通过。"""
        from app.services.entity_mapping_service import EntityMappingService
        from app.domain.schemas import EntityMappingUpdate

        mapping = self._mapping(owner=PROCUREMENT)
        svc = EntityMappingService()
        _run(
            svc.updateMapping(
                session=_makeFakeSession(mapping),
                id=1,
                dto=EntityMappingUpdate(enterpriseCode="SUP-NEW"),
                actor=_user(departments=(PROCUREMENT,)),
            )
        )

    def test_update_other_dept_raises(self):
        """owner=procurement + user.departments=finance → PermissionDeniedError。"""
        from app.services.entity_mapping_service import EntityMappingService
        from app.domain.schemas import EntityMappingUpdate

        mapping = self._mapping(owner=PROCUREMENT)
        svc = EntityMappingService()
        with pytest.raises(PermissionDeniedError):
            _run(
                svc.updateMapping(
                    session=_makeFakeSession(mapping),
                    id=1,
                    dto=EntityMappingUpdate(enterpriseCode="SUP-NEW"),
                    actor=_user(departments=(FINANCE,)),
                )
            )

    def test_delete_other_dept_raises(self):
        """owner=procurement + user=finance → delete 403。"""
        from app.services.entity_mapping_service import EntityMappingService

        mapping = self._mapping(owner=PROCUREMENT)
        svc = EntityMappingService()
        with pytest.raises(PermissionDeniedError):
            _run(
                svc.deleteMapping(
                    session=_makeFakeSession(mapping),
                    id=1,
                    actor=_user(departments=(FINANCE,)),
                )
            )


# ===========================================================================
# DataQualityRuleService ACL
# ===========================================================================

class TestDataQualityRuleAcl:
    """data_quality_rule.updateRule / disableRule 走 AclService。"""

    def _rule(self, owner: str | None, rule_id: int = 1) -> DataQualityRule:
        r = DataQualityRule(
            id=rule_id,
            rule_code="DQR_GOV_TEST",
            rule_name="测试规则",
            target_table="PORDER",
            rule_type="COMPLETENESS",
            owner=owner,
            is_enabled=True,
            version="v1.0",
            severity="MEDIUM",
        )
        return r

    def test_update_owner_dept_passes(self):
        from app.services.data_quality_service import DataQualityRuleService
        from app.domain.schemas import DataQualityRuleUpdate

        rule = self._rule(owner=PROCUREMENT)
        svc = DataQualityRuleService()
        _run(
            svc.updateRule(
                session=_makeFakeSession(rule),
                id=1,
                dto=DataQualityRuleUpdate(ruleName="改名"),
                actor=_user(departments=(PROCUREMENT,)),
            )
        )

    def test_update_other_dept_raises(self):
        from app.services.data_quality_service import DataQualityRuleService
        from app.domain.schemas import DataQualityRuleUpdate

        rule = self._rule(owner=PROCUREMENT)
        svc = DataQualityRuleService()
        with pytest.raises(PermissionDeniedError):
            _run(
                svc.updateRule(
                    session=_makeFakeSession(rule),
                    id=1,
                    dto=DataQualityRuleUpdate(ruleName="改名"),
                    actor=_user(departments=(FINANCE,)),
                )
            )

    def test_disable_other_dept_raises(self):
        from app.services.data_quality_service import DataQualityRuleService

        rule = self._rule(owner=QUALITY)
        svc = DataQualityRuleService()
        with pytest.raises(PermissionDeniedError):
            _run(
                svc.disableRule(
                    session=_makeFakeSession(rule),
                    id=1,
                    actor=_user(departments=(FINANCE,)),
                )
            )


# ===========================================================================
# OntologyService ACL
# ===========================================================================

class TestOntologyClassAcl:
    """ontology_class.updateClass / deleteClass 走 AclService（用 object_owner 字段）。"""

    def _klass(self, owner: str | None, class_id: int = 1) -> OntologyClass:
        c = OntologyClass(
            id=class_id,
            class_name="测试类",
            source_table="t",
            object_owner=owner,
            object_type="Master",
            version=1,
        )
        return c

    def test_update_owner_dept_passes(self):
        from app.services.ontology_service import OntologyService
        from app.domain.schemas import OntologyClassUpdate

        klass = self._klass(owner=PROCUREMENT)
        svc = OntologyService()
        _run(
            svc.updateClass(
                session=_makeFakeSession(klass),
                id=1,
                dto=OntologyClassUpdate(className="改名"),
                actor=_user(departments=(PROCUREMENT,)).userId,
                actor_departments=",".join(_user(departments=(PROCUREMENT,)).departments),
            )
        )

    def test_update_other_dept_raises(self):
        from app.services.ontology_service import OntologyService
        from app.domain.schemas import OntologyClassUpdate

        klass = self._klass(owner=PROCUREMENT)
        svc = OntologyService()
        with pytest.raises(PermissionDeniedError):
            _run(
                svc.updateClass(
                    session=_makeFakeSession(klass),
                    id=1,
                    dto=OntologyClassUpdate(className="改名"),
                    actor=_user(departments=(FINANCE,)),
                )
            )

    def test_delete_admin_passes(self):
        """admin 可删任意 owner 的类。"""
        from app.services.ontology_service import OntologyService

        klass = self._klass(owner=PROCUREMENT)
        svc = OntologyService()
        _run(
            svc.deleteClass(
                session=_makeFakeSession(klass),
                id=1,
                actor=_user(roles=(ADMIN_ROLE,)),
            )
        )

    def test_delete_other_dept_raises(self):
        from app.services.ontology_service import OntologyService

        klass = self._klass(owner=QUALITY)
        svc = OntologyService()
        with pytest.raises(PermissionDeniedError):
            _run(
                svc.deleteClass(
                    session=_makeFakeSession(klass),
                    id=1,
                    actor=_user(departments=(FINANCE,)),
                )
            )