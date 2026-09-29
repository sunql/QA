"""Task 0.1: PO 完成率 FeatureDefinition 集成测试（真实 PG 5433 + 完整 API 链路）。

TDD 步骤：
- Step 1: 写失败测试（feature_definition 表无 SUPPLIER_PO_COMPLETION_RATE）
- Step 3: seed_features.py 追加后重跑确认通过
- Step 4: 验算结果正确（AVG of ratios SQL 逻辑由 fake adapter 模拟返回行）
"""

from __future__ import annotations

from decimal import Decimal
from itertools import count

from sqlalchemy import func, select

from app.api.v1 import features as featuresModule
from app.domain.models import FeatureDefinition
from scripts.seed_features import FEATURE_SEEDS, seedFeatures


ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
_dsCounter = count(1)

_DATASOURCE_PAYLOAD: dict[str, object] = {
    "name": "po-completion-ds",
    "type": "postgresql",
    "host": "db.example.com",
    "port": 5432,
    "databaseName": "appdb",
    "username": "u",
    "password": "p",
}


class _FakePoCompletionAdapter:
    """模拟 PO 完成率计算结果：3 个供应商，AVG of ratios 逻辑。

    真实 SQL 为 AVG((received_qty - rejected_qty - returned_qty) / NULLIF(order_qty, 0))
    per entity（supplier_code），这里直接返回模拟结果行。
    """

    def __init__(self) -> None:
        # (entity_key, value)
        self._rows = [
            {"entity_key": "S001", "value": Decimal("0.8750")},   # 87.5%
            {"entity_key": "S002", "value": Decimal("0.9200")},   # 92.0%
            {"entity_key": "S003", "value": Decimal("0.7600")},   # 76.0%
        ]
        self.calls: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.calls.append(sql)
        return self._rows


def _installFakeAdapter(monkeypatch, adapter) -> None:
    """把 feature compute service 的 adapter 解析替换为 fake（chat 同模式）。"""
    monkeypatch.setattr(
        featuresModule._computeService, "_adapterProvider", lambda dsId, ds: adapter
    )


