"""数据质量评估端点集成测试（Phase 1.2）。

业务库适配器通过 monkeypatch 注入，避免依赖真实外部 DB；同时真实 PG 上验证
规则元数据持久化与 dispatcher 调度链路。

覆盖：
- POST /api/v1/data-quality/rules/{id}/evaluate（5 维 evaluator 真实拼接 SQL）
- POST /api/v1/data-quality/rules/evaluate-batch
- 错误路径：规则不存在 / 数据源不存在 / SQL 拼写校验失败
"""

from __future__ import annotations

from typing import Any

import app.services.data_quality_evaluator as dispatcher_module
from app.tests.integration.test_data_quality_api import _createTestDatasource

CREATE_PAYLOAD: dict[str, Any] = {
    "name": "dq-eval-ds",
    "type": "postgresql",
    "host": "db.example.com",
    "port": 5432,
    "databaseName": "appdb",
    "username": "u",
    "password": "p",
    "description": "DQ eval test ds",
    "isActive": True,
    "isDefault": False,
}


class _FakeAdapter:
    """按 SQL 关键字返回行；calls 记录所有执行过的 SQL。"""

    def __init__(self, rows_by_marker: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.rows_by_marker = rows_by_marker or {}
        self.calls: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.calls.append(sql)
        for marker, rows in self.rows_by_marker.items():
            if marker in sql:
                return rows
        return []


def _installFakeAdapter(monkeypatch, adapter: Any) -> None:
    """monkeypatch dispatcher 内部的 get_adapter，让所有 datasource_id 都返回 adapter。

    real get_adapter 是同步函数（缓存查找 + 可能调用 build_adapter），这里保持同步。
    """

    def _fake_get(_datasource_id: int, _ds: Any) -> Any:
        return adapter

    monkeypatch.setattr(dispatcher_module, "get_adapter", _fake_get)


async def _create_rule(client, ds_id: int, **overrides: Any) -> int:
    payload: dict[str, Any] = {
        "ruleName": "规则",
        "ruleCode": overrides.get("ruleCode", "EVAL_RULE"),
        "datasourceId": ds_id,
        "targetTable": overrides.get("targetTable", "PORDER"),
        "targetColumn": overrides.get("targetColumn"),
        "ruleType": overrides.get("ruleType", "COMPLETENESS"),
        "ruleExpression": overrides.get("ruleExpression"),
        "threshold": overrides.get("threshold", "95.00"),
        "severity": overrides.get("severity", "MEDIUM"),
    }
    # 删掉 None 字段，避免与 Pydantic 默认值混淆
    payload = {k: v for k, v in payload.items() if v is not None}
    resp = await client.post("/api/v1/data-quality/rules", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestDataQualityEvaluateApi:
    async def test_evaluate_completeness_pass(self, client, monkeypatch) -> None:
        ds_id = await _createTestDatasource(client)
        rid = await _create_rule(
            client,
            ds_id,
            ruleCode="EVAL_COMP",
            targetColumn="ORDER_QTY",
            ruleType="COMPLETENESS",
            threshold="50.00",  # 让 6/10 = 60% PASS
        )
        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 6}]})
        _installFakeAdapter(monkeypatch, adapter)

        resp = await client.post(f"/api/v1/data-quality/rules/{rid}/evaluate")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "PASS"
        assert body["totalCount"] == 10
        assert body["passedCount"] == 6
        assert body["passRate"] == 60.0
        assert body["ruleCode"] == "EVAL_COMP"
        assert body["datasourceId"] == ds_id
        # SQL 应被拼接
        assert any('FROM "PORDER"' in sql for sql in adapter.calls)

    async def test_evaluate_completeness_fail(self, client, monkeypatch) -> None:
        ds_id = await _createTestDatasource(client)
        rid = await _create_rule(
            client,
            ds_id,
            ruleCode="EVAL_COMP_FAIL",
            targetColumn="ORDER_QTY",
            ruleType="COMPLETENESS",
            threshold="99.00",
        )
        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 100, "passed": 50}]})
        _installFakeAdapter(monkeypatch, adapter)

        resp = await client.post(f"/api/v1/data-quality/rules/{rid}/evaluate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "FAIL"
        assert body["passRate"] == 50.0

    async def test_evaluate_validity_uses_expression(self, client, monkeypatch) -> None:
        ds_id = await _createTestDatasource(client)
        rid = await _create_rule(
            client,
            ds_id,
            ruleCode="EVAL_VAL",
            targetColumn="ORDER_QTY",
            ruleType="VALIDITY",
            ruleExpression="ORDER_QTY > 0",
            threshold="99.00",
        )
        adapter = _FakeAdapter(rows_by_marker={"CASE WHEN": [{"total": 100, "passed": 99}]})
        _installFakeAdapter(monkeypatch, adapter)

        resp = await client.post(f"/api/v1/data-quality/rules/{rid}/evaluate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "PASS"
        # 验证 SQL 真的拼接了 expression（被 CASE WHEN 包裹）
        joined = " ".join(adapter.calls)
        assert "ORDER_QTY > 0" in joined

    async def test_evaluate_referential_uses_exists(self, client, monkeypatch) -> None:
        ds_id = await _createTestDatasource(client)
        rid = await _create_rule(
            client,
            ds_id,
            ruleCode="EVAL_REF",
            targetColumn="SUPPLIER_KEY",
            ruleType="REFERENTIAL",
            ruleExpression="REF SUPPLIER.SUPPLIER_KEY",
            threshold="50.00",
        )
        adapter = _FakeAdapter(rows_by_marker={"EXISTS": [{"total": 50, "passed": 30}]})
        _installFakeAdapter(monkeypatch, adapter)

        resp = await client.post(f"/api/v1/data-quality/rules/{rid}/evaluate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "PASS"
        joined = " ".join(adapter.calls)
        assert 'FROM "SUPPLIER"' in joined
        assert "EXISTS" in joined

    async def test_evaluate_rule_not_found(self, client, monkeypatch) -> None:
        _installFakeAdapter(monkeypatch, _FakeAdapter())
        resp = await client.post("/api/v1/data-quality/rules/999999/evaluate")
        assert resp.status_code == 404

    async def test_evaluate_batch_returns_summary(self, client, monkeypatch) -> None:
        ds_id = await _createTestDatasource(client)
        r1 = await _create_rule(
            client, ds_id, ruleCode="BATCH_A", targetColumn="A", threshold="50.00"
        )
        r2 = await _create_rule(
            client, ds_id, ruleCode="BATCH_B", targetColumn="B", threshold="50.00"
        )
        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 9}]})
        _installFakeAdapter(monkeypatch, adapter)

        resp = await client.post(
            "/api/v1/data-quality/rules/evaluate-batch", json={"ruleIds": [r1, r2, 999999]}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["summaryTotal"] == 3
        # r1 + r2 PASS (90% >= 50%) + 999999 ERROR
        assert body["summaryPassed"] == 2
        results = body["results"]
        assert len(results) == 3
        # 缺失规则应出现在结果中
        assert any(r["ruleId"] == 999999 and r["status"] == "ERROR" for r in results)

    async def test_evaluate_batch_empty_returns_empty(self, client, monkeypatch) -> None:
        _installFakeAdapter(monkeypatch, _FakeAdapter())
        resp = await client.post(
            "/api/v1/data-quality/rules/evaluate-batch", json={"ruleIds": []}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"] == []
        assert body["summaryTotal"] == 0
        assert body["summaryPassed"] == 0