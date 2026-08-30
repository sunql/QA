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
        resp = await client.delete(f"/api/v1/data-quality/rules/{rid}")
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