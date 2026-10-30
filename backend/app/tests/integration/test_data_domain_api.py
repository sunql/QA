"""data_domain REST 端点集成测试（feat-data-domain-layer 方案 1）.

conftest autouse 已 seed 3 个内置域（PROCUREMENT/QUALITY/LOGISTICS，均 enabled）
+ 数据层规则 7 行。本文件只造 XDOM* 编码的自定义域，避免与预置种子撞 409。
"""
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_list_contains_seeded_domains(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/data-domains")
    assert resp.status_code == 200
    codes = {r["code"] for r in resp.json()}
    assert {"PROCUREMENT", "QUALITY", "LOGISTICS"} <= codes
    # 列表带主管理组织名（procurement org 由 seed 尽力解析）
    procurement = next(r for r in resp.json() if r["code"] == "PROCUREMENT")
    assert procurement["enabled"] is True


async def test_get_detail(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/data-domains/PROCUREMENT")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "PROCUREMENT"
    assert body["name"] == "采购域"


async def test_get_not_found_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/data-domains/NO_SUCH_DOMAIN")
    assert resp.status_code == 404


async def test_create_read_update_delete_roundtrip(client: AsyncClient) -> None:
    payload = {
        "code": "XDOM",
        "name": "测试域",
        "description": "集成测试用",
    }
    resp = await client.post("/api/v1/data-domains", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "XDOM"
    assert body["enabled"] is True

    resp = await client.get("/api/v1/data-domains/XDOM")
    assert resp.status_code == 200

    resp = await client.put(
        "/api/v1/data-domains/XDOM", json={"name": "测试域（新）", "enabled": False}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "测试域（新）"
    assert body["enabled"] is False

    # 禁用后 enabledOnly=true 的下拉口径不再返回（默认全量供管理页）
    resp = await client.get("/api/v1/data-domains", params={"enabledOnly": "true"})
    assert "XDOM" not in {r["code"] for r in resp.json()}
    # 默认全量（含禁用）仍返回
    resp = await client.get("/api/v1/data-domains")
    assert "XDOM" in {r["code"] for r in resp.json()}

    resp = await client.delete("/api/v1/data-domains/XDOM")
    assert resp.status_code == 204
    resp = await client.get("/api/v1/data-domains/XDOM")
    assert resp.status_code == 404


async def test_duplicate_code_returns_409(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/data-domains", json={"code": "PROCUREMENT", "name": "撞码"}
    )
    assert resp.status_code == 409


async def test_invalid_code_format_returns_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/data-domains", json={"code": "bad code", "name": "X"}
    )
    assert resp.status_code == 422


async def test_delete_referenced_domain_returns_409(
    client: AsyncClient,
) -> None:
    """PROCUREMENT 被 seed 业务对象 primary_domain_id 引用 → 删除 409。"""
    resp = await client.delete("/api/v1/data-domains/PROCUREMENT")
    assert resp.status_code == 409


async def test_create_with_unknown_org_returns_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/data-domains",
        json={"code": "XDOMORG", "name": "组织不存在", "primary_org_id": 999999},
    )
    assert resp.status_code == 404
