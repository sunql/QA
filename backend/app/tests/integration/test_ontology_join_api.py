"""本体关联关系目录（ontology_join）API 集成测试（真实 PostgreSQL + 完整 API 链路）。

验证 HTTP 契约（camelCase JSON）：
- GET    /api/v1/ontology/joins
- POST   /api/v1/ontology/joins
- DELETE /api/v1/ontology/joins/{id}

覆盖：创建（201 + 字段回显）、列表、列数不匹配（422）、重复 join_key（422）、
目标类不存在（404）、删除（204/404）。
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
    assert resp.status_code == 201
    return resp.json()["id"]


def _joinPayload(srcId: int, tgtId: int, *, srcCols=None, tgtCols=None) -> dict:
    return {
        "sourceClassId": srcId,
        "sourceColumns": srcCols if srcCols is not None else ["BPTNUM_0"],
        "targetClassId": tgtId,
        "targetColumns": tgtCols if tgtCols is not None else ["BPRNUM_0"],
    }


class TestOntologyJoinApi:
    async def test_list_joins_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/ontology/joins")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_and_list_join(self, client: AsyncClient) -> None:
        srcId = await _createClass(client, "PRECEIPT", "ZJTH.PRECEIPT")
        tgtId = await _createClass(client, "BPARTNER", "ZJTH.BPARTNER")

        payload = _joinPayload(srcId, tgtId, tgtCols=["BPRNUM_0"])
        resp = await client.post("/api/v1/ontology/joins", json=payload)
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] > 0
        assert body["sourceClassId"] == srcId
        assert body["sourceColumns"] == ["BPTNUM_0"]
        assert body["targetClassId"] == tgtId
        assert body["targetColumns"] == ["BPRNUM_0"]
        assert body["joinType"] == "INNER"
        assert body["relationType"] == "business"
        assert body["joinKey"] == f"{srcId}|BPTNUM_0->{tgtId}|BPRNUM_0"

        listing = await client.get("/api/v1/ontology/joins")
        assert listing.status_code == 200
        assert len(listing.json()) == 1
        assert listing.json()[0]["sourceColumns"] == ["BPTNUM_0"]

    async def test_create_column_count_mismatch_rejected(self, client: AsyncClient) -> None:
        srcId = await _createClass(client, "PRECEIPT", "ZJTH.PRECEIPT")
        tgtId = await _createClass(client, "BPARTNER", "ZJTH.BPARTNER")

        payload = _joinPayload(
            srcId, tgtId,
            srcCols=["PTHNUM_0", "PTDLIN_0"],
            tgtCols=["PTHNUM_0"],
        )
        resp = await client.post("/api/v1/ontology/joins", json=payload)
        assert resp.status_code == 422

    async def test_create_duplicate_join_key_conflict(self, client: AsyncClient) -> None:
        srcId = await _createClass(client, "PRECEIPT", "ZJTH.PRECEIPT")
        tgtId = await _createClass(client, "BPARTNER", "ZJTH.BPARTNER")

        payload = _joinPayload(srcId, tgtId)
        first = await client.post("/api/v1/ontology/joins", json=payload)
        assert first.status_code == 201
        dup = await client.post("/api/v1/ontology/joins", json=payload)
        assert dup.status_code == 422

    async def test_create_target_class_missing_not_found(self, client: AsyncClient) -> None:
        srcId = await _createClass(client, "PRECEIPT", "ZJTH.PRECEIPT")
        payload = _joinPayload(srcId, 999999)
        resp = await client.post("/api/v1/ontology/joins", json=payload)
        assert resp.status_code == 404

    async def test_delete_join_and_nonexistent(self, client: AsyncClient) -> None:
        srcId = await _createClass(client, "PRECEIPT", "ZJTH.PRECEIPT")
        tgtId = await _createClass(client, "BPARTNER", "ZJTH.BPARTNER")

        created = await client.post(
            "/api/v1/ontology/joins", json=_joinPayload(srcId, tgtId)
        )
        join_id = created.json()["id"]

        resp = await client.delete(f"/api/v1/ontology/joins/{join_id}")
        assert resp.status_code == 204
        listing = await client.get("/api/v1/ontology/joins")
        assert listing.json() == []

        missing = await client.delete("/api/v1/ontology/joins/999999")
        assert missing.status_code == 404