async def _createTestDatasource(client) -> int:
    payload = dict(_DATASOURCE_PAYLOAD)
    payload["name"] = f"po-completion-ds-{next(_dsCounter)}"
    resp = await client.post("/api/v1/datasources", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestSupplierPoCompletionRate:
    """Task 0.1: SUPPLIER_PO_COMPLETION_RATE feature 计算正确性。"""

    async def test_supplier_po_completion_rate_computes_avg_ratio(
        self, client, dbSession, monkeypatch
    ) -> None:
        """AVG of ratios 计算：FeatureComputeService 解析 entity_key + value 并落库。"""
        adapter = _FakePoCompletionAdapter()
        _installFakeAdapter(monkeypatch, adapter)

        # Step 1: 创建测试 datasource 并 seed 所有特征（包括新的 PO_COMPLETION_RATE）
        ds_id = await _createTestDatasource(client)
        inserted = await seedFeatures(dbSession, ds_id)
        # 6 条特征：5 条原有 + 1 条新的 PO_COMPLETION_RATE
        assert inserted >= 1, f"seedFeatures 应至少 upsert 1 条新特征，实际 {inserted}"
        await dbSession.commit()

        # Step 2: 确认 SUPPLIER_PO_COMPLETION_RATE 已注册
        result = await dbSession.execute(
            select(FeatureDefinition).where(
                FeatureDefinition.feature_name == "SUPPLIER_PO_COMPLETION_RATE"
            )
        )
        feat = result.scalar_one_or_none()
        assert feat is not None, (
            "SUPPLIER_PO_COMPLETION_RATE 未在 feature_definition 表中注册；"
            "请确认 seed_features.py 已追加定义并成功 upsert"
        )
        assert feat.feature_alias == "供应商采购订单完成率"
        assert feat.entity_type == "SUPPLIER"
        assert feat.status == "ACTIVE"
        assert feat.is_enabled is True

        # Step 3: 计算特征
        resp = await client.post(
            f"/api/v1/features/{feat.id}/compute",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["featureId"] == feat.id
        assert body["rows"] == 3, f"期望 3 行，实际 {body['rows']}"

        # Step 4: 验证落库值
        values_resp = await client.get(f"/api/v1/features/{feat.id}/values")
        assert values_resp.status_code == 200
        rows = values_resp.json()
        assert len(rows) == 3, f"期望 3 条 feature_value，实际 {len(rows)}"

        entity_keys = {r["entityKey"] for r in rows}
        assert entity_keys == {"S001", "S002", "S003"}, f"期望 S001/S002/S003，实际 {entity_keys}"

        by_key = {r["entityKey"]: r for r in rows}
        assert Decimal(by_key["S001"]["value"]) == Decimal("0.8750")
        assert Decimal(by_key["S002"]["value"]) == Decimal("0.9200")
        assert Decimal(by_key["S003"]["value"]) == Decimal("0.7600")

        # Step 5: 验证 SQL 调用包含关键片段（calculation_logic 注入正确）
        assert len(adapter.calls) == 1
        sql = adapter.calls[0]
        assert "AVG" in sql.upper(), f"SQL 应包含 AVG 聚合函数，实际 SQL: {sql}"
        assert ("received_qty" in sql.lower() or "RECEIVED_QTY" in sql.upper())

    async def test_seed_features_includes_po_completion_rate(
        self, client, dbSession
    ) -> None:
        """验证 seed_features.py FEATURE_SEEDS 包含 PO_COMPLETION_RATE。"""
        ds_id = await _createTestDatasource(client)
        await seedFeatures(dbSession, ds_id)
        await dbSession.commit()

        names = {f["feature_name"] for f in FEATURE_SEEDS}
        assert "SUPPLIER_PO_COMPLETION_RATE" in names

        result = await dbSession.execute(
            select(func.count()).select_from(FeatureDefinition)
        )
        total = result.scalar()
        # 6 条特征：5 原有 + 1 新 PO_COMPLETION_RATE
        assert total >= 6, f"期望至少 6 条特征，实际 {total}"

    async def test_po_completion_rate_sql_logic_correctness(
        self, client, dbSession, monkeypatch
    ) -> None:
        """验证 SUPPLIER_PO_COMPLETION_RATE 的 calculation_logic 包含正确 SQL 结构。"""
        adapter = _FakePoCompletionAdapter()
        _installFakeAdapter(monkeypatch, adapter)

        ds_id = await _createTestDatasource(client)
        await seedFeatures(dbSession, ds_id)
        await dbSession.commit()

        result = await dbSession.execute(
            select(FeatureDefinition).where(
                FeatureDefinition.feature_name == "SUPPLIER_PO_COMPLETION_RATE"
            )
        )
        feat = result.scalar_one_or_none()
        assert feat is not None

        sql = feat.calculation_logic
        # 核心结构验证
        assert "AVG(" in sql.upper(), "calculation_logic 必须包含 AVG 聚合"
        assert "entity_key" in sql.lower(), "calculation_logic 必须返回 entity_key 列"
        assert "value" in sql.lower(), "calculation_logic 必须返回 value 列"
        assert "zero_stock_flag" in sql.lower(), "必须包含 zero_stock_flag 过滤"
        assert "DWD_PURCHASE_ORDER_LINE" in sql.upper(), "必须引用 DWD_PURCHASE_ORDER_LINE 表"
        assert "DWD_GOODS_RECEIPT_LINE" in sql.upper(), "必须引用 DWD_GOODS_RECEIPT_LINE 表"
        assert "DIM_SUPPLIER" in sql.upper(), "必须引用 DIM_SUPPLIER 表"
        # 时间窗口使用 TRUNC(SYSDATE) 而非参数化 start_date/end_date（Oracle 不支持参数绑定）
        assert "TRUNC(SYSDATE)" in sql.upper() or "SYSDATE" in sql.upper()
