"""Agent Registry API 集成测试（真实 PostgreSQL + 完整 API 链路，Phase 6.1）。

验证 HTTP 契约（camelCase JSON）：
- GET    /api/v1/agents                       列表（按 status / dataDomain 过滤）
- GET    /api/v1/agents/{agent_code}          详情 + 嵌套 policies
- POST   /api/v1/agents                       创建（201）含 policies
- PUT    /api/v1/agents/{agent_code}          更新
- DELETE /api/v1/agents/{agent_code}          软删除（status → deprecated）
- GET    /api/v1/agents/{agent_code}/policies 列表
- POST   /api/v1/agents/{agent_code}/policies 追加策略
- PUT    /api/v1/agents/{agent_code}/policies/{pid}  更新策略
- DELETE /api/v1/agents/{agent_code}/policies/{pid}  删除策略

ACL：
- 读：所有登录用户（含 stub 默认 user）
- 写：admin / owner；非 admin + 非 owner → 403

测试在真实 PG 5433 上运行（qa_metadata_test 数据库）。
"""

from __future__ import annotations

from sqlalchemy import text

from app.domain.enums import AgentPermission, AgentStatus


ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


def _admin() -> dict[str, str]:
    return ADMIN_HEADERS


def _owner() -> dict[str, str]:
    """owner 部门成员：非 admin 角色但属 procurement 部门。"""
    return {
        "X-User-Id": "test-procurement",
        "X-User-Roles": "user",
        "X-User-Departments": "procurement",
    }


def _stranger() -> dict[str, str]:
    """非 owner 非 admin：user 角色 + finance 部门。"""
    return {
        "X-User-Id": "test-finance",
        "X-User-Roles": "user",
        "X-User-Departments": "finance",
    }


class TestAgentRegistryMigration:
    async def test_tables_and_columns_after_migration(self, dbSession) -> None:
        """Alembic upgrade head 后两张表 + 关键列齐全。"""
        for tbl in ("agent_definition", "agent_access_policy"):
            result = await dbSession.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name = '{tbl}'"
                )
            )
            cols = {row[0] for row in result}
            assert cols, f"missing table {tbl}"

        result = await dbSession.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'agent_definition'"
            )
        )
        cols = {row[0] for row in result}
        for c in (
            "id",
            "agent_code",
            "agent_name",
            "description",
            "trigger_type",
            "response_latency",
            "data_domains",
            "data_layers",
            "status",
            "owner",
            "version",
            "created_time",
            "updated_time",
        ):
            assert c in cols, f"missing column agent_definition.{c}"

        result = await dbSession.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'agent_access_policy'"
            )
        )
        cols = {row[0] for row in result}
        for c in (
            "id",
            "agent_id",
            "data_object",
            "permission",
            "data_layer",
            "notes",
            "created_time",
        ):
            assert c in cols, f"missing column agent_access_policy.{c}"


