"""本体属性 business_aliases/description 字段（2-2）CRUD 持久化测试。

覆盖：创建持久化、显式 None 清空（exclude_unset 语义）、空列表写入。
边界校验（纯 pydantic）留在 unit/test_ontology_property.py。

【迁移：真实 PG】由 unit/ 迁至 integration/（第三批），dbSession 走 integration/conftest.py
的真实 PostgreSQL + 每测试 TRUNCATE 隔离（Harness/rules/测试规范.md）；Neo4j 为外部依赖
仍 mock（best-effort），embedding 用 None 桩，其余数据层全真实。
"""

from __future__ import annotations

import pytest

import app.infrastructure.neo4j_client as neo4j
from app.dependencies import CurrentUser
from app.domain.schemas import (
    OntologyClassCreate,
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


async def _createProp(dbSession, service: OntologyService, **extra) -> object:
    cls = await service.createClass(
        dbSession,
        OntologyClassCreate(class_name="PRECEIPT", source_table="ZJTH.PRECEIPT"),
        actor=_ADMIN.userId,
        actor_departments=",".join(_ADMIN.departments) if _ADMIN.departments else None,
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
