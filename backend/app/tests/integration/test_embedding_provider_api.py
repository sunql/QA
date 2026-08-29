"""Embedding 服务注册表 API 集成测试（真实 PG + 完整 API 链路）。

验证 HTTP 契约、camelCase JSON、CRUD、单活互斥与 key 不泄漏。
"""

from __future__ import annotations

import pytest

CREATE_PAYLOAD = {
    "name": "Ollama bge-m3",
    "providerType": "ollama",
    "baseUrl": "http://localhost:11434/v1",
    "modelName": "bge-m3:latest",
    "apiKey": "ollama",
    "dimension": 1024,
    "isActive": True,
}

CREATE_PAYLOAD_INACTIVE = {
    "name": "oMLX bge-m3 FP16",
    "providerType": "omlx",
    "baseUrl": "http://localhost:8080/v1",
    "modelName": "bge-m3-mlx-fp16",
    "dimension": 1024,
    "isActive": False,
}


class TestEmbeddingProviderApi:
    async def test_create_returns_201_with_camel_case_json(self, client) -> None:
        # Act
        resp = await client.post("/api/v1/embedding-providers", json=CREATE_PAYLOAD)
        # Assert
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "Ollama bge-m3"
        assert body["providerType"] == "ollama"
        assert body["baseUrl"] == "http://localhost:11434/v1"
        assert body["modelName"] == "bge-m3:latest"
        assert body["dimension"] == 1024
        assert body["isActive"] is True
        # key 永不泄漏
        assert "api_key" not in body
        assert "apiKey" not in body
        assert "apiKeyEncrypted" not in body

    async def test_create_duplicate_name_returns_422(self, client) -> None:
        # Arrange
        await client.post("/api/v1/embedding-providers", json=CREATE_PAYLOAD)
        # Act
        resp = await client.post("/api/v1/embedding-providers", json=CREATE_PAYLOAD)
        # Assert
        assert resp.status_code == 422
        assert "已存在" in resp.json()["error"]

    async def test_list_returns_created_providers(self, client) -> None:
        # Arrange
        await client.post("/api/v1/embedding-providers", json=CREATE_PAYLOAD_INACTIVE)
        # Act
        resp = await client.get("/api/v1/embedding-providers")
        # Assert
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["name"] == "oMLX bge-m3 FP16"
        assert items[0]["isActive"] is False

    async def test_list_empty(self, client) -> None:
        resp = await client.get("/api/v1/embedding-providers")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_get_by_id(self, client) -> None:
        # Arrange
        create = await client.post("/api/v1/embedding-providers", json=CREATE_PAYLOAD)
        providerId = create.json()["id"]
        # Act
        resp = await client.get(f"/api/v1/embedding-providers/{providerId}")
        # Assert
        assert resp.status_code == 200
        assert resp.json()["id"] == providerId
        assert resp.json()["name"] == "Ollama bge-m3"

    async def test_get_nonexistent_returns_404(self, client) -> None:
        resp = await client.get("/api/v1/embedding-providers/99999")
        assert resp.status_code == 404

    async def test_update_modifies_fields(self, client) -> None:
        # Arrange
        create = await client.post("/api/v1/embedding-providers", json=CREATE_PAYLOAD_INACTIVE)
        providerId = create.json()["id"]
        # Act
        resp = await client.put(
            f"/api/v1/embedding-providers/{providerId}",
            json={"dimension": 1024, "baseUrl": "http://localhost:8081/v1"},
        )
        # Assert
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["baseUrl"] == "http://localhost:8081/v1"
        assert body["dimension"] == 1024

    async def test_update_rename_to_existing_name_returns_422(self, client) -> None:
        # Arrange: A 已存在；B 改名成 A 的名字
        await client.post("/api/v1/embedding-providers", json=CREATE_PAYLOAD)
        b = await client.post("/api/v1/embedding-providers", json=CREATE_PAYLOAD_INACTIVE)
        bId = b.json()["id"]
        # Act
        resp = await client.put(f"/api/v1/embedding-providers/{bId}", json={"name": "Ollama bge-m3"})
        # Assert: 422 而非唯一约束 500
        assert resp.status_code == 422
        assert "已存在" in resp.json()["error"]

    async def test_update_clears_api_key_with_null(self, client, dbSession) -> None:
        from app.domain.models import EmbeddingProvider

        # Arrange: 带 key 创建，确认已加密落库
        create = await client.post(
            "/api/v1/embedding-providers",
            json={**CREATE_PAYLOAD_INACTIVE, "apiKey": "secret-key"},
        )
        providerId = create.json()["id"]
        stored = await dbSession.get(EmbeddingProvider, providerId)
        assert stored.api_key_encrypted is not None
        # Act: 显式置空 key
        resp = await client.put(
            f"/api/v1/embedding-providers/{providerId}", json={"apiKey": None}
        )
        # Assert: DB 中已清除（refresh 绕过 dbSession 身份映射的陈旧缓存）
        assert resp.status_code == 200, resp.text
        await dbSession.refresh(stored)
        assert stored.api_key_encrypted is None

    async def test_db_partial_unique_index_enforces_single_active(self, dbSession) -> None:
        """单活互斥的 DB 层兜底：两个激活行直插，第二个被部分唯一索引拒绝。"""
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        insert = text(
            "INSERT INTO embedding_provider"
            " (name, provider_type, base_url, model_name, dimension, is_active,"
            "  created_time, updated_time)"
            " VALUES (:name, 'ollama', 'http://x/v1', 'm', 1024, true, NOW(), NOW())"
        )
        async with dbSession.begin():
            await dbSession.execute(insert, {"name": "active-a"})
        with pytest.raises(IntegrityError):
            async with dbSession.begin():
                await dbSession.execute(insert, {"name": "active-b"})

    async def test_delete_soft_deactivates_returns_204(self, client) -> None:
        # Arrange
        create = await client.post("/api/v1/embedding-providers", json=CREATE_PAYLOAD)
        providerId = create.json()["id"]
        # Act
        resp = await client.delete(f"/api/v1/embedding-providers/{providerId}")
        # Assert
        assert resp.status_code == 204
        get = await client.get(f"/api/v1/embedding-providers/{providerId}")
        assert get.json()["isActive"] is False

    async def test_active_returns_404_when_none_active(self, client) -> None:
        resp = await client.get("/api/v1/embedding-providers/active")
        assert resp.status_code == 404

    async def test_activate_is_single_and_exclusive(self, client) -> None:
        # Arrange: 两个非激活服务
        a = await client.post("/api/v1/embedding-providers", json=CREATE_PAYLOAD_INACTIVE)
        aId = a.json()["id"]
        bPayload = {**CREATE_PAYLOAD_INACTIVE, "name": "oMLX bge-m3 8bit", "modelName": "bge-m3-mlx-8bit"}
        b = await client.post("/api/v1/embedding-providers", json=bPayload)
        bId = b.json()["id"]
        # Act: 激活 B
        resp = await client.post(f"/api/v1/embedding-providers/{bId}/activate")
        # Assert: B 激活，A 被取消
        assert resp.status_code == 200, resp.text
        assert resp.json()["isActive"] is True
        aAfter = await client.get(f"/api/v1/embedding-providers/{aId}")
        assert aAfter.json()["isActive"] is False
        active = await client.get("/api/v1/embedding-providers/active")
        assert active.json()["id"] == bId

    async def test_create_active_clears_previous_active(self, client) -> None:
        # Arrange: 先建一个激活服务
        first = await client.post("/api/v1/embedding-providers", json=CREATE_PAYLOAD)
        firstId = first.json()["id"]
        # Act: 再建一个 isActive=True 的服务（单活互斥，应顶替）
        payload = {
            "name": "Ollama bge-m3 new",
            "providerType": "ollama",
            "baseUrl": "http://localhost:11434/v1",
            "modelName": "bge-m3:latest",
            "dimension": 1024,
            "isActive": True,
        }
        second = await client.post("/api/v1/embedding-providers", json=payload)
        secondId = second.json()["id"]
        # Assert
        firstAfter = await client.get(f"/api/v1/embedding-providers/{firstId}")
        assert firstAfter.json()["isActive"] is False
        active = await client.get("/api/v1/embedding-providers/active")
        assert active.json()["id"] == secondId

    async def test_update_set_active_clears_others(self, client) -> None:
        # Arrange
        a = await client.post("/api/v1/embedding-providers", json=CREATE_PAYLOAD_INACTIVE)
        aId = a.json()["id"]
        bPayload = {**CREATE_PAYLOAD_INACTIVE, "name": "oMLX bge-m3 8bit", "modelName": "bge-m3-mlx-8bit"}
        b = await client.post("/api/v1/embedding-providers", json=bPayload)
        bId = b.json()["id"]
        # Act: 把 A 更新为激活
        resp = await client.put(f"/api/v1/embedding-providers/{aId}", json={"isActive": True})
        # Assert
        assert resp.status_code == 200, resp.text
        assert resp.json()["isActive"] is True
        bAfter = await client.get(f"/api/v1/embedding-providers/{bId}")
        assert bAfter.json()["isActive"] is False
        active = await client.get("/api/v1/embedding-providers/active")
        assert active.json()["id"] == aId
