"""数据质量规则接口集成测试（真实 PostgreSQL + 完整 API 链路）。

验证 HTTP 契约（camelCase JSON）：
- GET    /api/v1/data-quality/rules
- GET    /api/v1/data-quality/rules/{id}
- POST   /api/v1/data-quality/rules
- PUT    /api/v1/data-quality/rules/{id}
- DELETE /api/v1/data-quality/rules/{id}

测试在真实 PG 5433 上运行（test_qa_metadata_test 数据库），每用例走 TestClient
+ DependencyOverrides，无 HTTP 鉴权（Phase 1.1 暂不强制登录，与既有 term_dictionary
保持一致）。

每测试通过 conftest 的 `client` 拿到真实 PG TestClient；数据源由 helper
`_createTestDatasource()` 在用例内就地创建，避免依赖外部 fixture 顺序。
"""

from __future__ import annotations

from typing import Any


_DATASOURCE_PAYLOAD: dict[str, Any] = {
    "name": "dq-test-ds",
    "type": "postgresql",
    "host": "db.example.com",
    "port": 5432,
    "databaseName": "appdb",
    "username": "u",
    "password": "p",
    "description": "DQ rule eval test datasource",
    "isActive": True,
    "isDefault": False,
}


async def _createTestDatasource(client, **overrides) -> int:
    """就地创建数据源并返回 id。TRUNCATE 会清库，所以每个用例都先建一个。"""
    payload = dict(_DATASOURCE_PAYLOAD)
    payload.update(overrides)
    resp = await client.post("/api/v1/datasources", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestDataQualityRuleApi:
    async def test_list_empty(self, client) -> None:
        resp = await client.get("/api/v1/data-quality/rules")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_and_list(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        payload = {
            "ruleName": "订单数量必须大于 0",
            "ruleCode": "PORDER_QTY_POSITIVE",
            "datasourceId": ds_id,
            "targetTable": "PORDER",
            "targetColumn": "ORDER_QTY",
            "ruleType": "VALIDITY",
            "ruleExpression": "ORDER_QTY > 0",
            "threshold": "99.50",
            "severity": "HIGH",
            "owner": "采购部",
        }
        resp = await client.post("/api/v1/data-quality/rules", json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["ruleName"] == "订单数量必须大于 0"
        assert body["ruleCode"] == "PORDER_QTY_POSITIVE"
        assert body["datasourceId"] == ds_id
        assert body["ruleType"] == "VALIDITY"
        assert body["severity"] == "HIGH"
        assert body["isEnabled"] is True
        assert body["version"] == "v1.0"
        assert body["id"] is not None

        listing = await client.get("/api/v1/data-quality/rules")
        assert listing.status_code == 200
        assert len(listing.json()) == 1

    async def test_create_duplicate_code_conflict(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        payload = {
            "ruleName": "规则 A",
            "ruleCode": "DUP_CODE",
            "datasourceId": ds_id,
            "targetTable": "T1",
            "ruleType": "COMPLETENESS",
        }
        first = await client.post("/api/v1/data-quality/rules", json=payload)
        assert first.status_code == 201
        dup = await client.post("/api/v1/data-quality/rules", json=payload)
        assert dup.status_code == 422

    async def test_create_invalid_code_format_rejected(self, client) -> None:
        """rule_code 必须匹配 ^[A-Z][A-Z0-9_]*$，小写应被 Pydantic 拒绝。"""
        ds_id = await _createTestDatasource(client)
        payload = {
            "ruleName": "规则",
            "ruleCode": "lower_case",
            "datasourceId": ds_id,
            "targetTable": "T1",
            "ruleType": "COMPLETENESS",
        }
        resp = await client.post("/api/v1/data-quality/rules", json=payload)
        assert resp.status_code == 422

    async def test_create_threshold_out_of_range_rejected(self, client) -> None:
        """threshold > 100 应被 Pydantic 拒绝。"""
        ds_id = await _createTestDatasource(client)
        payload = {
            "ruleName": "规则",
            "ruleCode": "BAD_THRESH",
            "datasourceId": ds_id,
            "targetTable": "T1",
            "ruleType": "COMPLETENESS",
            "threshold": "150.00",
        }
        resp = await client.post("/api/v1/data-quality/rules", json=payload)
        assert resp.status_code == 422

    async def test_get_by_id(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        created = await client.post(
            "/api/v1/data-quality/rules",
            json={
                "ruleName": "规则",
                "ruleCode": "GET_BY_ID",
                "datasourceId": ds_id,
                "targetTable": "PORDER",
                "ruleType": "COMPLETENESS",
            },
        )
        rid = created.json()["id"]
        resp = await client.get(f"/api/v1/data-quality/rules/{rid}")
        assert resp.status_code == 200
        assert resp.json()["ruleCode"] == "GET_BY_ID"

    async def test_get_not_found(self, client) -> None:
        resp = await client.get("/api/v1/data-quality/rules/999999")
        assert resp.status_code == 404

    async def test_update_partial(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        created = await client.post(
            "/api/v1/data-quality/rules",
            json={
                "ruleName": "规则",
                "ruleCode": "UPDATE_PARTIAL",
                "datasourceId": ds_id,
                "targetTable": "PORDER",
                "ruleType": "COMPLETENESS",
            },
        )
        rid = created.json()["id"]
        resp = await client.put(
            f"/api/v1/data-quality/rules/{rid}",
            json={"severity": "HIGH", "threshold": "99.00"},
        headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["severity"] == "HIGH"
        assert body["threshold"] == "99.00"
        # 未修改字段保持不变
        assert body["ruleCode"] == "UPDATE_PARTIAL"

    async def test_delete_soft(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        created = await client.post(
            "/api/v1/data-quality/rules",
            json={
                "ruleName": "规则",
                "ruleCode": "SOFT_DELETE",
                "datasourceId": ds_id,
                "targetTable": "T1",
                "ruleType": "COMPLETENESS",
            },
        )
        rid = created.json()["id"]
        resp = await client.delete(f"/api/v1/data-quality/rules/{rid}", headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"})
        assert resp.status_code == 204

        # is_enabled=false 后默认列表不返回（enabledOnly=None 时仍返回），按 enabledOnly=true 过滤验证
        listing = await client.get(
            "/api/v1/data-quality/rules?enabledOnly=true"
        )
        assert all(r["id"] != rid for r in listing.json())

        detail = await client.get(f"/api/v1/data-quality/rules/{rid}")
        assert detail.status_code == 200
        assert detail.json()["isEnabled"] is False

    async def test_filter_by_type_and_table(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        await client.post(
            "/api/v1/data-quality/rules",
            json={
                "ruleName": "A",
                "ruleCode": "FILTER_A",
                "datasourceId": ds_id,
                "targetTable": "PORDER",
                "ruleType": "VALIDITY",
            },
        )
        await client.post(
            "/api/v1/data-quality/rules",
            json={
                "ruleName": "B",
                "ruleCode": "FILTER_B",
                "datasourceId": ds_id,
                "targetTable": "PRECEIPT",
                "ruleType": "COMPLETENESS",
            },
        )

        by_type = await client.get(
            "/api/v1/data-quality/rules?ruleType=VALIDITY"
        )
        assert all(r["ruleType"] == "VALIDITY" for r in by_type.json())

        by_table = await client.get(
            "/api/v1/data-quality/rules?targetTable=PRECEIPT"
        )
        assert len(by_table.json()) == 1
        assert by_table.json()[0]["ruleCode"] == "FILTER_B"

    async def test_owner_dept_can_update_and_other_dept_blocked(self, client) -> None:
        """Phase 4.5 ACL：owner 部门可改；跨部门 403；admin 通过。

        创建时通过 X-User-Departments 头让 service 派生 owner = procurement，
        然后用相同部门 PUT（200）+ 跨部门 PUT（403）走完整 ACL 链路。
        """
        ds_id = await _createTestDatasource(client)
        created = await client.post(
            "/api/v1/data-quality/rules",
            json={
                "ruleName": "owner部门",
                "ruleCode": "OWNER_DEPT_RULE",
                "datasourceId": ds_id,
                "targetTable": "PORDER",
                "ruleType": "COMPLETENESS",
            },
            headers={"X-User-Departments": "procurement"},
        )
        assert created.status_code == 201, created.text
        rid = created.json()["id"]
        # 服务端已按部门派生 owner → 读回应是 procurement
        assert created.json()["owner"] == "procurement"

        # owner 部门 PUT → 200
        okPut = await client.put(
            f"/api/v1/data-quality/rules/{rid}",
            json={"ruleName": "owner改后"},
            headers={"X-User-Departments": "procurement"},
        )
        assert okPut.status_code == 200, okPut.text
        assert okPut.json()["ruleName"] == "owner改后"

        # 跨部门 PUT → 403
        denied = await client.put(
            f"/api/v1/data-quality/rules/{rid}",
            json={"ruleName": "finance想改"},
            headers={
                "X-User-Id": "fin-user",
                "X-User-Roles": "user",
                "X-User-Departments": "finance",
            },
        )
        assert denied.status_code == 403

        # admin 任意改 → 200（覆盖前面 owner=procurement）
        adminPut = await client.put(
            f"/api/v1/data-quality/rules/{rid}",
            json={"ruleName": "admin改"},
            headers={"X-User-Roles": "admin"},
        )
        assert adminPut.status_code == 200, adminPut.text


# ---------------------------------------------------------------------------
# feat-dq-rule-list-filters — listRules 6 字段过滤 + /rules/options 端点
# ---------------------------------------------------------------------------


async def _seedRule(
    client,
    *,
    ruleName: str = "默认规则",
    ruleCode: str = "DEFAULT_RULE",
    datasourceId: int,
    targetTable: str = "T_DEFAULT",
    targetColumn: str | None = None,
    ruleType: str = "VALIDITY",
    ruleExpression: str | None = None,
    threshold: str | None = None,
    severity: str = "MEDIUM",
    isEnabled: bool = True,
) -> dict[str, Any]:
    """就地创建一条 data_quality_rule，返回响应 JSON。"""
    payload: dict[str, Any] = {
        "ruleName": ruleName,
        "ruleCode": ruleCode,
        "datasourceId": datasourceId,
        "targetTable": targetTable,
        "ruleType": ruleType,
        "severity": severity,
        "isEnabled": isEnabled,
    }
    if targetColumn is not None:
        payload["targetColumn"] = targetColumn
    if ruleExpression is not None:
        payload["ruleExpression"] = ruleExpression
    if threshold is not None:
        payload["threshold"] = threshold
    resp = await client.post("/api/v1/data-quality/rules", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestDataQualityRuleFilters:
    """feat-dq-rule-list-filters：5 字段 + 模糊筛选 + /rules/options 端点。"""

    async def test_list_rules_filter_by_rule_name_ilike(
        self, client,
    ) -> None:
        """ruleName=PO 应只返 name 含 PO 的规则（ILIKE 模糊）。"""
        ds_id = await _createTestDatasource(client)
        await _seedRule(client, ruleName="PO订单数量校验", ruleCode="R_PO_1",
                        datasourceId=ds_id, targetTable="PORDER")
        await _seedRule(client, ruleName="供应商合规校验", ruleCode="R_SUP_1",
                        datasourceId=ds_id, targetTable="SPL")
        resp = await client.get(
            "/api/v1/data-quality/rules", params={"ruleName": "PO"}
        )
        assert resp.status_code == 200
        names = [r["ruleName"] for r in resp.json()]
        assert "PO订单数量校验" in names
        assert "供应商合规校验" not in names

    async def test_list_rules_filter_by_datasource_id(
        self, client,
    ) -> None:
        """datasourceId=X 只返该数据源下的规则。"""
        ds1 = await _createTestDatasource(client, name="ds-1")
        ds2 = await _createTestDatasource(client, name="ds-2")
        await _seedRule(client, ruleCode="R_DS1", datasourceId=ds1)
        await _seedRule(client, ruleCode="R_DS2", datasourceId=ds2)
        resp = await client.get(
            "/api/v1/data-quality/rules", params={"datasourceId": ds1}
        )
        codes = [r["ruleCode"] for r in resp.json()]
        assert codes == ["R_DS1"]

    async def test_list_rules_filter_by_severity(self, client) -> None:
        """severity=HIGH 只返 HIGH 规则。"""
        ds_id = await _createTestDatasource(client)
        await _seedRule(client, ruleCode="R_HIGH", datasourceId=ds_id,
                        severity="HIGH")
        await _seedRule(client, ruleCode="R_LOW", datasourceId=ds_id,
                        severity="LOW")
        resp = await client.get(
            "/api/v1/data-quality/rules", params={"severity": "HIGH"}
        )
        codes = [r["ruleCode"] for r in resp.json()]
        assert codes == ["R_HIGH"]

    async def test_list_rules_filter_enabled_three_states(
        self, client,
    ) -> None:
        """enabled 三态：all / enabled / disabled 各自过滤正确。"""
        ds_id = await _createTestDatasource(client)
        await _seedRule(client, ruleCode="R_ON", datasourceId=ds_id,
                        isEnabled=True)
        await _seedRule(client, ruleCode="R_OFF", datasourceId=ds_id,
                        isEnabled=False)

        # all：全返
        allResp = await client.get(
            "/api/v1/data-quality/rules", params={"enabled": "all"}
        )
        codes_all = sorted(r["ruleCode"] for r in allResp.json())
        assert codes_all == ["R_OFF", "R_ON"]

        # enabled：只返启用
        onResp = await client.get(
            "/api/v1/data-quality/rules", params={"enabled": "enabled"}
        )
        codes_on = sorted(r["ruleCode"] for r in onResp.json())
        assert codes_on == ["R_ON"]

        # disabled：只返未启用
        offResp = await client.get(
            "/api/v1/data-quality/rules", params={"enabled": "disabled"}
        )
        codes_off = sorted(r["ruleCode"] for r in offResp.json())
        assert codes_off == ["R_OFF"]

    async def test_list_rules_target_table_ilike(self, client) -> None:
        """targetTable=PO 应 ILIKE 匹配 PORDER / PO_HEADER 等。"""
        ds_id = await _createTestDatasource(client)
        await _seedRule(client, ruleCode="R1", datasourceId=ds_id,
                        targetTable="PORDER")
        await _seedRule(client, ruleCode="R2", datasourceId=ds_id,
                        targetTable="PO_HEADER")
        await _seedRule(client, ruleCode="R3", datasourceId=ds_id,
                        targetTable="SPL")
        resp = await client.get(
            "/api/v1/data-quality/rules", params={"targetTable": "PO"}
        )
        codes = sorted(r["ruleCode"] for r in resp.json())
        assert codes == ["R1", "R2"]

    async def test_list_rules_combined_filters(self, client) -> None:
        """多条件 AND 叠加。"""
        ds1 = await _createTestDatasource(client, name="ds-1")
        ds2 = await _createTestDatasource(client, name="ds-2")
        await _seedRule(client, ruleName="PO订单HIGH",
                        ruleCode="R1", datasourceId=ds1,
                        targetTable="PORDER", severity="HIGH")
        await _seedRule(client, ruleName="PO订单LOW",
                        ruleCode="R2", datasourceId=ds1,
                        targetTable="PORDER", severity="LOW")
        await _seedRule(client, ruleName="PO订单HIGH别库",
                        ruleCode="R3", datasourceId=ds2,
                        targetTable="PORDER", severity="HIGH")
        resp = await client.get(
            "/api/v1/data-quality/rules",
            params={
                "ruleName": "PO",
                "datasourceId": ds1,
                "severity": "HIGH",
                "targetTable": "PO",
            },
        )
        codes = [r["ruleCode"] for r in resp.json()]
        assert codes == ["R1"]

    async def test_list_options_returns_distinct_values(
        self, client,
    ) -> None:
        """GET /rules/options 返回 DISTINCT rule_name/target_table + 全量 datasource + 静态 severities。"""
        ds_id = await _createTestDatasource(client, name="opts-ds")
        await _seedRule(client, ruleName="订单数量>0", ruleCode="R_OPTS_1",
                        datasourceId=ds_id, targetTable="PORDER",
                        severity="HIGH")
        await _seedRule(client, ruleName="供应商去重", ruleCode="R_OPTS_2",
                        datasourceId=ds_id, targetTable="SPL",
                        severity="LOW")
        resp = await client.get("/api/v1/data-quality/rules/options")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # DISTINCT rule_names
        assert "订单数量>0" in body["ruleNames"]
        assert "供应商去重" in body["ruleNames"]
        # DISTINCT target_tables
        assert "PORDER" in body["targetTables"]
        assert "SPL" in body["targetTables"]
        # 全量 active datasource（至少含 opts-ds）
        ds_ids = {d["id"] for d in body["datasourceIds"]}
        assert ds_id in ds_ids
        # 静态 severities 全集
        assert set(body["severities"]) == {"HIGH", "MEDIUM", "LOW", "INFO"}