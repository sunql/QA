"""跨系统编码映射接口集成测试（真实 PostgreSQL + 完整 API 链路，Phase 3.1）。

验证 HTTP 契约（camelCase JSON）：
- GET    /api/v1/entity-mappings                列表（按 entityType / sourceSystem / enterpriseKey 过滤）
- GET    /api/v1/entity-mappings/search         AutoComplete 搜索
- GET    /api/v1/entity-mappings/{id}           详情
- POST   /api/v1/entity-mappings                创建（201）
- POST   /api/v1/entity-mappings/bulk           批量导入（feat-entity-mapping-bulk-import）
- PUT    /api/v1/entity-mappings/{id}           更新
- DELETE /api/v1/entity-mappings/{id}           删除（204）

唯一性约束：unique (entity_type, enterprise_key, source_system) →
同实体 + 同源系统重复创建返回 422。

测试在真实 PG 5433 上运行（qa_metadata_test 数据库），每用例走 TestClient
+ DependencyOverrides，与 Phase 2.1 数据血缘保持一致。
"""

from __future__ import annotations


_BASE_PAYLOAD: dict[str, object] = {
    "entityType": "SUPPLIER",
    "enterpriseKey": 100001,
    "enterpriseCode": "SUP000001",
    "sourceSystem": "ERP",
    "sourceKey": "V000001",
    "sourceCode": "V000001",
    "matchRule": "MDM_MASTER",
    "effectiveDate": "2026-01-01",
    "expiryDate": "2099-12-31",
}


def _payload(**overrides) -> dict[str, object]:
    """构造合法 payload，支持字段级覆盖。"""
    p: dict[str, object] = dict(_BASE_PAYLOAD)
    p.update(overrides)
    return p


