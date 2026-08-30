"""Phase 4.5 ACL 扩展集成测试（真实 PG 5433 + 完整 API 链路）。

覆盖 feat-acl-extension-3-entities 的 3 张新表：
- entity_mapping  （迁移 0026 新增 owner 列）
- data_quality_rule（迁移已包含 owner 列）
- ontology_class   （已有 object_owner 列）

ACL 行为：admin 通过；owner 部门通过；其他部门 403；空 owner 仅 admin 可改。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL + 完整 API 链路。
ASCII department tokens（HTTP header 安全）：procurement / finance / quality。
"""

from __future__ import annotations

from sqlalchemy import delete

from app.domain.models import (
    DataQualityRule,
    DataSource,
    EntityMapping,
    OntologyClass,
)


# ASCII department tokens — 与 Phase 4.5 kpi_catalog governance 集成测试一致
PROCUREMENT = "procurement"
FINANCE = "finance"
QUALITY = "quality"


def _datasource() -> DataSource:
    """DQ 规则需要 datasource_id FK；构造最小可用 DataSource 行。"""
    return DataSource(
        name="t-dq-acl",
        type="oracle",
        host="db.example.com",
        port=1521,
        database_name="db",
        username="ZJTH",
        password_encrypted="enc",
        is_active=True,
        is_default=False,
    )


class _Helper:
    """每个测试 Arrange / Assert 公共助手。"""

    @staticmethod
    async def clean(dbSession) -> None:
        """按前缀删除本测试造的数据，避免污染其他测试。"""
        await dbSession.execute(
            delete(EntityMapping).where(EntityMapping.enterprise_code.like("EM_ACL_%"))
        )
        await dbSession.execute(
            delete(DataQualityRule).where(DataQualityRule.rule_code.like("DQR_ACL_%"))
        )
        await dbSession.execute(
            delete(OntologyClass).where(OntologyClass.class_name.like("CLS_ACL_%"))
        )
        await dbSession.commit()

    @staticmethod
    async def createDatasource(dbSession) -> DataSource:
        ds = _datasource()
        dbSession.add(ds)
        await dbSession.commit()
        await dbSession.refresh(ds)
        return ds


# ===========================================================================
# entity_mapping
# ===========================================================================


