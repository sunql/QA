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


class _CloseSentinel:
    """带 close 记录的客户端哨兵（模拟已缓存的旧客户端）。"""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class TestModelConfigCacheInvalidation:
    """OpenAiClient 构造时固化 endpoint/key——配置编辑/停用必须失效缓存客户端，
    否则运行中的进程永远用旧配置（2026-10-03 MiniMax Connection error 根因）。"""

    async def test_update_invalidates_cached_client(self, client) -> None:
        from app.infrastructure.llm import factory as factoryModule

        factoryModule.resetFactory()
        try:
            create = await client.post("/api/v1/models", json=CREATE_PAYLOAD)
            configId = create.json()["id"]
            sentinel = _CloseSentinel()
            factoryModule._clients[configId] = sentinel
            # Act
            resp = await client.put(
                f"/api/v1/models/{configId}",
                json={
                    "apiEndpoint": "https://api.changed.example/v1",
                    # P1-2 校验（model_config_service.py:90）：apiEndpoint 变更
                    # 必须同时提供新 apiKey，防旧 key 泄露到新端点。
                    "apiKey": "sk-test-key-rotated",
                },
            )
            # Assert
            assert resp.status_code == 200, resp.text
            assert configId not in factoryModule._clients
            assert sentinel.closed is True
        finally:
            factoryModule.resetFactory()

    async def test_update_endpoint_without_apiKey_returns_422(self, client) -> None:
        """反向断言：`apiEndpoint` 变更但没带 `apiKey` 必须返回 422。

        钉死 `model_config_service.py:90` 的 audit-fx-1.1 校验，防止后续有人
        「为了过测试」移除 apiKey 字段而让旧 key 泄露到新端点（MiniMax
        Connection error 2026-10-03 根因）。
        """
        from app.infrastructure.llm import factory as factoryModule

        factoryModule.resetFactory()
        try:
            create = await client.post("/api/v1/models", json=CREATE_PAYLOAD)
            configId = create.json()["id"]
            # Act：故意只发 apiEndpoint，不发 apiKey
            resp = await client.put(
                f"/api/v1/models/{configId}",
                json={"apiEndpoint": "https://api.no-key.example/v1"},
            )
            # Assert
            assert resp.status_code == 422, (
                f"expected 422 for endpoint change without apiKey, "
                f"got {resp.status_code}: {resp.text}"
            )
            body = resp.json()
            assert "apiKey" in (body.get("error") or ""), (
                f"422 error should mention apiKey, got: {body}"
            )
        finally:
            factoryModule.resetFactory()

    async def test_deactivate_invalidates_cached_client(self, client) -> None:
        from app.infrastructure.llm import factory as factoryModule

        factoryModule.resetFactory()
        try:
            create = await client.post("/api/v1/models", json=CREATE_PAYLOAD)
            configId = create.json()["id"]
            sentinel = _CloseSentinel()
            factoryModule._clients[configId] = sentinel
            # Act
            resp = await client.delete(f"/api/v1/models/{configId}")
            # Assert
            assert resp.status_code == 204
            assert configId not in factoryModule._clients
            assert sentinel.closed is True
        finally:
            factoryModule.resetFactory()


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
                cost_source="measured",
            )
        resp = await client.get("/api/v1/sessions/sess_2/usage/list")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["modelName"] == "llama3.1"
        assert items[0]["totalTokens"] == 15
        # P1-2 读契约：成本口径必须以 camelCase 暴露（此前只写不读）
        assert items[0]["costSource"] == "measured"