class TestAgentRegistryApi:
    async def test_create_get_roundtrip_with_policies(self, client) -> None:
        """创建 Agent（含 2 条 policy）→ GET 详情 → 字段一致。"""
        resp = await client.post(
            "/api/v1/agents",
            headers=_admin(),
            json={
                "agentCode": "TEST_SUPPLIER_RISK",
                "agentName": "供应商风险 Agent",
                "description": "用于 Phase 5.4 风险评估",
                "triggerType": "user_question",
                "responseLatency": "realtime",
                "dataDomains": ["PROCUREMENT"],
                "dataLayers": ["FEATURE"],
                "status": "active",
                "version": "v1.0",
                "policies": [
                    {
                        "dataObject": "SUPPLIER",
                        "permission": "read",
                        "dataLayer": None,
                        "notes": "可读供应商主数据",
                    },
                    {
                        "dataObject": "FEATURE_VALUE",
                        "permission": "masked_read",
                        "dataLayer": "FEATURE",
                        "notes": "特征值敏感字段脱敏",
                    },
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["agentCode"] == "TEST_SUPPLIER_RISK"
        assert body["status"] == "active"
        assert body["dataDomains"] == ["PROCUREMENT"]
        assert len(body["policies"]) == 2
        assert body["policies"][0]["dataObject"] == "SUPPLIER"
        # admin 用户 stub 默认 departments=() → owner=None
        assert body["owner"] is None

        # GET 详情
        detail = await client.get(
            "/api/v1/agents/TEST_SUPPLIER_RISK", headers=_admin()
        )
        assert detail.status_code == 200
        assert detail.json()["agentCode"] == "TEST_SUPPLIER_RISK"

    async def test_create_duplicate_code_returns_409(self, client) -> None:
        payload = {
            "agentCode": "DUP_AGENT_API",
            "agentName": "重复测试",
            "triggerType": "user_question",
            "responseLatency": "realtime",
        }
        r1 = await client.post("/api/v1/agents", headers=_admin(), json=payload)
        assert r1.status_code == 201
        r2 = await client.post("/api/v1/agents", headers=_admin(), json=payload)
        assert r2.status_code == 409, r2.text

    async def test_create_with_lowercase_code_rejected_by_validation(self, client) -> None:
        """agentCode 强制 ^[A-Z][A-Z0-9_]*$ → 小写 422。"""
        resp = await client.post(
            "/api/v1/agents",
            headers=_admin(),
            json={
                "agentCode": "lower_case",
                "agentName": "小写",
                "triggerType": "user_question",
                "responseLatency": "realtime",
            },
        )
        assert resp.status_code == 422

    async def test_get_unknown_code_returns_404(self, client) -> None:
        resp = await client.get(
            "/api/v1/agents/UNKNOWN_AGENT_CODE", headers=_admin()
        )
        assert resp.status_code == 404

    async def test_update_by_owner_department_succeeds(self, client) -> None:
        """owner=采购部 创建 → 采购部成员可更新（Phase 4.5 ACL）。"""
        # owner=采购部 创建：用 procurement 用户（departments=采购部）
        create = await client.post(
            "/api/v1/agents",
            headers=_owner(),
            json={
                "agentCode": "OWNER_UPDATE_AGENT",
                "agentName": "owner 可改",
                "triggerType": "user_question",
                "responseLatency": "realtime",
            },
        )
        assert create.status_code == 201, create.text

        # 财务部（stranger）尝试更新 → 403
        forbidden = await client.put(
            "/api/v1/agents/OWNER_UPDATE_AGENT",
            headers=_stranger(),
            json={"agentName": "财务部想改"},
        )
        assert forbidden.status_code == 403

        # 采购部（owner）更新 → 200
        ok = await client.put(
            "/api/v1/agents/OWNER_UPDATE_AGENT",
            headers=_owner(),
            json={"agentName": "采购部改名", "status": "active"},
        )
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["agentName"] == "采购部改名"
        assert body["status"] == "active"

    async def test_list_filter_by_status_and_domain(self, client) -> None:
        # 创建 active（PROCUREMENT）和 draft（INVENTORY）
        await client.post(
            "/api/v1/agents",
            headers=_admin(),
            json={
                "agentCode": "FILTER_ACTIVE",
                "agentName": "active",
                "triggerType": "user_question",
                "responseLatency": "realtime",
                "status": "active",
                "dataDomains": ["PROCUREMENT"],
            },
        )
        await client.post(
            "/api/v1/agents",
            headers=_admin(),
            json={
                "agentCode": "FILTER_DRAFT",
                "agentName": "draft",
                "triggerType": "user_question",
                "responseLatency": "realtime",
                "status": "draft",
                "dataDomains": ["INVENTORY"],
            },
        )

        # status=active
        resp = await client.get("/api/v1/agents?status=active", headers=_admin())
        assert resp.status_code == 200
        codes = [a["agentCode"] for a in resp.json()]
        assert "FILTER_ACTIVE" in codes
        assert "FILTER_DRAFT" not in codes

        # dataDomain=PROCUREMENT
        resp = await client.get(
            "/api/v1/agents?dataDomain=PROCUREMENT", headers=_admin()
        )
        codes = [a["agentCode"] for a in resp.json()]
        assert "FILTER_ACTIVE" in codes
        assert "FILTER_DRAFT" not in codes

    async def test_deprecate_returns_deprecated_status(self, client) -> None:
        await client.post(
            "/api/v1/agents",
            headers=_admin(),
            json={
                "agentCode": "DEP_API_AGENT",
                "agentName": "待停用",
                "triggerType": "user_question",
                "responseLatency": "realtime",
                "status": "active",
            },
        )
        resp = await client.delete(
            "/api/v1/agents/DEP_API_AGENT", headers=_admin()
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deprecated"

    async def test_policy_crud_roundtrip(self, client) -> None:
        # 先建一个 agent
        await client.post(
            "/api/v1/agents",
            headers=_admin(),
            json={
                "agentCode": "POLICY_API_AGENT",
                "agentName": "policy 测试",
                "triggerType": "user_question",
                "responseLatency": "realtime",
            },
        )

        # POST policy
        add = await client.post(
            "/api/v1/agents/POLICY_API_AGENT/policies",
            headers=_admin(),
            json={
                "dataObject": "SUPPLIER",
                "permission": "read",
                "dataLayer": None,
            },
        )
        assert add.status_code == 201, add.text
        policy_id = add.json()["id"]

        # GET policies
        lst = await client.get(
            "/api/v1/agents/POLICY_API_AGENT/policies", headers=_admin()
        )
        assert lst.status_code == 200
        assert len(lst.json()) == 1

        # PUT policy
        upd = await client.put(
            f"/api/v1/agents/POLICY_API_AGENT/policies/{policy_id}",
            headers=_admin(),
            json={"permission": "forbidden_write", "notes": "调整"},
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["permission"] == "forbidden_write"

        # DELETE policy
        delete = await client.delete(
            f"/api/v1/agents/POLICY_API_AGENT/policies/{policy_id}",
            headers=_admin(),
        )
        assert delete.status_code == 204

        # 列表应为空
        empty = await client.get(
            "/api/v1/agents/POLICY_API_AGENT/policies", headers=_admin()
        )
        assert empty.json() == []

    async def test_policy_duplicate_returns_409(self, client) -> None:
        await client.post(
            "/api/v1/agents",
            headers=_admin(),
            json={
                "agentCode": "POLICY_DUP_AGENT",
                "agentName": "policy 重复",
                "triggerType": "user_question",
                "responseLatency": "realtime",
            },
        )
        p = {
            "dataObject": "SUPPLIER",
            "permission": "read",
            "dataLayer": "DWS",
        }
        r1 = await client.post(
            "/api/v1/agents/POLICY_DUP_AGENT/policies", headers=_admin(), json=p
        )
        assert r1.status_code == 201
        r2 = await client.post(
            "/api/v1/agents/POLICY_DUP_AGENT/policies", headers=_admin(), json=p
        )
        assert r2.status_code == 409

    async def test_get_unknown_agent_returns_404(self, client) -> None:
        resp = await client.get(
            "/api/v1/agents/NO_SUCH_AGENT/policies", headers=_admin()
        )
        assert resp.status_code == 404