"""Phase 4.3 Feature 定义 API 集成测试（真实 PG 5433 + 完整 API 链路）。

覆盖：
1. 迁移后 feature_definition 表存在 + 列齐全
2. CRUD：创建（默认 DRAFT/DAILY/v1.0 + owner 派生）→ 读回一致 → 更新 → 删除级联
3. 重复 feature_name → 409；datasource_id 无效 → 422；status 非法 → 422
4. calculation_logic 非只读 → 400（SqlSafetyError）
5. owner-based ACL：非 owner 非 admin → 403
"""

from __future__ import annotations

from sqlalchemy import text

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
PROC_HEADERS = {
    "X-User-Id": "proc-user",
    "X-User-Departments": "procurement",
}
FIN_HEADERS = {
    "X-User-Id": "fin-user",
    "X-User-Roles": "user",
    "X-User-Departments": "finance",
}

_DATASOURCE_PAYLOAD: dict[str, object] = {
    "name": "feature-test-ds",
    "type": "postgresql",
    "host": "db.example.com",
    "port": 5432,
    "databaseName": "appdb",
    "username": "u",
    "password": "p",
    "description": "feature test datasource",
    "isActive": True,
    "isDefault": False,
}

_BASE_PAYLOAD: dict[str, object] = {
    "featureName": "SUPPLIER_OTD_3M",
    "featureAlias": "供应商3月准时交付率",
    "entityType": "SUPPLIER",
    "calculationLogic": (
        "SELECT supplier_code AS entity_key, AVG(otd_rate) AS value "
        "FROM DWS_SUPPLIER_DELIVERY_MONTHLY GROUP BY supplier_code"
    ),
    "windowSize": "3M",
    "unit": "%",
}


async def _createTestDatasource(client, **overrides) -> int:
    payload = dict(_DATASOURCE_PAYLOAD)
    payload.update(overrides)
    resp = await client.post("/api/v1/datasources", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _payload(**overrides) -> dict[str, object]:
    p: dict[str, object] = dict(_BASE_PAYLOAD)
    p.update(overrides)
    return p


class TestFeatureMigration:
    async def test_table_and_columns_after_migration(self, dbSession) -> None:
        result = await dbSession.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'feature_definition'"
            )
        )
        cols = {row[0] for row in result}
        for c in (
            "id",
            "feature_name",
            "feature_alias",
            "feature_definition",
            "entity_type",
            "calculation_logic",
            "window_size",
            "refresh_frequency",
            "unit",
            "owner",
            "version",
            "status",
            "is_enabled",
            "datasource_id",
            "created_by",
            "created_time",
            "updated_time",
        ):
            assert c in cols, f"missing column {c}"

        result2 = await dbSession.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'feature_value'"
            )
        )
        cols2 = {row[0] for row in result2}
        for c in (
            "id",
            "feature_id",
            "entity_key",
            "value",
            "value_text",
            "valid_at",
            "computed_at",
        ):
            assert c in cols2, f"missing column {c}"