class TestEntityMappingApi:
    async def test_list_empty(self, client) -> None:
        resp = await client.get("/api/v1/entity-mappings")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_get_update_roundtrip(self, client) -> None:
        # CREATE
        resp = await client.post("/api/v1/entity-mappings", json=_payload())
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["entityType"] == "SUPPLIER"
        assert body["enterpriseKey"] == 100001
        assert body["enterpriseCode"] == "SUP000001"
        assert body["sourceSystem"] == "ERP"
        assert body["sourceKey"] == "V000001"
        assert body["matchRule"] == "MDM_MASTER"
        assert body["effectiveDate"] == "2026-01-01"
        assert body["id"] > 0

        mapping_id = body["id"]

        # GET
        detail = await client.get(f"/api/v1/entity-mappings/{mapping_id}")
        assert detail.status_code == 200
        assert detail.json()["id"] == mapping_id

        # UPDATE（局部：变更源系统编码与匹配规则）
        upd = await client.put(
            f"/api/v1/entity-mappings/{mapping_id}",
            json={"sourceCode": "V000001-X", "matchRule": "MAPPING"},
        headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
        )
        assert upd.status_code == 200, upd.text
        upd_body = upd.json()
        assert upd_body["sourceCode"] == "V000001-X"
        assert upd_body["matchRule"] == "MAPPING"
        # 未更新字段保留
        assert upd_body["entityType"] == "SUPPLIER"
        assert upd_body["enterpriseKey"] == 100001

        # LIST 含 1 条
        listing = await client.get("/api/v1/entity-mappings")
        assert listing.status_code == 200
        assert len(listing.json()) == 1

    async def test_delete_returns_204_and_gone(self, client) -> None:
        created = await client.post("/api/v1/entity-mappings", json=_payload())
        assert created.status_code == 201
        mapping_id = created.json()["id"]

        delete = await client.delete(f"/api/v1/entity-mappings/{mapping_id}", headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"})
        assert delete.status_code == 204

        after = await client.get(f"/api/v1/entity-mappings/{mapping_id}")
        assert after.status_code == 404

    async def test_list_filter_by_enterprise_key(self, client) -> None:
        """按企业代理键过滤：插入 2 个不同实体，返回 1 条。"""
        await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=100001, sourceKey="V000001"),
        )
        await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=100002, sourceKey="V000002"),
        )
        resp = await client.get("/api/v1/entity-mappings?enterpriseKey=100002")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["enterpriseKey"] == 100002

    async def test_list_filter_by_entity_type_and_source_system(self, client) -> None:
        await client.post(
            "/api/v1/entity-mappings",
            json=_payload(entityType="SUPPLIER", sourceSystem="ERP", sourceKey="V000001"),
        )
        await client.post(
            "/api/v1/entity-mappings",
            json=_payload(
                entityType="MATERIAL",
                sourceSystem="SRM",
                sourceKey="M000001",
                sourceCode="M000001",
            ),
        )
        by_type = await client.get("/api/v1/entity-mappings?entityType=MATERIAL")
        assert by_type.status_code == 200
        assert len(by_type.json()) == 1
        assert by_type.json()[0]["entityType"] == "MATERIAL"

        by_system = await client.get("/api/v1/entity-mappings?sourceSystem=ERP")
        assert by_system.status_code == 200
        assert len(by_system.json()) == 1
        assert by_system.json()[0]["sourceSystem"] == "ERP"

    async def test_duplicate_unique_constraint_returns_422(self, client) -> None:
        """同实体类型 + 同企业代理键 + 同源系统不允许重复。"""
        first = await client.post("/api/v1/entity-mappings", json=_payload())
        assert first.status_code == 201
        dup = await client.post("/api/v1/entity-mappings", json=_payload())
        assert dup.status_code == 422

    async def test_same_entity_different_source_system_is_allowed(self, client) -> None:
        """同一企业代理键在不同源系统可各有一条映射。"""
        erp = await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=100003, sourceSystem="ERP", sourceKey="V000003"),
        )
        assert erp.status_code == 201
        srm = await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=100003, sourceSystem="SRM", sourceKey="S000003"),
        )
        assert srm.status_code == 201

    async def test_get_not_found_returns_404(self, client) -> None:
        resp = await client.get("/api/v1/entity-mappings/999999")
        assert resp.status_code == 404

    async def test_invalid_enum_returns_422(self, client) -> None:
        """非枚举值应被 Pydantic 拒绝。"""
        resp = await client.post(
            "/api/v1/entity-mappings",
            json=_payload(entityType="INVALID_TYPE"),
        )
        assert resp.status_code == 422

    async def test_list_respects_limit_offset(self, client) -> None:
        """分页：limit 截断 + offset 跳过，order by id 保证稳定。"""
        for key in (200001, 200002, 200003):
            await client.post(
                "/api/v1/entity-mappings",
                json=_payload(enterpriseKey=key, sourceKey=f"V{key}"),
            )
        first_page = await client.get("/api/v1/entity-mappings?limit=2&offset=0")
        assert first_page.status_code == 200
        assert len(first_page.json()) == 2

        second_page = await client.get("/api/v1/entity-mappings?limit=2&offset=2")
        assert second_page.status_code == 200
        rows = second_page.json()
        assert len(rows) == 1
        assert rows[0]["enterpriseKey"] == 200003

    async def test_update_null_for_non_nullable_ignored(self, client) -> None:
        """非空列（sourceCode）显式传 null 视为不动，保留原值。"""
        created = await client.post("/api/v1/entity-mappings", json=_payload())
        assert created.status_code == 201
        mapping_id = created.json()["id"]

        upd = await client.put(
            f"/api/v1/entity-mappings/{mapping_id}",
            json={"sourceCode": None},
        headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["sourceCode"] == "V000001"

    async def test_update_null_clears_expiry_date(self, client) -> None:
        """日期列显式传 null 清除已有失效日期。"""
        created = await client.post("/api/v1/entity-mappings", json=_payload())
        assert created.status_code == 201
        assert created.json()["expiryDate"] == "2099-12-31"
        mapping_id = created.json()["id"]

        upd = await client.put(
            f"/api/v1/entity-mappings/{mapping_id}",
            json={"expiryDate": None},
        headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["expiryDate"] is None

    async def test_update_empty_string_rejected_422(self, client) -> None:
        """非空列空串被 Pydantic min_length=1 拒绝。"""
        created = await client.post("/api/v1/entity-mappings", json=_payload())
        assert created.status_code == 201
        mapping_id = created.json()["id"]

        resp = await client.put(
            f"/api/v1/entity-mappings/{mapping_id}",
            json={"sourceCode": ""},
        headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
        )
        assert resp.status_code == 422

    async def test_create_inverted_date_range_422(self, client) -> None:
        """生效日期晚于失效日期应被 service 层拒绝。"""
        resp = await client.post(
            "/api/v1/entity-mappings",
            json=_payload(effectiveDate="2099-12-31", expiryDate="2026-01-01"),
        )
        assert resp.status_code == 422

    async def test_create_enterprise_key_overflow_422(self, client) -> None:
        """企业代理键超过 BIGINT 上限（2^63-1）应被 Pydantic 拒绝。"""
        resp = await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=2**63),
        )
        assert resp.status_code == 422

    async def test_search_empty_q_returns_empty(self, client) -> None:
        """搜索接口：q 空 → 200 + 空列表（不返回全表）。

        路由顺序：/search 必须在 /{mappingId} 之前注册，否则 FastAPI 会把 "search"
        当作 mappingId=非整数而返回 422。本测试间接确认路由顺序正确。
        """
        resp = await client.get("/api/v1/entity-mappings/search?q=")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    async def test_search_matches_enterprise_code(self, client) -> None:
        """搜索接口：q 模糊匹配 enterprise_code。"""
        await client.post("/api/v1/entity-mappings", json=_payload())
        await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=100002, enterpriseCode="SUP000002",
                          sourceKey="V000002", sourceCode="V000002"),
        )
        resp = await client.get("/api/v1/entity-mappings/search?q=SUP000")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) >= 2
        # 轻量 DTO 不暴露治理字段
        for r in rows:
            assert {"id", "entityType", "enterpriseKey", "enterpriseCode",
                    "sourceSystem", "sourceCode"} <= set(r.keys())
            assert "owner" not in r
            assert "effectiveDate" not in r

    async def test_search_digit_q_prefers_exact_key(self, client) -> None:
        """搜索接口：q 为数字时优先命中 enterprise_key 精确匹配。"""
        await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=200003, enterpriseCode="SUP200003",
                          sourceKey="V200003", sourceCode="V200003"),
        )
        resp = await client.get("/api/v1/entity-mappings/search?q=200003")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) >= 1
        # 精确 enterprise_key 命中排第一位
        assert rows[0]["enterpriseKey"] == 200003

    async def test_search_filters_by_entity_type(self, client) -> None:
        """搜索接口：entityType 过滤生效。"""
        await client.post("/api/v1/entity-mappings", json=_payload())  # SUPPLIER
        await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=200010, enterpriseCode="MAT200010",
                          sourceKey="V200010", sourceCode="V200010",
                          entityType="MATERIAL"),
        )
        resp = await client.get(
            "/api/v1/entity-mappings/search?q=MAT&entityType=MATERIAL"
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert all(r["entityType"] == "MATERIAL" for r in rows)
        assert len(rows) >= 1

    async def test_bulk_import_empty_array_returns_422(self, client) -> None:
        """bulk 端点空数组 → 422 拒绝。"""
        resp = await client.post("/api/v1/entity-mappings/bulk", json=[])
        assert resp.status_code == 422

    async def test_bulk_import_inserts_new_rows(self, client) -> None:
        """bulk：3 行新数据 → inserted=3, updated=0, skipped=0, failed=0。"""
        items = [
            _payload(enterpriseKey=400001, enterpriseCode="SUP400001",
                     sourceKey="X1", sourceCode="X1", name="批1供应商"),
            _payload(enterpriseKey=400002, enterpriseCode="SUP400002",
                     sourceKey="X2", sourceCode="X2", name="批2供应商",
                     sourceSystem="SRM"),
            _payload(enterpriseKey=400003, enterpriseCode="SUP400003",
                     sourceKey="X3", sourceCode="X3", name="批3供应商",
                     sourceSystem="QMS"),
        ]
        resp = await client.post("/api/v1/entity-mappings/bulk", json=items)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["inserted"] == 3
        assert data["updated"] == 0
        assert data["skipped"] == 0
        assert data["failed"] == 0
        for row in data["results"]:
            assert row["status"] == "inserted"
            assert row["id"] is not None

    async def test_bulk_import_skips_unchanged_and_updates_changed(self, client) -> None:
        """bulk 幂等性：第 2 批混入相同行（应 skipped）+ name 变更行（应 updated）+ 新行（应 inserted）。"""
        # 先灌 2 行作为基线
        baseline = [
            _payload(enterpriseKey=500001, enterpriseCode="SUP500001",
                     sourceKey="B1", sourceCode="B1", name="原名1"),
            _payload(enterpriseKey=500002, enterpriseCode="SUP500002",
                     sourceKey="B2", sourceCode="B2", name="原名2"),
        ]
        resp1 = await client.post("/api/v1/entity-mappings/bulk", json=baseline)
        assert resp1.json()["inserted"] == 2

        # 第 2 批：1 行完全相同（skipped）+ 1 行 name 变化（updated）+ 1 行全新（inserted）
        second = [
            _payload(enterpriseKey=500001, enterpriseCode="SUP500001",
                     sourceKey="B1", sourceCode="B1", name="原名1"),  # 完全一致 → skipped
            _payload(enterpriseKey=500002, enterpriseCode="SUP500002",
                     sourceKey="B2", sourceCode="B2", name="新名2"),  # name 变 → updated
            _payload(enterpriseKey=500003, enterpriseCode="SUP500003",
                     sourceKey="B3", sourceCode="B3", name="全新3"),  # 新行 → inserted
        ]
        resp2 = await client.post("/api/v1/entity-mappings/bulk", json=second)
        assert resp2.status_code == 200
        data = resp2.json()
        assert data["inserted"] == 1
        assert data["updated"] == 1
        assert data["skipped"] == 1
        assert data["failed"] == 0
        # 验证 changedFields 含 name
        updated_row = next(r for r in data["results"] if r["status"] == "updated")
        assert "name" in (updated_row.get("changedFields") or [])

    async def test_bulk_import_oversize_returns_422(self, client) -> None:
        """bulk 超 1000 行 → 422。"""
        items = [
            _payload(enterpriseKey=600000 + i, enterpriseCode=f"SUP{i:06d}",
                     sourceKey=f"K{i}", sourceCode=f"C{i}")
            for i in range(1001)
        ]
        resp = await client.post("/api/v1/entity-mappings/bulk", json=items)
        assert resp.status_code == 422
