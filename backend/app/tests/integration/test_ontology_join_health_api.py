"""本体 JOIN 关系健康巡检端点集成测试（真实 PostgreSQL + 完整 API 链路）。

背景（2026-09-18 DIM_SUPPLIER 孤岛事故）：零边类只在用户提问报
「无法通过关联路径连通」时才暴露。本端点把孤岛/死边变成可主动巡检的
结构化报告（三层保障的第 2 层）。

覆盖 HTTP 契约（camelCase）：
- GET /ontology/health/joins            孤岛清单（分层 + FK 语义列）+ 计数
- GET /ontology/health/joins?probe=true 死边检测（值域探针，fake 数据源）
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _createClass(client: AsyncClient, name: str, table: str) -> int:
    resp = await client.post(
        "/api/v1/ontology/classes",
        json={"className": name, "sourceTable": table},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _createProperty(
    client: AsyncClient,
    classId: int,
    name: str,
    *,
    sourceColumn: str,
    isPrimaryKey: bool = False,
    isForeignKey: bool = False,
) -> None:
    resp = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": classId,
            "propertyName": name,
            "dataType": "STRING",
            "sourceColumn": sourceColumn,
            "isPrimaryKey": isPrimaryKey,
            "isForeignKey": isForeignKey,
        },
    )
    assert resp.status_code == 201, resp.text


async def _createJoin(
    client: AsyncClient,
    sourceClassId: int,
    sourceColumns: list[str],
    targetClassId: int,
    targetColumns: list[str],
) -> None:
    resp = await client.post(
        "/api/v1/ontology/joins",
        json={
            "sourceClassId": sourceClassId,
            "sourceColumns": sourceColumns,
            "targetClassId": targetClassId,
            "targetColumns": targetColumns,
        },
    )
    assert resp.status_code == 201, resp.text


class TestJoinHealthEndpoint:
    async def test_isolated_classes_reported_with_layer_and_fk_columns(
        self, client: AsyncClient
    ) -> None:
        """零边类入清单（layer 取 source_table 首段；FK 语义列 = isForeignKey 或 *_CODE）。"""
        odsA = await _createClass(client, "HEALTH_ODS_A", "ODS_HEALTH_A")
        dwdB = await _createClass(client, "HEALTH_DWD_B", "DWD_HEALTH_B")
        dimC = await _createClass(client, "HEALTH_DIM_C", "DIM_HEALTH_C")
        await _createProperty(
            client, odsA, "PARTY_CODE", sourceColumn="PARTY_CODE", isForeignKey=True
        )
        await _createProperty(client, dwdB, "B_CODE", sourceColumn="B_CODE")
        await _createProperty(client, dimC, "C_ID", sourceColumn="C_ID", isPrimaryKey=True)
        await _createJoin(client, dwdB, ["B_CODE"], dimC, ["C_ID"])

        resp = await client.get("/api/v1/ontology/health/joins")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["totalClasses"] >= 3
        assert body["totalJoins"] >= 1
        isolated = {item["className"]: item for item in body["isolated"]}
        assert "HEALTH_ODS_A" in isolated
        assert isolated["HEALTH_ODS_A"]["layer"] == "ODS"
        assert isolated["HEALTH_ODS_A"]["fkLikeColumns"] == ["PARTY_CODE"]
        # 有一条边的类不算孤岛
        assert "HEALTH_DWD_B" not in isolated
        assert "HEALTH_DIM_C" not in isolated
        # probe=false → 死边为 null
        assert body["deadEdges"] is None

    async def test_class_without_fk_columns_still_listed(self, client: AsyncClient) -> None:
        """无 FK 语义列的零边类也入清单（fkLikeColumns 为空数组）。"""
        await _createClass(client, "HEALTH_LONELY", "HEALTH_LONELY_X")

        resp = await client.get("/api/v1/ontology/health/joins")

        assert resp.status_code == 200
        isolated = {i["className"]: i for i in resp.json()["isolated"]}
        assert "HEALTH_LONELY" in isolated
        assert isolated["HEALTH_LONELY"]["fkLikeColumns"] == []

    async def test_probe_true_detects_dead_edge(
        self, client: AsyncClient, monkeypatch
    ) -> None:
        """probe=true 时跑值域探针：两侧值域零交集 → deadEdge（overlapRatio=0）。"""
        src = await _createClass(client, "HEALTH_SRC", "HEALTH_SRC_T")
        tgt = await _createClass(client, "HEALTH_TGT", "HEALTH_TGT_T")
        await _createProperty(client, src, "S_CODE", sourceColumn="S_CODE")
        await _createProperty(client, tgt, "T_ID", sourceColumn="T_ID", isPrimaryKey=True)
        await _createJoin(client, src, ["S_CODE"], tgt, ["T_ID"])

        async def _fakeRunQuery(sql: str) -> list[dict]:
            if "HEALTH_SRC_T" in sql:
                return [{"v": "A1"}, {"v": "A2"}]
            if "HEALTH_TGT_T" in sql:
                return [{"v": "B1"}]
            raise AssertionError(f"unexpected sql: {sql}")

        import app.services.ontology_join_health_service as svc

        monkeypatch.setattr(svc, "_defaultRunQuery", _fakeRunQuery)

        resp = await client.get("/api/v1/ontology/health/joins?probe=true")

        assert resp.status_code == 200, resp.text
        dead = resp.json()["deadEdges"]
        assert dead is not None
        entry = next(
            d for d in dead
            if d["sourceClass"] == "HEALTH_SRC" and d["targetClass"] == "HEALTH_TGT"
        )
        assert entry["sourceColumn"] == "S_CODE"
        assert entry["targetColumn"] == "T_ID"
        assert entry["overlapRatio"] == 0.0

    async def test_probe_true_skips_healthy_edge(
        self, client: AsyncClient, monkeypatch
    ) -> None:
        """两侧值域有交集（重叠率>0）→ 不进 deadEdges。"""
        src = await _createClass(client, "HEALTH_SRC_OK", "HEALTH_SRC_OK_T")
        tgt = await _createClass(client, "HEALTH_TGT_OK", "HEALTH_TGT_OK_T")
        await _createProperty(client, src, "S_CODE", sourceColumn="S_CODE")
        await _createProperty(client, tgt, "T_ID", sourceColumn="T_ID", isPrimaryKey=True)
        await _createJoin(client, src, ["S_CODE"], tgt, ["T_ID"])

        async def _fakeRunQuery(sql: str) -> list[dict]:
            if "SRC_OK" in sql:
                return [{"v": "A1"}, {"v": "A2"}]
            return [{"v": "A1"}, {"v": "B9"}]

        import app.services.ontology_join_health_service as svc

        monkeypatch.setattr(svc, "_defaultRunQuery", _fakeRunQuery)

        resp = await client.get("/api/v1/ontology/health/joins?probe=true")

        assert resp.status_code == 200
        dead = [
            d for d in resp.json()["deadEdges"] or []
            if d["sourceClass"] == "HEALTH_SRC_OK"
        ]
        assert dead == []
