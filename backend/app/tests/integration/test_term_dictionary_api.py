"""NL2SQL 术语字典接口集成测试（真实 PostgreSQL + 完整 API 链路）。

验证 HTTP 契约（camelCase JSON）：
- GET    /api/v1/term-dictionary
- POST   /api/v1/term-dictionary
- DELETE /api/v1/term-dictionary/{id}

写入口自 fix-governance-write-gate 起限 admin；映射类/属性必须在本体库中存世。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass, OntologyProperty

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


class TestTermDictionaryApi:
    async def test_list_terms_empty(self, client) -> None:
        resp = await client.get("/api/v1/term-dictionary")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_and_list_term(
        self, client, dbSession: AsyncSession
    ) -> None:
        cls = OntologyClass(class_name="PRECEIPTD", source_table="ZJTH.PRECEIPTD")
        cls.properties = [
            OntologyProperty(property_name="收货数量", data_type="DECIMAL")
        ]
        dbSession.add(cls)
        await dbSession.commit()

        payload = {
            "term": "占比",
            "definition": "某值占总量的比例",
            "mappedClassName": "PRECEIPTD",
            "mappedPropertyName": "收货数量",
            "formulaHint": "占比 = SUM(收货数量) / SUM(SUM(收货数量)) OVER ()",
        }
        resp = await client.post(
            "/api/v1/term-dictionary", json=payload, headers=ADMIN_HEADERS
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["term"] == "占比"
        assert body["definition"] == "某值占总量的比例"
        assert body["mappedClassName"] == "PRECEIPTD"
        assert body["mappedPropertyName"] == "收货数量"
        assert body["formulaHint"] == "占比 = SUM(收货数量) / SUM(SUM(收货数量)) OVER ()"
        assert body["createdBy"] == "test-admin"
        assert body["id"] is not None

        listing = await client.get("/api/v1/term-dictionary")
        assert listing.status_code == 200
        assert len(listing.json()) == 1
        assert listing.json()[0]["term"] == "占比"

    async def test_create_duplicate_term_conflict(self, client) -> None:
        payload = {"term": "占比", "definition": "某值占总量的比例"}
        first = await client.post(
            "/api/v1/term-dictionary", json=payload, headers=ADMIN_HEADERS
        )
        assert first.status_code == 201
        dup = await client.post(
            "/api/v1/term-dictionary", json=payload, headers=ADMIN_HEADERS
        )
        assert dup.status_code == 422

    async def test_delete_term(self, client) -> None:
        payload = {"term": "实际到货", "definition": "真正入库的数量"}
        created = await client.post(
            "/api/v1/term-dictionary", json=payload, headers=ADMIN_HEADERS
        )
        term_id = created.json()["id"]
        resp = await client.delete(
            f"/api/v1/term-dictionary/{term_id}", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 204
        listing = await client.get("/api/v1/term-dictionary")
        assert listing.json() == []

    async def test_delete_nonexistent_term_not_found(self, client) -> None:
        resp = await client.delete(
            "/api/v1/term-dictionary/999999", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 404