class TestFeatureDefinitionApi:
    async def test_list_empty(self, client) -> None:
        resp = await client.get("/api/v1/features")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_minimal_fields(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        resp = await client.post(
            "/api/v1/features",
            json=_payload(datasourceId=ds_id),
            headers=PROC_HEADERS,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["featureName"] == "SUPPLIER_OTD_3M"
        assert body["status"] == "DRAFT"
        assert body["refreshFrequency"] == "DAILY"
        assert body["version"] == "v1.0"
        assert body["isEnabled"] is True
        assert body["owner"] == "procurement"  # 派生自 actor.departments[0]
        assert body["datasourceId"] == ds_id

    async def test_create_full_fields(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        resp = await client.post(
            "/api/v1/features",
            json=_payload(
                datasourceId=ds_id,
                featureDefinition="供应商最近 3 个月准时交付率均值",
                refreshFrequency="WEEKLY",
                status="ACTIVE",
                version="v2.0",
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["featureDefinition"] == "供应商最近 3 个月准时交付率均值"
        assert body["refreshFrequency"] == "WEEKLY"
        assert body["status"] == "ACTIVE"
        assert body["version"] == "v2.0"

    async def test_duplicate_feature_name_returns_409(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        first = await client.post(
            "/api/v1/features", json=_payload(datasourceId=ds_id), headers=ADMIN_HEADERS
        )
        assert first.status_code == 201
        dup = await client.post(
            "/api/v1/features", json=_payload(datasourceId=ds_id), headers=ADMIN_HEADERS
        )
        assert dup.status_code == 409

    async def test_invalid_datasource_id_returns_422(self, client) -> None:
        resp = await client.post(
            "/api/v1/features", json=_payload(datasourceId=999999), headers=ADMIN_HEADERS
        )
        assert resp.status_code == 422

    async def test_invalid_status_returns_422(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        resp = await client.post(
            "/api/v1/features",
            json=_payload(datasourceId=ds_id, status="BOGUS"),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422

    async def test_non_readonly_calculation_logic_returns_400(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        resp = await client.post(
            "/api/v1/features",
            json=_payload(
                datasourceId=ds_id,
                calculationLogic="DELETE FROM DWS_SUPPLIER_DELIVERY_MONTHLY",
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400

    async def test_get_by_id_and_not_found(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        created = await client.post(
            "/api/v1/features", json=_payload(datasourceId=ds_id), headers=ADMIN_HEADERS
        )
        assert created.status_code == 201
        feature_id = created.json()["id"]

        detail = await client.get(f"/api/v1/features/{feature_id}")
        assert detail.status_code == 200
        assert detail.json()["id"] == feature_id

        missing = await client.get("/api/v1/features/999999")
        assert missing.status_code == 404

    async def test_update_by_owner_returns_200(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        created = await client.post(
            "/api/v1/features", json=_payload(datasourceId=ds_id), headers=PROC_HEADERS
        )
        feature_id = created.json()["id"]

        upd = await client.put(
            f"/api/v1/features/{feature_id}",
            json={"unit": "天", "windowSize": "YTD"},
            headers=PROC_HEADERS,
        )
        assert upd.status_code == 200, upd.text
        body = upd.json()
        assert body["unit"] == "天"
        assert body["windowSize"] == "YTD"
        assert body["featureName"] == "SUPPLIER_OTD_3M"  # 未更新字段保留

    async def test_update_by_non_owner_returns_403(self, client) -> None:
        ds_id = await _createTestDatasource(client)
        created = await client.post(
            "/api/v1/features", json=_payload(datasourceId=ds_id), headers=PROC_HEADERS
        )
        feature_id = created.json()["id"]

        upd = await client.put(
            f"/api/v1/features/{feature_id}",
            json={"unit": "%"},
            headers=FIN_HEADERS,
        )
        assert upd.status_code == 403

    async def test_delete_cascades_values(self, client, dbSession) -> None:
        ds_id = await _createTestDatasource(client)
        created = await client.post(
            "/api/v1/features", json=_payload(datasourceId=ds_id), headers=ADMIN_HEADERS
        )
        feature_id = created.json()["id"]

        # 直接插一条 feature_value（绕过 compute，验证级联删除）
        await dbSession.execute(
            text(
                "INSERT INTO feature_value "
                "(feature_id, entity_key, value, valid_at, computed_at) "
                "VALUES (:fid, 'Q630', 0.95, CURRENT_DATE, now())"
            ),
            {"fid": feature_id},
        )
        await dbSession.commit()

        delete = await client.delete(f"/api/v1/features/{feature_id}", headers=ADMIN_HEADERS)
        assert delete.status_code == 204

        # 特征值已被级联删除
        cnt = await dbSession.execute(
            text("SELECT count(*) FROM feature_value WHERE feature_id = :fid"),
            {"fid": feature_id},
        )
        assert cnt.scalar_one() == 0
