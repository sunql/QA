"""本体类更新（原版本管理回归 → 原地更新）测试。

覆盖 updateClass 原地更新语义（版本管理已移除）：
- 主键 id 稳定：更新不产生新行，属性/引用天然保持有效；
- 属性不克隆不重复：同一条属性行仍挂在原类上；
- 子类 parent_class_id / 属性 FK ref_class_id / metric target_class_id 无需重映射；
- 连续多次更新 id 始终不变、版本号不再递增；
- 更新后 schema 文本仍含列，validatePlan 通过（用户报障的直接回归点）。

【迁移：真实 PG】由 unit/ 迁至 integration/（第三批），dbSession 走 integration/conftest.py
的真实 PostgreSQL + 每测试 TRUNCATE 隔离（Harness/rules/测试规范.md）；Neo4j 为外部依赖
仍 mock（best-effort），embedding 用 None 桩，其余数据层全真实。
"""

from __future__ import annotations

import pytest

import app.infrastructure.neo4j_client as neo4j
from app.domain.schemas import (
    OntologyClassCreate,
    OntologyClassUpdate,
    OntologyMetricCreate,
    OntologyPropertyCreate,
)
from app.services.ontology_service import OntologyService


# Phase 4.5 ACL 扩展后 updateClass/deleteClass 要求 actor。
# 本测试不验 ACL 行为，用 admin 绕过。ACL 行为由 test_governance_extension_acl.py 覆盖。
_ADMIN_ID = "t-admin"
_ADMIN_DEPARTMENTS = ""  # empty, matches _ADMIN.departments=()


