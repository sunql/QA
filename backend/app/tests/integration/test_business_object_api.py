"""business_object REST 端点集成测试."""
import pytest
from httpx import AsyncClient


pytestmark = pytest.mark.asyncio
async def test_list_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/business-objects")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_get_update_roundtrip(client: AsyncClient) -> None:
    # graph_label omitted so service receives header_class_id=None, graph_label=None (valid)
    payload = {"code": "SUPPLIER", "name": "供应商"}
    resp = await client.post("/api/v1/business-objects", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "SUPPLIER"
    assert body["name"] == "供应商"

    resp = await client.get("/api/v1/business-objects/SUPPLIER")
    assert resp.status_code == 200
    assert resp.json()["code"] == "SUPPLIER"

    resp = await client.put(
        "/api/v1/business-objects/SUPPLIER", json={"name": "供应商（新）"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "供应商（新）"


@pytest.mark.asyncio
async def test_duplicate_code_returns_409(client: AsyncClient) -> None:
    payload = {"code": "MATERIAL", "name": "物料"}
    resp = await client.post("/api/v1/business-objects", json=payload)
    assert resp.status_code == 201
    resp = await client.post("/api/v1/business-objects", json=payload)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_get_not_found_returns_404(client: AsyncClient) -> None:
    # Use valid BusinessObjectCode that doesn't exist in DB (UNKNOWN fails Pydantic validation)
    resp = await client.get("/api/v1/business-objects/IQC")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_invalid_code_returns_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/business-objects",
        json={"code": "INVALID", "name": "X"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_graph_label_mismatch_returns_422(
    client: AsyncClient
) -> None:
    """没有 header_class 时 graph_label 必须 NULL；提供不匹配的报 422."""
    resp = await client.post(
        "/api/v1/business-objects",
        json={
            "code": "SUPPLIER",
            "name": "供应商",
            "header_class_id": None,
            "graph_label": "Supplier",  # 二者不一致
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_returns_204(client: AsyncClient) -> None:
    payload = {"code": "NCR", "name": "不合格处理"}
    resp = await client.post("/api/v1/business-objects", json=payload)
    assert resp.status_code == 201
    resp = await client.delete("/api/v1/business-objects/NCR")
    assert resp.status_code == 204
    resp = await client.get("/api/v1/business-objects/NCR")
    assert resp.status_code == 404
