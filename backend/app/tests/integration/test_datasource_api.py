"""数据源管理 API 集成测试。

验证 HTTP 契约、camelCase JSON 输出、CRUD、默认排他性、
主机白名单、密码不外泄、连接测试端点。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.enums import DataSourceType


CREATE_PAYLOAD = {
    "name": "ZJTH-Oracle",
    "type": "oracle",
    "host": "192.168.205.70",
    "port": 1521,
    "databaseName": "X3V71ORA",
    "username": "ZJTH",
    "password": "secret",
    "description": "Sage X3 Oracle",
    "isActive": True,
    "isDefault": False,
}


def _make_payload(name: str = "ds", **overrides) -> dict:
    payload = {
        "name": name,
        "type": "postgresql",
        "host": "db.example.com",
        "port": 5432,
        "databaseName": "appdb",
        "username": "u",
        "password": "p",
        "description": None,
        "isActive": True,
        "isDefault": False,
    }
    payload.update(overrides)
    return payload


class TestDataSourceCrud:
    async def test_create_returns_201_camel_case_no_password(self, client) -> None:
        resp = await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "ZJTH-Oracle"
        assert body["type"] == "oracle"
        assert body["databaseName"] == "X3V71ORA"
        assert body["isActive"] is True
        assert body["isDefault"] is False
        # 测试客户端未携带 X-User-Id 头，getCurrentUser 返回默认值 anonymous
        assert body["createdBy"] == "anonymous"
        assert "password" not in body
        assert "passwordEncrypted" not in body

    async def test_create_duplicate_returns_422(self, client) -> None:
        await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        resp = await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        assert resp.status_code == 422
        assert "已存在" in resp.json()["error"]

    async def test_list_returns_created(self, client) -> None:
        await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        resp = await client.get("/api/v1/datasources")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["name"] == "ZJTH-Oracle"

    async def test_list_active_only_excludes_inactive(self, client) -> None:
        create = await client.post("/api/v1/datasources", json=_make_payload(isActive=False))
        dsId = create.json()["id"]
        resp = await client.get("/api/v1/datasources", params={"activeOnly": "true"})
        assert resp.status_code == 200
        assert resp.json() == []
        # 清理
        await client.delete(f"/api/v1/datasources/{dsId}")

    async def test_get_by_id(self, client) -> None:
        create = await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        dsId = create.json()["id"]
        resp = await client.get(f"/api/v1/datasources/{dsId}")
        assert resp.status_code == 200
        assert resp.json()["id"] == dsId

    async def test_get_nonexistent_returns_404(self, client) -> None:
        resp = await client.get("/api/v1/datasources/99999")
        assert resp.status_code == 404

    async def test_update_without_password_keeps_credentials(self, client) -> None:
        create = await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        dsId = create.json()["id"]
        resp = await client.put(
            f"/api/v1/datasources/{dsId}",
            json={"description": "updated desc"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "updated desc"

    async def test_update_with_password(self, client) -> None:
        create = await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        dsId = create.json()["id"]
        resp = await client.put(
            f"/api/v1/datasources/{dsId}",
            json={"password": "new-secret"},
        )
        assert resp.status_code == 200, resp.text
        assert "password" not in resp.json()

    async def test_delete_returns_204_then_404(self, client) -> None:
        create = await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        dsId = create.json()["id"]
        resp = await client.delete(f"/api/v1/datasources/{dsId}")
        assert resp.status_code == 204
        resp = await client.get(f"/api/v1/datasources/{dsId}")
        assert resp.status_code == 404


class TestDefaultExclusivity:
    async def test_new_default_clears_existing(self, client) -> None:
        ds1 = await client.post("/api/v1/datasources", json=_make_payload("ds1", isDefault=True))
        assert ds1.json()["isDefault"] is True

        ds2 = await client.post("/api/v1/datasources", json=_make_payload("ds2", isDefault=True))
        assert ds2.json()["isDefault"] is True

        resp = await client.get(f"/api/v1/datasources/{ds1.json()['id']}")
        assert resp.json()["isDefault"] is False

    async def test_update_default_clears_others(self, client) -> None:
        ds1 = await client.post("/api/v1/datasources", json=_make_payload("ds1", isDefault=True))
        ds2 = await client.post("/api/v1/datasources", json=_make_payload("ds2", isDefault=False))

        await client.put(
            f"/api/v1/datasources/{ds2.json()['id']}",
            json={"isDefault": True},
        )
        resp = await client.get(f"/api/v1/datasources/{ds1.json()['id']}")
        assert resp.json()["isDefault"] is False

    async def test_delete_default_promotes_next_active(self, client) -> None:
        ds1 = await client.post("/api/v1/datasources", json=_make_payload("ds1", isDefault=True))
        ds2 = await client.post("/api/v1/datasources", json=_make_payload("ds2", isDefault=False))

        await client.delete(f"/api/v1/datasources/{ds1.json()['id']}")

        resp = await client.get(f"/api/v1/datasources/{ds2.json()['id']}")
        assert resp.json()["isDefault"] is True


class TestHostAllowlist:
    async def test_create_host_not_in_allowlist_returns_422(self, client, monkeypatch) -> None:
        fakeSettings = SimpleNamespace(
            datasourceHosts=["10.0.0.1"],
            # 沿用 config.py 新默认（取消行数上限）；本测试只校验 host 白名单，不触达 fetchmany
            queryRowLimit=0,
            queryTimeoutSeconds=30,
        )
        monkeypatch.setattr(
            "app.services.datasource_service.getSettings", lambda: fakeSettings
        )
        resp = await client.post("/api/v1/datasources", json=_make_payload(host="evil.example.com"))
        assert resp.status_code == 422
        assert "不在允许列表" in resp.json()["error"]


class TestConnectionEndpoint:
    @staticmethod
    def _fakeAdapter(success: bool, message: str):
        class _Fake:
            async def test(self) -> tuple[bool, str]:
                return success, message

            async def dispose(self) -> None:
                pass

        return _Fake()

    async def test_test_endpoint_success(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.services.datasource_service.build_adapter",
            lambda *a, **kw: self._fakeAdapter(True, "连接成功"),
        )
        resp = await client.post(
            "/api/v1/datasources/test",
            json={
                "type": "oracle",
                "host": "192.168.205.70",
                "port": 1521,
                "databaseName": "X3V71ORA",
                "username": "ZJTH",
                "password": "secret",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert "连接成功" in body["message"]

    async def test_test_endpoint_failure(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.services.datasource_service.build_adapter",
            lambda *a, **kw: self._fakeAdapter(False, "connection refused"),
        )
        resp = await client.post(
            "/api/v1/datasources/test",
            json={
                "type": "postgresql",
                "host": "db.example.com",
                "port": 5432,
                "databaseName": "appdb",
                "username": "u",
                "password": "p",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "connection refused" in body["message"]
