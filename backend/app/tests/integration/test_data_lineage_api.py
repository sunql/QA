"""数据血缘接口集成测试（真实 PostgreSQL + 完整 API 链路，Phase 2.1）。

验证 HTTP 契约（camelCase JSON）：
- GET    /api/v1/lineage/edges          列表（按 sourceLayer / targetLayer / activeOnly 过滤）
- GET    /api/v1/lineage/edges/{id}     详情
- POST   /api/v1/lineage/edges          创建（201）
- PUT    /api/v1/lineage/edges/{id}     更新
- DELETE /api/v1/lineage/edges/{id}     软删除（204；is_active=false）

测试在真实 PG 5433 上运行（qa_metadata_test 数据库），每用例走 TestClient
+ DependencyOverrides，与 Phase 1.1 数据质量 / TermDictionary 保持一致。
"""

from __future__ import annotations


_BASE_PAYLOAD: dict[str, object] = {
    "sourceLayer": "SOURCE_SYSTEM",
    "sourceSystem": "ERP",
    "sourceObject": "PORDER",
    "sourceField": "ORDERQTY",
    "targetLayer": "ODS",
    "targetSystem": "ODS",
    "targetObject": "ODS_PURCHASE_ORDER",
    "targetField": "ORDER_QTY",
    "transformationRule": "标准化 + 代理键",
    "refreshFrequency": "DAILY",
    "owner": "数据团队",
    "description": "源端 → ODS 标准化接入",
}


def _payload(**overrides) -> dict[str, object]:
    """构造合法 payload，支持字段级覆盖。"""
    p: dict[str, object] = dict(_BASE_PAYLOAD)
    p.update(overrides)
    return p


class TestDataLineageApi:
    async def test_list_empty(self, client) -> None:
        resp = await client.get("/api/v1/lineage/edges")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_get_update_disable_roundtrip(self, client) -> None:
        # CREATE
        resp = await client.post("/api/v1/lineage/edges", json=_payload())
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["sourceLayer"] == "SOURCE_SYSTEM"
        assert body["sourceSystem"] == "ERP"
        assert body["sourceObject"] == "PORDER"
        assert body["sourceField"] == "ORDERQTY"
        assert body["targetLayer"] == "ODS"
        assert body["refreshFrequency"] == "DAILY"
        assert body["isActive"] is True
        assert body["id"] > 0

        edge_id = body["id"]

        # GET
        detail = await client.get(f"/api/v1/lineage/edges/{edge_id}")
        assert detail.status_code == 200
        assert detail.json()["id"] == edge_id

        # UPDATE（局部）
        upd = await client.put(
            f"/api/v1/lineage/edges/{edge_id}",
            json={"refreshFrequency": "HOURLY", "owner": "新责任方"},
        )
        assert upd.status_code == 200, upd.text
        upd_body = upd.json()
        assert upd_body["refreshFrequency"] == "HOURLY"
        assert upd_body["owner"] == "新责任方"
        # 未更新字段保留
        assert upd_body["sourceLayer"] == "SOURCE_SYSTEM"

        # LIST 含 1 条
        listing = await client.get("/api/v1/lineage/edges")
        assert listing.status_code == 200
        assert len(listing.json()) == 1

        # DISABLE（软删除）
        delete = await client.delete(f"/api/v1/lineage/edges/{edge_id}")
        assert delete.status_code == 204
        after = await client.get(f"/api/v1/lineage/edges/{edge_id}")
        assert after.status_code == 200
        assert after.json()["isActive"] is False

    async def test_table_level_edge_null_fields(self, client) -> None:
        """表级血缘：source/target_field 都为空。"""
        resp = await client.post(
            "/api/v1/lineage/edges",
            json=_payload(sourceField=None, targetField=None),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["sourceField"] is None
        assert body["targetField"] is None

    async def test_duplicate_edge_returns_422(self, client) -> None:
        """同上下游 + 字段组合不允许重复。"""
        first = await client.post("/api/v1/lineage/edges", json=_payload())
        assert first.status_code == 201
        dup = await client.post("/api/v1/lineage/edges", json=_payload())
        assert dup.status_code == 422

    async def test_self_loop_returns_422(self, client) -> None:
        """同层同对象同字段自指 → 422。"""
        payload = _payload(
            sourceLayer="ODS",
            sourceSystem="ODS",
            sourceObject="X",
            sourceField="F",
            targetLayer="ODS",
            targetSystem="ODS",
            targetObject="X",
            targetField="F",
        )
        resp = await client.post("/api/v1/lineage/edges", json=payload)
        assert resp.status_code == 422

    async def test_get_not_found_returns_404(self, client) -> None:
        resp = await client.get("/api/v1/lineage/edges/999999")
        assert resp.status_code == 404

    async def test_list_filters_by_source_layer(self, client) -> None:
        """按 sourceLayer 过滤：插入 2 条不同源层，返回 1 条。"""
        await client.post(
            "/api/v1/lineage/edges",
            json=_payload(sourceLayer="SOURCE_SYSTEM", sourceObject="PORDER"),
        )
        await client.post(
            "/api/v1/lineage/edges",
            json=_payload(
                sourceLayer="ODS",
                sourceSystem="ODS",
                sourceObject="ODS_PO",
                targetLayer="DWD",
                targetSystem="DWD",
                targetObject="DWD_PO",
                sourceField=None,
                targetField=None,
            ),
        )
        resp = await client.get("/api/v1/lineage/edges?sourceLayer=SOURCE_SYSTEM")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["sourceLayer"] == "SOURCE_SYSTEM"

    async def test_list_filters_by_active_only_excludes_disabled(self, client) -> None:
        created = await client.post("/api/v1/lineage/edges", json=_payload())
        edge_id = created.json()["id"]
        await client.delete(f"/api/v1/lineage/edges/{edge_id}")

        all_rows = await client.get("/api/v1/lineage/edges")
        assert len(all_rows.json()) == 1

        active_rows = await client.get("/api/v1/lineage/edges?activeOnly=true")
        assert active_rows.json() == []

    async def test_invalid_enum_returns_422(self, client) -> None:
        """非枚举值应被 Pydantic 拒绝。"""
        resp = await client.post(
            "/api/v1/lineage/edges",
            json=_payload(sourceLayer="INVALID_LAYER"),
        )
        assert resp.status_code == 422

    async def test_table_level_edge_with_same_source_and_target_is_422(self, client) -> None:
        """表级血缘同上下游（source/target_field 都为 None）→ 自指。"""
        payload = _payload(
            sourceLayer="ODS",
            sourceSystem="ODS",
            sourceObject="X",
            sourceField=None,
            targetLayer="ODS",
            targetSystem="ODS",
            targetObject="X",
            targetField=None,
        )
        resp = await client.post("/api/v1/lineage/edges", json=payload)
        assert resp.status_code == 422