class TestEntityMappingAcl:
    """entity_mapping PUT/DELETE 走 AclService：admin / owner 部门通过；其他 403。"""

    async def _create(
        self,
        client,
        code: str,
        dept: str | None,
    ) -> dict:
        """Create entity_mapping.

        Phase 4.5：DTO 不再接受 owner（防越权）；服务端从 actor.departments[0]
        派生 owner。dept=None → actor 无部门 → owner 留空（仅 admin 可改）。
        """
        headers = {"X-User-Id": "creator"}
        if dept:
            headers["X-User-Departments"] = dept
        resp = await client.post(
            "/api/v1/entity-mappings",
            json={
                "entityType": "SUPPLIER",
                "enterpriseKey": 100001,
                "enterpriseCode": code,
                "sourceSystem": "ERP",
                "sourceKey": "V000001",
                "sourceCode": "V000001",
                "matchRule": "MDM_MASTER",
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test_update_owner_dept_passes(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        m = await self._create(client, "EM_ACL_A", PROCUREMENT)
        resp = await client.put(
            f"/api/v1/entity-mappings/{m['id']}",
            json={"sourceCode": "V000001-X"},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["sourceCode"] == "V000001-X"

    async def test_update_other_dept_returns_403(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        m = await self._create(client, "EM_ACL_B", PROCUREMENT)
        resp = await client.put(
            f"/api/v1/entity-mappings/{m['id']}",
            json={"sourceCode": "X"},
            headers={"X-User-Id": "bob", "X-User-Departments": FINANCE},
        )
        assert resp.status_code == 403
        assert "无权修改" in resp.json().get("error", "")

    async def test_update_admin_any_owner_passes(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        m = await self._create(client, "EM_ACL_C", PROCUREMENT)
        resp = await client.put(
            f"/api/v1/entity-mappings/{m['id']}",
            json={"sourceCode": "admin改"},
            headers={"X-User-Id": "root", "X-User-Roles": "admin"},
        )
        assert resp.status_code == 200, resp.text

    async def test_delete_other_dept_returns_403(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        m = await self._create(client, "EM_ACL_D", QUALITY)
        resp = await client.delete(
            f"/api/v1/entity-mappings/{m['id']}",
            headers={"X-User-Id": "bob", "X-User-Departments": FINANCE},
        )
        assert resp.status_code == 403

    async def test_delete_owner_dept_passes(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        m = await self._create(client, "EM_ACL_E", QUALITY)
        resp = await client.delete(
            f"/api/v1/entity-mappings/{m['id']}",
            headers={"X-User-Id": "alice", "X-User-Departments": QUALITY},
        )
        assert resp.status_code == 204

    async def test_empty_owner_only_admin_can_modify(
        self, client, dbSession
    ) -> None:
        """owner 为空 + 非 admin → 403；admin → 200。"""
        await _Helper.clean(dbSession)
        m = await self._create(client, "EM_ACL_F", None)
        # 非 admin + 有部门 → 403
        resp = await client.put(
            f"/api/v1/entity-mappings/{m['id']}",
            json={"sourceCode": "X"},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        assert resp.status_code == 403
        # admin → 200
        resp2 = await client.put(
            f"/api/v1/entity-mappings/{m['id']}",
            json={"sourceCode": "admin改"},
            headers={"X-User-Id": "root", "X-User-Roles": "admin"},
        )
        assert resp2.status_code == 200


# ===========================================================================
# data_quality_rule
# ===========================================================================


class TestDataQualityRuleAcl:
    """data_quality_rule PUT/DELETE 走 AclService。"""

    async def _create(self, client, dsId: int, dept: str | None) -> dict:
        """Create data_quality_rule.

        Phase 4.5：DTO 不再接受 owner；服务端从 actor.departments[0] 派生。
        dept=None → owner 留空 → 仅 admin 可改。
        """
        headers = {"X-User-Id": "creator"}
        if dept:
            headers["X-User-Departments"] = dept
        resp = await client.post(
            "/api/v1/data-quality/rules",
            json={
                "ruleName": "测试规则",
                "ruleCode": f"DQR_ACL_{dsId}",
                "datasourceId": dsId,
                "targetTable": "PORDER",
                "ruleType": "COMPLETENESS",
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test_update_owner_dept_passes(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        ds = await _Helper.createDatasource(dbSession)
        rule = await self._create(client, ds.id, PROCUREMENT)
        resp = await client.put(
            f"/api/v1/data-quality/rules/{rule['id']}",
            json={"ruleName": "改后"},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ruleName"] == "改后"

    async def test_update_other_dept_returns_403(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        ds = await _Helper.createDatasource(dbSession)
        rule = await self._create(client, ds.id, PROCUREMENT)
        resp = await client.put(
            f"/api/v1/data-quality/rules/{rule['id']}",
            json={"ruleName": "X"},
            headers={"X-User-Id": "bob", "X-User-Departments": FINANCE},
        )
        assert resp.status_code == 403

    async def test_disable_other_dept_returns_403(self, client, dbSession) -> None:
        """DELETE 是软删除（disableRule）；其他部门拒绝。"""
        await _Helper.clean(dbSession)
        ds = await _Helper.createDatasource(dbSession)
        rule = await self._create(client, ds.id, QUALITY)
        resp = await client.delete(
            f"/api/v1/data-quality/rules/{rule['id']}",
            headers={"X-User-Id": "bob", "X-User-Departments": FINANCE},
        )
        assert resp.status_code == 403

    async def test_update_admin_any_owner_passes(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        ds = await _Helper.createDatasource(dbSession)
        rule = await self._create(client, ds.id, FINANCE)
        resp = await client.put(
            f"/api/v1/data-quality/rules/{rule['id']}",
            json={"ruleName": "admin改"},
            headers={"X-User-Id": "root", "X-User-Roles": "admin"},
        )
        assert resp.status_code == 200, resp.text


# ===========================================================================
# ontology_class
# ===========================================================================


class TestOntologyClassAcl:
    """ontology_class PUT/DELETE 走 AclService（基于 object_owner 字段）。"""

    async def _create(self, client, name: str, dept: str | None) -> dict:
        """Create ontology_class.

        Phase 4.5：DTO 不再接受 objectOwner；服务端从 actor.departments[0] 派生。
        """
        headers = {"X-User-Id": "creator"}
        if dept:
            headers["X-User-Departments"] = dept
        resp = await client.post(
            "/api/v1/ontology/classes",
            json={"className": name},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test_update_owner_dept_passes(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        cls = await self._create(client, "CLS_ACL_A", PROCUREMENT)
        resp = await client.put(
            f"/api/v1/ontology/classes/{cls['id']}",
            json={"classAlias": "改后"},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["classAlias"] == "改后"

    async def test_update_other_dept_returns_403(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        cls = await self._create(client, "CLS_ACL_B", PROCUREMENT)
        resp = await client.put(
            f"/api/v1/ontology/classes/{cls['id']}",
            json={"classAlias": "X"},
            headers={"X-User-Id": "bob", "X-User-Departments": FINANCE},
        )
        assert resp.status_code == 403

    async def test_delete_admin_any_owner_passes(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        cls = await self._create(client, "CLS_ACL_C", PROCUREMENT)
        resp = await client.delete(
            f"/api/v1/ontology/classes/{cls['id']}",
            headers={"X-User-Id": "root", "X-User-Roles": "admin"},
        )
        assert resp.status_code == 204

    async def test_delete_other_dept_returns_403(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        cls = await self._create(client, "CLS_ACL_D", QUALITY)
        resp = await client.delete(
            f"/api/v1/ontology/classes/{cls['id']}",
            headers={"X-User-Id": "bob", "X-User-Departments": FINANCE},
        )
        assert resp.status_code == 403