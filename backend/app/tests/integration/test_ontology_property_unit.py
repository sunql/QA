"""ontology_property.unit 落库与清空（真实 PG + 完整 API 链路）。"""

from __future__ import annotations

from httpx import AsyncClient

_ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


async def _createClass(client: AsyncClient, className: str) -> int:
    """经 API 建类（与 test_ontology_api 同一入口），返回 classId。"""
    response = await client.post(
        "/api/v1/ontology/classes",
        json={"className": className, "sourceTable": f"zjth.{className.lower()}"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def test_create_and_update_unit_persists(client: AsyncClient) -> None:
    classId = await _createClass(client, "UnitReceipt")
    created = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": classId,
            "propertyName": "RCV_QTY_PUU",
            "dataType": "DECIMAL",
            "unit": "件",
        },
    )
    assert created.status_code == 201, created.text
    prop = created.json()
    assert prop["unit"] == "件"

    # 更新走 PUT（见 app/api/v1/ontology.py:269；本仓没有 PATCH 路由）
    updated = await client.put(
        f"/api/v1/ontology/properties/{prop['id']}",
        json={"unit": "吨"},
        headers=_ADMIN_HEADERS,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["unit"] == "吨"


async def test_blank_unit_clears_value(client: AsyncClient) -> None:
    classId = await _createClass(client, "UnitBlank")
    created = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": classId,
            "propertyName": "RCV_QTY_PUU",
            "dataType": "DECIMAL",
            "unit": "件",
        },
    )
    assert created.status_code == 201, created.text
    propId = created.json()["id"]

    cleared = await client.put(
        f"/api/v1/ontology/properties/{propId}",
        json={"unit": ""},
        headers=_ADMIN_HEADERS,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["unit"] is None
