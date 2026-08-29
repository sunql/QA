"""模型配置与会话 API 集成测试。

验证 HTTP 契约、camelCase JSON 输出、CRUD 行为与错误码。
"""

from __future__ import annotations

from decimal import Decimal


CREATE_PAYLOAD = {
    "modelName": "gpt-4o",
    "provider": "openai",
    "apiEndpoint": "https://api.openai.com/v1",
    "apiKey": "sk-test-key",
    "costPer1kInput": "0.03",
    "costPer1kOutput": "0.06",
    "maxInputTokens": 8000,
    "weight": 30,
    "costThreshold": "0.05",
    "isActive": True,
}


class TestModelConfigApi:
    async def test_create_returns_201_with_camel_case_json(self, client) -> None:
        # Act
        resp = await client.post("/api/v1/models", json=CREATE_PAYLOAD)
        # Assert
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["modelName"] == "gpt-4o"
        assert body["provider"] == "openai"
        assert body["isActive"] is True
        assert body["weight"] == 30
        assert "api_key" not in body  # 不泄露密钥
        assert "apiKeyEncrypted" not in body

    async def test_create_duplicate_returns_422(self, client) -> None:
        # Arrange
        await client.post("/api/v1/models", json=CREATE_PAYLOAD)
        # Act
        resp = await client.post("/api/v1/models", json=CREATE_PAYLOAD)
        # Assert
        assert resp.status_code == 422
        assert "已存在" in resp.json()["error"]

    async def test_list_returns_created_config(self, client) -> None:
        # Arrange
        await client.post("/api/v1/models", json=CREATE_PAYLOAD)
        # Act
        resp = await client.get("/api/v1/models")
        # Assert
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["modelName"] == "gpt-4o"

    async def test_list_active_only_excludes_deactivated(self, client) -> None:
        # Arrange
        create = await client.post("/api/v1/models", json=CREATE_PAYLOAD)
        configId = create.json()["id"]
        await client.delete(f"/api/v1/models/{configId}")
        # Act
        resp = await client.get("/api/v1/models", params={"activeOnly": "true"})
        # Assert
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_get_by_id_returns_config(self, client) -> None:
        # Arrange
        create = await client.post("/api/v1/models", json=CREATE_PAYLOAD)
        configId = create.json()["id"]
        # Act
        resp = await client.get(f"/api/v1/models/{configId}")
        # Assert
        assert resp.status_code == 200
        assert resp.json()["id"] == configId

    async def test_get_nonexistent_returns_404(self, client) -> None:
        resp = await client.get("/api/v1/models/99999")
        assert resp.status_code == 404

    async def test_update_modifies_fields(self, client) -> None:
        # Arrange
        create = await client.post("/api/v1/models", json=CREATE_PAYLOAD)
        configId = create.json()["id"]
        # Act
        resp = await client.put(
            f"/api/v1/models/{configId}",
            json={"weight": 50, "isActive": False, "apiKey": "sk-new-key"},
        )
        # Assert
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["weight"] == 50
        assert body["isActive"] is False

    async def test_delete_deactivates_and_returns_204(self, client) -> None:
        # Arrange
        create = await client.post("/api/v1/models", json=CREATE_PAYLOAD)
        configId = create.json()["id"]
        # Act
        resp = await client.delete(f"/api/v1/models/{configId}")
        # Assert
        assert resp.status_code == 204
        get = await client.get(f"/api/v1/models/{configId}")
        assert get.json()["isActive"] is False


class TestSessionUsageApi:
    async def test_list_sessions_returns_aggregates(self, client) -> None:
        from app.infrastructure.database import getSessionFactory
        from app.services.token_usage_service import TokenUsageService

        svc = TokenUsageService()
        async with getSessionFactory()() as session:
            await svc.recordUsage(
                session, sessionId="sess_a", modelConfigId=None, modelName="gpt-4o",
                promptTokens=100, completionTokens=50, cost=Decimal("0.005"), purpose="nl2sql",
            )
        # Act
        resp = await client.get("/api/v1/sessions")
        # Assert
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["sessionId"] == "sess_a"
        assert items[0]["totalRequests"] == 1
        assert items[0]["totalTokens"] == 150
        assert Decimal(items[0]["totalCost"]) == Decimal("0.005")
        assert items[0]["lastQuestion"] is None
        # camelCase 时间字段
        assert "lastRequestTime" in items[0]
        assert "firstRequestTime" in items[0]

    async def test_list_sessions_empty(self, client) -> None:
        resp = await client.get("/api/v1/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_usage_summary_for_empty_session(self, client) -> None:
        resp = await client.get("/api/v1/sessions/sess_empty/usage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["sessionId"] == "sess_empty"
        assert body["totalRequests"] == 0
        assert body["totalTokens"] == 0
        assert Decimal(body["totalCost"]) == Decimal("0")

    async def test_usage_summary_reflects_recorded_usage(self, client) -> None:
        # Arrange: 通过 service 写入一条流水
        from app.infrastructure.database import getSessionFactory
        from app.services.token_usage_service import TokenUsageService

        svc = TokenUsageService()
        async with getSessionFactory()() as session:
            await svc.recordUsage(
                session,
                sessionId="sess_1",
                modelConfigId=None,
                modelName="gpt-4o",
                promptTokens=100,
                completionTokens=50,
                cost=Decimal("0.005"),
                purpose="chat",
            )
        # Act
        resp = await client.get("/api/v1/sessions/sess_1/usage")
        # Assert
        assert resp.status_code == 200
        body = resp.json()
        assert body["totalRequests"] == 1
        assert body["totalTokens"] == 150
        assert Decimal(body["totalCost"]) == Decimal("0.005")

    async def test_usage_list_returns_detail(self, client) -> None:
        from app.infrastructure.database import getSessionFactory
        from app.services.token_usage_service import TokenUsageService

        svc = TokenUsageService()
        async with getSessionFactory()() as session:
            await svc.recordUsage(
                session,
                sessionId="sess_2",
                modelConfigId=None,
                modelName="llama3.1",
                promptTokens=10,
                completionTokens=5,
                cost=Decimal("0"),
                purpose="chat",
            )
        resp = await client.get("/api/v1/sessions/sess_2/usage/list")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["modelName"] == "llama3.1"
        assert items[0]["totalTokens"] == 15
