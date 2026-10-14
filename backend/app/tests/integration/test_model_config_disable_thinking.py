"""disable_thinking 字段的 API 往返测试（真实 PG + 公开 API）。

覆盖：创建时携带 / 默认 false / 更新勾选 / 取消勾选传 false（非 undefined）。
取消勾选必须走 exclude_unset 语义显式落库，否则用户「取消勾选」会失效。
"""

from __future__ import annotations

BASE_PAYLOAD = {
    "modelName": "minimax-test-m3",
    "provider": "openai_compatible_proxy",
    "apiEndpoint": "https://api.minimax.cn/v1",
    "apiKey": "sk-test-key",
    "costPer1kInput": "0.01",
    "costPer1kOutput": "0.02",
    "maxInputTokens": 8000,
    "weight": 10,
    "costThreshold": "0.05",
    "isActive": True,
}


class TestDisableThinkingRoundTrip:
    async def test_create_defaults_to_false(self, client) -> None:
        resp = await client.post("/api/v1/models", json=BASE_PAYLOAD)

        assert resp.status_code == 201, resp.text
        assert resp.json()["disableThinking"] is False

    async def test_create_with_true_is_persisted(self, client) -> None:
        resp = await client.post(
            "/api/v1/models", json={**BASE_PAYLOAD, "disableThinking": True}
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["disableThinking"] is True

        got = await client.get(f"/api/v1/models/{resp.json()['id']}")
        assert got.json()["disableThinking"] is True

    async def test_update_can_enable(self, client) -> None:
        created = (await client.post("/api/v1/models", json=BASE_PAYLOAD)).json()

        resp = await client.put(
            f"/api/v1/models/{created['id']}", json={"disableThinking": True}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["disableThinking"] is True

    async def test_update_can_disable_with_explicit_false(self, client) -> None:
        """取消勾选必须落库为 false——不是「不传该字段」。

        这是本用例的核心：若实现把 false 当成「未提供」跳过，
        用户取消勾选后会静默失效（配置永远关不掉 thinking）。
        """
        created = (
            await client.post(
                "/api/v1/models", json={**BASE_PAYLOAD, "disableThinking": True}
            )
        ).json()
        assert created["disableThinking"] is True

        resp = await client.put(
            f"/api/v1/models/{created['id']}", json={"disableThinking": False}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["disableThinking"] is False

        got = await client.get(f"/api/v1/models/{created['id']}")
        assert got.json()["disableThinking"] is False