@pytest.fixture()
def _neutralizeNeo4j(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neo4j 同步为 best-effort：mock 掉写入函数，避免依赖外部服务。"""
    monkeypatch.setattr(neo4j, "upsertClassNode", lambda **kw: None)
    monkeypatch.setattr(neo4j, "reconcileClassSubclassOf", lambda *a, **kw: None)
    monkeypatch.setattr(neo4j, "deleteNode", lambda *a, **kw: None)


@pytest.fixture()
def service() -> OntologyService:
    return OntologyService(embeddingService=None)


async def _createClassWithProperty(
    dbSession, service: OntologyService,
) -> tuple[object, object]:
    """创建带一个 STRING 属性、含 FK 引用的类，返回 (classEntity, propertyEntity)。"""
    cls = await service.createClass(
        dbSession,
        OntologyClassCreate(class_name="PRECEIPT", class_alias="收货单", source_table="ZJTH.PRECEIPT"),
        actor=_ADMIN_ID,
        actor_departments=_ADMIN_DEPARTMENTS,
    )
    prop = await service.createProperty(
        dbSession,
        OntologyPropertyCreate(
            class_id=cls.id,
            property_name="STATUS",
            data_type="STRING",
            source_column="STATUS_0",
        ),
        actor=_ADMIN_ID,
        actor_departments=_ADMIN_DEPARTMENTS,
    )
    return cls, prop


class TestUpdateKeepsOwnedData:
    async def test_update_keeps_id_and_properties(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """原地更新：id 不变、版本不递增、属性行不克隆不重复。

        回归点：旧版本管理新增行 + 克隆导致属性空壳（"属性看不到"），
        以及 Milvus 向量指向旧 id 导致"找不到本体对象"。
        """
        cls, prop = await _createClassWithProperty(dbSession, service)
        updated = await service.updateClass(
            dbSession, cls.id, OntologyClassUpdate(description="v2 描述"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS
        )
        assert updated.id == cls.id  # 主键稳定
        assert updated.version == 1  # 不再版本提升
        assert updated.description == "v2 描述"

        reloaded = await service.getClass(dbSession, cls.id)
        assert [p.property_name for p in reloaded.properties] == ["STATUS"]
        assert {p.id for p in reloaded.properties} == {prop.id}  # 同一条属性行，不克隆

    async def test_list_classes_returns_updated_class_with_properties(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """listClasses 返回更新后的类，且仍带属性（智能问答 schema 来源）。"""
        cls, prop = await _createClassWithProperty(dbSession, service)
        await service.updateClass(dbSession, cls.id, OntologyClassUpdate(class_alias="收货单v2"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        current = await service.listClasses(dbSession)
        assert len(current) == 1
        assert current[0].id == cls.id  # 不产生新行
        assert current[0].class_alias == "收货单v2"
        assert [p.property_name for p in current[0].properties] == ["STATUS"]

    async def test_update_keeps_child_parent_reference(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """子类 parent_class_id 在父类更新后仍指向同一 id（无需重映射）。"""
        parent = await service.createClass(
            dbSession, OntologyClassCreate(class_name="B", source_table="T_B")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        child = await service.createClass(
            dbSession, OntologyClassCreate(class_name="C", source_table="T_C", parent_class_id=parent.id)
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        updatedParent = await service.updateClass(
            dbSession, parent.id, OntologyClassUpdate(description="B v2"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS
        )
        assert updatedParent.id == parent.id
        reloadedChild = await service.getClass(dbSession, child.id)
        assert reloadedChild.parent_class_id == parent.id

    async def test_update_keeps_fk_ref_class(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """其他类属性的 ref_class_id 在目标类更新后仍指向同一 id。"""
        target = await service.createClass(
            dbSession, OntologyClassCreate(class_name="SUPPLIER", source_table="T_SUPPLIER")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        source = await service.createClass(
            dbSession, OntologyClassCreate(class_name="RECEIPT", source_table="T_RECEIPT")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        fk = await service.createProperty(
            dbSession,
            OntologyPropertyCreate(
                class_id=source.id,
                property_name="SUPPLIER_ID",
                data_type="STRING",
                is_foreign_key=True,
                ref_class_id=target.id,
            ),
            actor=_ADMIN_ID,
            actor_departments=_ADMIN_DEPARTMENTS,
        )
        updatedTarget = await service.updateClass(
            dbSession, target.id, OntologyClassUpdate(description="SUPPLIER v2"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS
        )
        assert updatedTarget.id == target.id
        reloadedSource = await service.getClass(dbSession, source.id)
        reloadedFk = next(p for p in reloadedSource.properties if p.property_name == "SUPPLIER_ID")
        assert reloadedFk.ref_class_id == target.id

    async def test_update_keeps_metric_target(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """metric target_class_id 在目标类更新后仍指向同一 id。"""
        cls = await service.createClass(
            dbSession, OntologyClassCreate(class_name="SALES", source_table="T_SALES")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        metric = await service.createMetric(
            dbSession,
            OntologyMetricCreate(metric_name="sales_sum", formula="SUM(QTY)", target_class_id=cls.id),
            actor=_ADMIN_ID,
            actor_departments=_ADMIN_DEPARTMENTS,
        )
        updatedCls = await service.updateClass(
            dbSession, cls.id, OntologyClassUpdate(description="SALES v2"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS
        )
        assert updatedCls.id == cls.id
        reloaded = await service.getMetric(dbSession, metric.id)
        assert reloaded.target_class_id == cls.id

    async def test_update_keeps_self_fk_property(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """自引用 FK（ref_class_id == 自身 class_id，如员工→经理层级）更新后不悬空。"""
        cls = await service.createClass(
            dbSession, OntologyClassCreate(class_name="EMPLOYEE", source_table="T_EMPLOYEE")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        await service.createProperty(
            dbSession,
            OntologyPropertyCreate(
                class_id=cls.id,
                property_name="MANAGER_ID",
                data_type="STRING",
                is_foreign_key=True,
                ref_class_id=cls.id,
            ),
            actor=_ADMIN_ID,
            actor_departments=_ADMIN_DEPARTMENTS,
        )
        updatedCls = await service.updateClass(
            dbSession, cls.id, OntologyClassUpdate(description="EMPLOYEE v2"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS
        )
        assert updatedCls.id == cls.id
        reloaded = await service.getClass(dbSession, cls.id)
        fk = next(p for p in reloaded.properties if p.property_name == "MANAGER_ID")
        assert fk.ref_class_id == cls.id  # 仍指向自身，未失效

    async def test_update_keeps_property_aliases_and_description(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """2-2：更新类后属性保留 business_aliases/description（schema 消歧依赖）。"""
        cls = await service.createClass(
            dbSession, OntologyClassCreate(class_name="PRECEIPT", source_table="ZJTH.PRECEIPT")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        await service.createProperty(
            dbSession,
            OntologyPropertyCreate(
                class_id=cls.id,
                property_name="AMT_0",
                data_type="DECIMAL",
                source_column="AMT_0",
                property_alias="金额",
                business_aliases=["营业额", "收入"],
                description="订单实收金额",
            ),
            actor=_ADMIN_ID,
            actor_departments=_ADMIN_DEPARTMENTS,
        )
        await service.updateClass(dbSession, cls.id, OntologyClassUpdate(description="v2"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        reloaded = await service.getClass(dbSession, cls.id)
        prop = reloaded.properties[0]
        assert prop.property_alias == "金额"
        assert prop.business_aliases == ["营业额", "收入"]
        assert prop.description == "订单实收金额"

    async def test_repeated_updates_keep_stable_id(self, dbSession, service, _neutralizeNeo4j) -> None:
        """连续多次更新：id 始终不变、版本号不再递增、属性与父引用不丢失。"""
        parent = await service.createClass(
            dbSession, OntologyClassCreate(class_name="ORG", source_table="T_ORG")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        child = await service.createClass(
            dbSession, OntologyClassCreate(class_name="DEPT", source_table="T_DEPT", parent_class_id=parent.id)
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        await service.createProperty(
            dbSession,
            OntologyPropertyCreate(class_id=child.id, property_name="NAME", data_type="STRING", source_column="NAME_0"),
            actor=_ADMIN_ID,
            actor_departments=_ADMIN_DEPARTMENTS,
        )
        # 连续两次更新：id 稳定、版本保持 1（不再 v1→v2→v3 递增）
        updated2 = await service.updateClass(dbSession, child.id, OntologyClassUpdate(class_alias="部门v2"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        assert updated2.id == child.id
        assert updated2.version == 1
        updated3 = await service.updateClass(dbSession, child.id, OntologyClassUpdate(class_alias="部门v3"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        assert updated3.id == child.id
        assert updated3.version == 1

        reloaded = await service.getClass(dbSession, child.id)
        assert [p.property_name for p in reloaded.properties] == ["NAME"]
        assert reloaded.parent_class_id == parent.id  # 父引用未丢失

        current = await service.listClasses(dbSession)
        dept = next(c for c in current if c.class_name == "DEPT")
        assert dept.id == child.id
        assert dept.version == 1
        assert dept.properties[0].property_name == "NAME"

    async def test_update_expired_class_rejected(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """软删除（墓碑）后的类禁止再更新。"""
        cls = await service.createClass(
            dbSession, OntologyClassCreate(class_name="GONE", source_table="T_GONE")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        await service.deleteClass(dbSession, cls.id, actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        from app.domain.exceptions import ValidationError

        with pytest.raises(ValidationError):
            await service.updateClass(dbSession, cls.id, OntologyClassUpdate(description="复活"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)

    async def test_update_rename_to_existing_name_rejected(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """原地改名撞上另一活跃类同名时拒绝（避免双活跃类/唯一约束 500）。"""
        from app.domain.exceptions import ValidationError

        a = await service.createClass(
            dbSession, OntologyClassCreate(class_name="SUPPLIER_A", source_table="T_A")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        b = await service.createClass(
            dbSession, OntologyClassCreate(class_name="SUPPLIER_B", source_table="T_B")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        with pytest.raises(ValidationError):
            await service.updateClass(
                dbSession, b.id, OntologyClassUpdate(class_name="SUPPLIER_A"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS
            )

    async def test_update_rename_to_tombstoned_name_rejected(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """改名到已软删除（墓碑）的名称同样拒绝——(class_name, version) 约束保留名称。"""
        from app.domain.exceptions import ValidationError

        a = await service.createClass(
            dbSession, OntologyClassCreate(class_name="RETIRED", source_table="T_R")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        await service.deleteClass(dbSession, a.id, actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)  # 墓碑：RETIRED
        b = await service.createClass(
            dbSession, OntologyClassCreate(class_name="LIVE", source_table="T_L")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        with pytest.raises(ValidationError):
            await service.updateClass(
                dbSession, b.id, OntologyClassUpdate(class_name="RETIRED"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS
            )

    async def test_create_after_soft_delete_rejected(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """软删除后同名单类重建被拒绝（墓碑保留名称），而非 IntegrityError 500。"""
        from app.domain.exceptions import ValidationError

        a = await service.createClass(
            dbSession, OntologyClassCreate(class_name="RESERVED", source_table="T_R")
        , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        await service.deleteClass(dbSession, a.id, actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        with pytest.raises(ValidationError):
            await service.createClass(
                dbSession, OntologyClassCreate(class_name="RESERVED", source_table="T_R2")
            , actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)


class TestUpdatedSchemaFeedsNl2Sql:
    async def test_schema_text_shows_columns_after_update(
        self, dbSession, service: OntologyService, _neutralizeNeo4j
    ) -> None:
        """端到端：更新后 schema 文本仍含列，validatePlan 通过——用户报障的直接验证。"""
        from app.domain.query_plan import QueryPlan
        from app.services.nl2sql_service import Nl2SqlService

        cls, prop = await _createClassWithProperty(dbSession, service)
        await service.updateClass(dbSession, cls.id, OntologyClassUpdate(class_alias="收货单v2"), actor=_ADMIN_ID, actor_departments=_ADMIN_DEPARTMENTS)
        current = await service.listClasses(dbSession)

        text = Nl2SqlService().buildSchemaText(current)
        assert "PRECEIPT" in text
        assert "STATUS: STRING (column=STATUS_0)" in text  # 列仍在

        # LLM 若选中该类与属性，校验必须通过（修复前会报"属性不属于选定的任何类"）
        plan = QueryPlan(target="查状态", selectedClasses=("PRECEIPT",), selectedProperties=("STATUS",))
        assert Nl2SqlService().validatePlan(plan, current) == []
