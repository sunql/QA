"""history 回放 API 集成测试（Phase 4.5）。

覆盖：kpi_catalog_history 回放 + feature_definition_history 回放。
"""

from __future__ import annotations

import pytest
from fastapi import status


ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


@pytest.mark.asyncio
class TestKpiHistoryReplayApi:
    """GET /api/v1/kpi-catalog/{id}/history 系列端点。"""

    async def test_list_kpi_history_returns_revisions(self, client) -> None:
        """创建 KPI → 更新 → 查 history 有多条记录。"""
        # 创建 KPI（revision=0 的 history 写入）
        kpi_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"HIST_TEST_{id(self)}",
                "kpiName": "历史回放测试",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        assert kpi_resp.status_code == status.HTTP_201_CREATED
        kpi_id = kpi_resp.json()["id"]

        # 更新 KPI（revision=1）
        upd_resp = await client.put(
            f"/api/v1/kpi-catalog/{kpi_id}",
            json={"kpiName": "历史回放测试-已更新"},
            headers=ADMIN_HEADERS,
        )
        assert upd_resp.status_code == status.HTTP_200_OK

        # 查询历史
        resp = await client.get(
            f"/api/v1/kpi-catalog/{kpi_id}/history",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        rows = resp.json()
        assert len(rows) >= 2
        # revision 倒序
        assert rows[0]["revision"] == 1
        assert rows[1]["revision"] == 0
        # CamelModel alias: snapshot_json → snapshotJson；但内部 JSONB dict 用 DB 列名（snake_case）
        assert "kpi_name" in rows[0]["snapshotJson"]
        assert rows[0]["snapshotJson"]["kpi_name"] == "历史回放测试-已更新"

    async def test_get_kpi_history_revision_returns_snapshot(self, client) -> None:
        """GET /kpi-catalog/{id}/history/{revision} 返回指定快照。"""
        kpi_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"HIST_REV_{id(self)}",
                "kpiName": "Revision 测试",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        assert kpi_resp.status_code == status.HTTP_201_CREATED
        kpi_id = kpi_resp.json()["id"]

        # 查 revision 0
        resp = await client.get(
            f"/api/v1/kpi-catalog/{kpi_id}/history/0",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["revision"] == 0
        # snapshotJson 内部是 DB 原始列名（snake_case）
        assert body["snapshotJson"]["kpi_name"] == "Revision 测试"

    async def test_unknown_kpi_history_returns_404(self, client) -> None:
        """不存在的 KPI → history 端点 404。"""
        resp = await client.get(
            "/api/v1/kpi-catalog/999999/history",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
class TestFeatureHistoryReplayApi:
    """GET /api/v1/features/{id}/history 端点。"""

    async def test_list_feature_history_returns_snapshots(self, client) -> None:
        """创建 Feature → 更新 → 查 history 有多条。"""
        # 创建 datasource
        ds_resp = await client.post(
            "/api/v1/datasources",
            json={
                "name": f"hist-ds-{id(self)}",
                "type": "postgresql",
                "host": "db.example.com",
                "port": 5432,
                "databaseName": "testdb",
                "username": "u",
                "password": "p",
            },
        )
        assert ds_resp.status_code == status.HTTP_201_CREATED
        ds_id = ds_resp.json()["id"]

        # 创建 Feature
        feat_resp = await client.post(
            "/api/v1/features",
            json={
                "featureName": f"HIST_FEAT_{id(self)}",
                "featureAlias": "历史测试特征",
                "entityType": "SUPPLIER",
                "calculationLogic": "SELECT 1",
                "datasourceId": ds_id,
            },
            headers=ADMIN_HEADERS,
        )
        assert feat_resp.status_code == status.HTTP_201_CREATED
        feat_id = feat_resp.json()["id"]

        # 更新 Feature
        upd_resp = await client.put(
            f"/api/v1/features/{feat_id}",
            json={"featureAlias": "历史测试特征-已更新"},
            headers=ADMIN_HEADERS,
        )
        assert upd_resp.status_code == status.HTTP_200_OK

        # 查 history
        resp = await client.get(
            f"/api/v1/features/{feat_id}/history",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        rows = resp.json()
        assert len(rows) >= 2
        # changed_at 倒序；snapshotJson 内部是 DB 原始列名（snake_case）
        assert rows[0]["snapshotJson"]["feature_alias"] == "历史测试特征-已更新"

    async def test_unknown_feature_history_returns_404(self, client) -> None:
        """不存在的 Feature → history 端点 404。"""
        resp = await client.get(
            "/api/v1/features/999999/history",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND
