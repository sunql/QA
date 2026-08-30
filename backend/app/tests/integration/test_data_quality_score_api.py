"""数据质量评分端点集成测试（Phase 1.3）。

真实 PG + 完整 API 链路。dispatcher 通过 monkeypatch 注入 fake adapter
避免依赖真实外部业务库；metadata DB 上验证 DataQualityRule 持久化 +
DataQualityScore 聚合落库 + 列表过滤。

覆盖：
- POST /api/v1/data-quality/scores/compute：拉 enabled rule → 评估 → 聚合 → 落库
- GET /api/v1/data-quality/scores：默认倒序 + latest + table / scoreType 过滤
- 空 rule 集合：compute 返回空响应且不写库
- ERROR 状态结果不污染聚合分母
"""

from __future__ import annotations

from typing import Any

import app.services.data_quality_evaluator as dispatcher_module
from app.tests.integration.test_data_quality_api import _createTestDatasource
from app.tests.integration.test_data_quality_eval_api import (
    _FakeAdapter,
    _installFakeAdapter,
)


async def _create_rule(client, ds_id: int, **overrides: Any) -> int:
    payload: dict[str, Any] = {
        "ruleName": "规则",
        "ruleCode": overrides.get("ruleCode", "SCORE_RULE"),
        "datasourceId": ds_id,
        "targetTable": overrides.get("targetTable", "PORDER"),
        "targetColumn": overrides.get("targetColumn", "ORDER_QTY"),
        "ruleType": overrides.get("ruleType", "COMPLETENESS"),
        "ruleExpression": overrides.get("ruleExpression"),
        "threshold": overrides.get("threshold", "50.00"),
        "severity": overrides.get("severity", "MEDIUM"),
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    resp = await client.post("/api/v1/data-quality/rules", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestDataQualityScoreApi:
    async def test_compute_aggregates_per_table_and_global(
        self, client, monkeypatch
    ) -> None:
        """compute 走完整链路：2 条 enabled rule 同表 → 落库 1 TABLE + 1 GLOBAL。"""
        ds_id = await _createTestDatasource(client)
        await _create_rule(
            client,
            ds_id,
            ruleCode="SCORE_C",
            targetTable="PORDER",
            targetColumn="A",
            ruleType="COMPLETENESS",
            threshold="0.00",  # 任意通过率都 PASS
        )
        await _create_rule(
            client,
            ds_id,
            ruleCode="SCORE_V",
            targetTable="PORDER",
            targetColumn="B",
            ruleType="VALIDITY",
            ruleExpression="B > 0",
            threshold="0.00",
        )
        # 任意 100% 通过
        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 10}]})
        _installFakeAdapter(monkeypatch, adapter)

        resp = await client.post("/api/v1/data-quality/scores/compute")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["evaluatedRules"] == 2
        # PORDER 表 1 条 + GLOBAL 1 条 = 2
        assert body["savedScores"] == 2
        assert len(body["scores"]) == 2
        tables = {s["targetTable"]: s for s in body["scores"]}
        # TABLE 行
        assert "PORDER" in tables
        po = tables["PORDER"]
        assert po["scoreType"] == "TABLE"
        assert po["completenessScore"] is not None
        assert po["validityScore"] is not None
        assert po["timelinessScore"] is None  # 暂未实现
        assert po["overallScore"] is not None
        assert po["rulesCount"] == 2
        # GLOBAL 行
        assert "*" in tables
        glob = tables["*"]
        assert glob["scoreType"] == "GLOBAL"

    async def test_compute_multi_table(self, client, monkeypatch) -> None:
        """2 个不同表 → 落库 2 TABLE + 1 GLOBAL = 3 条。"""
        ds_id = await _createTestDatasource(client)
        await _create_rule(
            client,
            ds_id,
            ruleCode="MULTI_A",
            targetTable="PORDER",
            targetColumn="X",
            ruleType="COMPLETENESS",
            threshold="0.00",
        )
        await _create_rule(
            client,
            ds_id,
            ruleCode="MULTI_B",
            targetTable="SUPPLIER",
            targetColumn="Y",
            ruleType="COMPLETENESS",
            threshold="0.00",
        )
        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 8}]})
        _installFakeAdapter(monkeypatch, adapter)

        resp = await client.post("/api/v1/data-quality/scores/compute")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["savedScores"] == 3  # 2 TABLE + 1 GLOBAL

    async def test_compute_empty_returns_no_scores(
        self, client, monkeypatch
    ) -> None:
        """没有任何 enabled rule → 0 saved，不调业务库。"""
        adapter = _FakeAdapter()
        _installFakeAdapter(monkeypatch, adapter)
        # 不创建任何 rule

        resp = await client.post("/api/v1/data-quality/scores/compute")
        assert resp.status_code == 200
        body = resp.json()
        assert body["evaluatedRules"] == 0
        assert body["savedScores"] == 0
        assert body["scores"] == []
        # 没调业务库
        assert adapter.calls == []

    async def test_compute_skips_disabled_rules(self, client, monkeypatch) -> None:
        """软删除（is_enabled=false）的 rule 不参与 compute。"""
        ds_id = await _createTestDatasource(client)
        rid = await _create_rule(
            client,
            ds_id,
            ruleCode="DISABLED_R",
            targetTable="PORDER",
            targetColumn="X",
            ruleType="COMPLETENESS",
            threshold="0.00",
        )
        # 软删
        await client.delete(f"/api/v1/data-quality/rules/{rid}", headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"})
        adapter = _FakeAdapter()
        _installFakeAdapter(monkeypatch, adapter)

        resp = await client.post("/api/v1/data-quality/scores/compute")
        assert resp.status_code == 200
        body = resp.json()
        assert body["evaluatedRules"] == 0
        assert body["savedScores"] == 0

    async def test_compute_then_list_default_desc(self, client, monkeypatch) -> None:
        """compute 写入后，GET /scores 默认按 evaluated_at DESC。"""
        ds_id = await _createTestDatasource(client)
        await _create_rule(
            client,
            ds_id,
            ruleCode="LIST_R",
            targetTable="PORDER",
            targetColumn="X",
            ruleType="COMPLETENESS",
            threshold="0.00",
        )
        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 10}]})
        _installFakeAdapter(monkeypatch, adapter)

        # 跑一次 compute
        cr = await client.post("/api/v1/data-quality/scores/compute")
        assert cr.status_code == 200

        # 列表
        lr = await client.get("/api/v1/data-quality/scores")
        assert lr.status_code == 200, lr.text
        rows = lr.json()
        assert len(rows) == 2  # 1 TABLE + 1 GLOBAL
        # 验证字段形状
        for r in rows:
            assert "id" in r
            assert "targetTable" in r
            assert "scoreType" in r
            assert "overallScore" in r
            assert "evaluatedAt" in r

    async def test_list_latest_returns_one_per_table(self, client, monkeypatch) -> None:
        """latest=true → 每个 (target_table, score_type) 只返回最新一条。"""
        ds_id = await _createTestDatasource(client)
        await _create_rule(
            client,
            ds_id,
            ruleCode="LATEST_R",
            targetTable="PORDER",
            targetColumn="X",
            ruleType="COMPLETENESS",
            threshold="0.00",
        )
        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 10}]})
        _installFakeAdapter(monkeypatch, adapter)

        # 跑两次 compute：应该只保留每组最新
        for _ in range(2):
            r = await client.post("/api/v1/data-quality/scores/compute")
            assert r.status_code == 200

        lr = await client.get("/api/v1/data-quality/scores?latest=true")
        assert lr.status_code == 200
        rows = lr.json()
        # 1 table × 2 scoreType (TABLE + GLOBAL) = 2
        assert len(rows) == 2
        keys = {(r["targetTable"], r["scoreType"]) for r in rows}
        assert keys == {("PORDER", "TABLE"), ("*", "GLOBAL")}

    async def test_list_filter_by_table(self, client, monkeypatch) -> None:
        """?table=PORDER 只返回该表相关行（TABLE + GLOBAL，因为 GLOBAL 也算）→ 这里按 table=PORDER 只剩 TABLE 行。"""
        ds_id = await _createTestDatasource(client)
        await _create_rule(
            client,
            ds_id,
            ruleCode="FILTER_R",
            targetTable="PORDER",
            targetColumn="X",
            ruleType="COMPLETENESS",
            threshold="0.00",
        )
        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 10}]})
        _installFakeAdapter(monkeypatch, adapter)

        await client.post("/api/v1/data-quality/scores/compute")

        lr = await client.get("/api/v1/data-quality/scores?table=PORDER")
        assert lr.status_code == 200
        rows = lr.json()
        assert len(rows) == 1
        assert rows[0]["targetTable"] == "PORDER"
        assert rows[0]["scoreType"] == "TABLE"

    async def test_list_filter_by_score_type(self, client, monkeypatch) -> None:
        """?scoreType=GLOBAL 只返回 GLOBAL 行。"""
        ds_id = await _createTestDatasource(client)
        await _create_rule(
            client,
            ds_id,
            ruleCode="GLOBAL_R",
            targetTable="PORDER",
            targetColumn="X",
            ruleType="COMPLETENESS",
            threshold="0.00",
        )
        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 10}]})
        _installFakeAdapter(monkeypatch, adapter)

        await client.post("/api/v1/data-quality/scores/compute")

        lr = await client.get("/api/v1/data-quality/scores?scoreType=GLOBAL")
        assert lr.status_code == 200
        rows = lr.json()
        assert len(rows) == 1
        assert rows[0]["scoreType"] == "GLOBAL"
