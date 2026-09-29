"""business_object REST 端点集成测试.

conftest 的 autouse fixture 在 TRUNCATE 后 seed 6 个内置 business object
（SUPPLIER/MATERIAL/PO/GR/IQC/NCR，见 scripts/seed_business_objects.py）。
涉及唯一 code 冲突的用例改用 seed 清单外的 XTEST* 编码（BusinessObjectCodeNew
format-only，create 允许），避免与预置种子撞 409。
"""
import pytest
from httpx import AsyncClient

# autouse seed 覆盖的内置 code（scripts/seed_business_objects.py::_seed_rows）
SEEDED_CODES = {"SUPPLIER", "MATERIAL", "PO", "GR", "IQC", "NCR"}


pytestmark = pytest.mark.asyncio
async def test_list_contains_seeded_rows(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/business-objects")
    assert resp.status_code == 200
    assert SEEDED_CODES <= {r["code"] for r in resp.json()}


@pytest.mark.asyncio
async def test_create_get_update_roundtrip(client: AsyncClient) -> None:
    # graph_label omitted so service receives header_class_id=None, graph_label=None (valid)
    payload = {"code": "XTEST", "name": "测试对象"}
    resp = await client.post("/api/v1/business-objects", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "XTEST"
    assert body["name"] == "测试对象"

    resp = await client.get("/api/v1/business-objects/XTEST")
    assert resp.status_code == 200
    assert resp.json()["code"] == "XTEST"

    resp = await client.put(
        "/api/v1/business-objects/XTEST", json={"name": "测试对象（新）"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "测试对象（新）"


@pytest.mark.asyncio
async def test_duplicate_code_returns_409(client: AsyncClient) -> None:
    # MATERIAL 由 autouse seed 预置 → 直接撞唯一约束
    payload = {"code": "MATERIAL", "name": "物料"}
    resp = await client.post("/api/v1/business-objects", json=payload)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_get_not_found_returns_404(client: AsyncClient) -> None:
    # XMISSING：格式合法（create 允许）但不在库中
    resp = await client.get("/api/v1/business-objects/XMISSING")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_invalid_code_returns_422(client: AsyncClient) -> None:
    # create 已改 format-only（BusinessObjectCodeNew）：不再有闭合枚举，
    # 但格式违规（小写）仍 422
    resp = await client.post(
        "/api/v1/business-objects",
        json={"code": "invalid", "name": "X"},
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
            "code": "XTEST",
            "name": "测试对象",
            "header_class_id": None,
            "graph_label": "Supplier",  # 二者不一致
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_returns_204(client: AsyncClient) -> None:
    payload = {"code": "XTEST2", "name": "待删对象"}
    resp = await client.post("/api/v1/business-objects", json=payload)
    assert resp.status_code == 201
    resp = await client.delete("/api/v1/business-objects/XTEST2")
    assert resp.status_code == 204
    resp = await client.get("/api/v1/business-objects/XTEST2")
    assert resp.status_code == 404
