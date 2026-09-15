"""DQ 规则结构化参数 API 集成测试（真实 PostgreSQL + 完整 API 链路）。

验证 HTTP 契约（camelCase JSON）：
- GET    /api/v1/dq-rule-params/rules
- GET    /api/v1/dq-rule-params/rules/{id}
- POST   /api/v1/dq-rule-params/rules
- PUT    /api/v1/dq-rule-params/rules/{id}
- DELETE /api/v1/dq-rule-params/rules/{id}

每测试通过 conftest 的 `client` 拿到真实 PG TestClient；数据源由 helper
`_createTestDatasource()` 在用例内就地创建，避免依赖外部 fixture 顺序。
"""

from __future__ import annotations

from typing import Any

import pytest


_DATASOURCE_PAYLOAD: dict[str, Any] = {
    "name": "dq-rule-params-test-ds",
    "type": "postgresql",
    "host": "db.example.com",
    "port": 5432,
    "databaseName": "appdb",
    "username": "u",
    "password": "p",
    "description": "DQ rule params test datasource",
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


@pytest.mark.integration
class TestCreateStructured:
    """结构化模式 create：params → rule_expression 编译。"""

    async def test_validity_range_end_to_end(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        payload = {
            "ruleCode": "DQ_INT_RANGE",
            "ruleName": "order_qty range",
            "ruleType": "VALIDITY",
            "targetTable": "PO_LINE",
            "targetColumn": "ORDER_QTY",
            "threshold": "95",
            "severity": "MEDIUM",
            "datasourceId": ds_id,
            "ruleParams": {"kind": "range", "min": 0, "max": 100},
        }
        resp = await client.post("/api/v1/dq-rule-params/rules", json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["configMode"] == "structured"
        assert body["ruleParams"] == {"kind": "range", "min": 0, "max": 100}
        assert "BETWEEN" in body["ruleExpression"]
        assert "ORDER_QTY" in body["ruleExpression"]

    async def test_unique_compiles_expression(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        payload = {
            "ruleCode": "DQ_UNIQUE",
            "ruleName": "supplier code unique",
            "ruleType": "UNIQUENESS",
            "targetTable": "SUPPLIER",
            "targetColumn": "SUPPLIER_CODE",
            "threshold": "100",
            "severity": "HIGH",
            "datasourceId": ds_id,
            "ruleParams": {"kind": "unique"},
        }
        resp = await client.post("/api/v1/dq-rule-params/rules", json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["configMode"] == "structured"
        assert body["ruleParams"] == {"kind": "unique"}
        assert "SUPPLIER_CODE" in body["ruleExpression"]

    async def test_compare_compiles_expression(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        payload = {
            "ruleCode": "DQ_COMPARE",
            "ruleName": "qty positive",
            "ruleType": "VALIDITY",
            "targetTable": "PO_LINE",
            "targetColumn": "ORDER_QTY",
            "threshold": "99",
            "severity": "HIGH",
            "datasourceId": ds_id,
            "ruleParams": {"kind": "compare", "op": ">=", "value": 0},
        }
        resp = await client.post("/api/v1/dq-rule-params/rules", json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["configMode"] == "structured"
        assert "ORDER_QTY" in body["ruleExpression"]


@pytest.mark.integration
class TestCreateCustom:
    """自定义表达式 create：直接透传 rule_expression。"""

    async def test_validity_expression(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        payload = {
            "ruleCode": "DQ_INT_CUSTOM",
            "ruleName": "custom expr",
            "ruleType": "VALIDITY",
            "targetTable": "PO_LINE",
            "targetColumn": "ORDER_QTY",
            "threshold": "95",
            "severity": "LOW",
            "datasourceId": ds_id,
            "ruleExpression": "ORDER_QTY > 0",
        }
        resp = await client.post("/api/v1/dq-rule-params/rules", json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["configMode"] == "custom"
        assert body["ruleParams"] is None
        assert body["ruleExpression"] == "ORDER_QTY > 0"


@pytest.mark.integration
class TestMutex:
    """rule_params / rule_expression 互斥校验。"""

    async def test_both_rejected_422(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        payload = {
            "ruleCode": "DQ_BOTH",
            "ruleName": "x",
            "ruleType": "VALIDITY",
            "targetTable": "T",
            "targetColumn": "C",
            "threshold": "95",
            "severity": "LOW",
            "datasourceId": ds_id,
            "ruleParams": {"kind": "range", "min": 0},
            "ruleExpression": "C > 0",
        }
        resp = await client.post("/api/v1/dq-rule-params/rules", json=payload)
        assert resp.status_code == 422

    async def test_neither_rejected_422(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        payload = {
            "ruleCode": "DQ_NEITHER",
            "ruleName": "x",
            "ruleType": "VALIDITY",
            "targetTable": "T",
            "targetColumn": "C",
            "threshold": "95",
            "severity": "LOW",
            "datasourceId": ds_id,
        }
        resp = await client.post("/api/v1/dq-rule-params/rules", json=payload)
        assert resp.status_code == 422


@pytest.mark.integration
class TestListAndGet:
    """列表 / 详情端点。"""

    async def test_list_empty(self, client) -> None:
        resp = await client.get("/api/v1/dq-rule-params/rules")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_list_and_get_roundtrip(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        create_payload = {
            "ruleCode": "DQ_ROUNDTRIP",
            "ruleName": "roundtrip test",
            "ruleType": "VALIDITY",
            "targetTable": "PO_LINE",
            "targetColumn": "ORDER_QTY",
            "threshold": "95",
            "severity": "MEDIUM",
            "datasourceId": ds_id,
            "ruleParams": {"kind": "range", "min": 0, "max": 1000},
        }
        create_resp = await client.post(
            "/api/v1/dq-rule-params/rules", json=create_payload
        )
        assert create_resp.status_code == 201, create_resp.text
        rule_id = create_resp.json()["id"]

        list_resp = await client.get("/api/v1/dq-rule-params/rules")
        assert list_resp.status_code == 200
        rules = list_resp.json()
        assert any(r["id"] == rule_id for r in rules)

        get_resp = await client.get(
            f"/api/v1/dq-rule-params/rules/{rule_id}"
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == rule_id
        assert get_resp.json()["ruleCode"] == "DQ_ROUNDTRIP"

    async def test_get_404(self, client) -> None:
        resp = await client.get("/api/v1/dq-rule-params/rules/999999")
        assert resp.status_code == 404


@pytest.mark.integration
class TestUpdate:
    """更新端点：结构化重新编译 / 自定义清空 params。"""

    async def test_update_structured_recompiles(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        create_payload = {
            "ruleCode": "DQ_UPDATE_COMPILE",
            "ruleName": "original name",
            "ruleType": "VALIDITY",
            "targetTable": "PO_LINE",
            "targetColumn": "ORDER_QTY",
            "threshold": "95",
            "severity": "LOW",
            "datasourceId": ds_id,
            "ruleParams": {"kind": "range", "min": 0, "max": 100},
        }
        create_resp = await client.post(
            "/api/v1/dq-rule-params/rules", json=create_payload
        )
        assert create_resp.status_code == 201, create_resp.text
        rule_id = create_resp.json()["id"]

        update_payload = {
            "ruleName": "updated name",
            "threshold": "99",
            "ruleParams": {"kind": "range", "min": -50, "max": 200},
        }
        update_resp = await client.put(
            f"/api/v1/dq-rule-params/rules/{rule_id}", json=update_payload
        )
        assert update_resp.status_code == 200, update_resp.text
        body = update_resp.json()
        assert body["ruleName"] == "updated name"
        assert body["threshold"] == "99.00"
        assert body["configMode"] == "structured"
        assert "BETWEEN" in body["ruleExpression"]
        assert "ORDER_QTY" in body["ruleExpression"]

    async def test_update_expression_clears_params(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        create_payload = {
            "ruleCode": "DQ_UPDATE_EXPR",
            "ruleName": "struct to expr",
            "ruleType": "VALIDITY",
            "targetTable": "PO_LINE",
            "targetColumn": "ORDER_QTY",
            "threshold": "95",
            "severity": "LOW",
            "datasourceId": ds_id,
            "ruleParams": {"kind": "range", "min": 0, "max": 100},
        }
        create_resp = await client.post(
            "/api/v1/dq-rule-params/rules", json=create_payload
        )
        assert create_resp.status_code == 201, create_resp.text
        rule_id = create_resp.json()["id"]

        update_payload = {"ruleExpression": "ORDER_QTY >= 1"}
        update_resp = await client.put(
            f"/api/v1/dq-rule-params/rules/{rule_id}", json=update_payload
        )
        assert update_resp.status_code == 200, update_resp.text
        body = update_resp.json()
        assert body["configMode"] == "custom"
        assert body["ruleParams"] is None
        assert body["ruleExpression"] == "ORDER_QTY >= 1"


@pytest.mark.integration
class TestDelete:
    """删除端点。"""

    async def test_delete_204_and_get_404(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        create_payload = {
            "ruleCode": "DQ_DELETE",
            "ruleName": "to be deleted",
            "ruleType": "VALIDITY",
            "targetTable": "PO_LINE",
            "targetColumn": "ORDER_QTY",
            "threshold": "95",
            "severity": "LOW",
            "datasourceId": ds_id,
            "ruleExpression": "ORDER_QTY > 0",
        }
        create_resp = await client.post(
            "/api/v1/dq-rule-params/rules", json=create_payload
        )
        assert create_resp.status_code == 201, create_resp.text
        rule_id = create_resp.json()["id"]

        del_resp = await client.delete(
            f"/api/v1/dq-rule-params/rules/{rule_id}"
        )
        assert del_resp.status_code == 204

        get_resp = await client.get(
            f"/api/v1/dq-rule-params/rules/{rule_id}"
        )
        assert get_resp.status_code == 404
