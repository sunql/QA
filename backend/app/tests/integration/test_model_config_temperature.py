"""audit-fx-5.1: temperature 字段创建往返测试。

验证 POST /api/v1/models 携带 temperature: 0.7 → 201 返回值含 temperature: 0.7，
且后续 GET /api/v1/models/{id} 同样返回 temperature: 0.7。
"""

from __future__ import annotations

import math

import pytest
from httpx import AsyncClient

# adminToken fixture lives in sibling module — import via conftest or define locally
# (sibling import works when conftest.py at same level exports it)
from app.tests.integration.test_model_config_rbac import adminToken  # noqa: F401


@pytest.mark.asyncio
async def test_temperature_persisted_on_create(client: AsyncClient, adminToken: str) -> None:
    """POST 时给 temperature=0.7，创建成功且返回值含该值；GET 一次也一致。"""
    headers = {"Authorization": f"Bearer {adminToken}"}
    payload = {
        "modelName": "temp-roundtrip-test",
        "provider": "openai",
        "apiEndpoint": "https://api.openai.com/v1",
        "apiKey": "sk-test-temp",
        "costPer1kInput": "0.03",
        "costPer1kOutput": "0.06",
        "maxInputTokens": 8000,
        "weight": 30,
        "costThreshold": "0.05",
        "isActive": True,
        "temperature": 0.7,
    }
    # Act — create
    resp = await client.post("/api/v1/models", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["modelName"] == "temp-roundtrip-test"
    # temperature 必须出现在返回值中（这是 audit-fx-5.1 的核心验证）
    assert "temperature" in body
    assert math.isclose(body["temperature"], 0.7, abs_tol=1e-6), (
        f"expected temperature=0.7, got {body['temperature']}"
    )

    # Act — retrieve
    config_id = body["id"]
    get_resp = await client.get(f"/api/v1/models/{config_id}", headers=headers)
    assert get_resp.status_code == 200
    get_body = get_resp.json()
    assert math.isclose(get_body["temperature"], 0.7, abs_tol=1e-6), (
        f"GET temperature expected 0.7, got {get_body['temperature']}"
    )
