"""business_object REST 端点集成测试.

conftest 的 autouse fixture 在 TRUNCATE 后补种 header 本体类（0133 收紧
header_class_id NOT NULL），再 seed 业务对象：SUPPLIER/MATERIAL/PO/GR/IQC
（NCR 无本体类不再 seed，见 scripts/seed_business_objects.py docstring）。
涉及唯一 code 冲突的用例改用 seed 清单外的 XTEST* 编码（BusinessObjectCodeNew
format-only，create 允许），避免与预置种子撞 409。

feat-data-domain-layer：primary_domain_id 必填（有且只有一个主域），
attached_domain_ids 可选（附域，不能与主域重复）。
"""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass

# autouse seed 覆盖的内置 code（scripts/seed_business_objects.py::_seed_rows；
# IQC 依赖 IncomingInspection 类已由 conftest seedHeaderOntologyClasses 补种）
SEEDED_CODES = {"SUPPLIER", "MATERIAL", "PO", "GR", "IQC"}

pytestmark = pytest.mark.asyncio


async def _newHeaderClass(dbSession: AsyncSession, name: str) -> int:
    """造独立 header 类（rule ②：header_class_id 唯一——不能复用 seed 类，
    否则与 seed BO 撞 uq_business_object_header_class → 409）。"""
    cls = OntologyClass(
        class_name=name, created_by="test-bo-api", version=1
    )
    dbSession.add(cls)
    await dbSession.commit()
    await dbSession.refresh(cls)
    return cls.id


@pytest.mark.asyncio
async def test_list_contains_seeded_rows(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/business-objects")
    assert resp.status_code == 200
    rows = resp.json()
    assert SEEDED_CODES <= {r["code"] for r in rows}
    # 0133 后每行必有主域
    for r in rows:
        assert r["primaryDomainId"], f"{r['code']} 缺 primaryDomainId"


@pytest.mark.asyncio
async def test_create_get_update_roundtrip(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    headerId = await _newHeaderClass(dbSession, "XTestClass")
    payload = {
        "code": "XTEST",
        "name": "测试对象",
        "header_class_id": headerId,
        "primary_domain_id": "PROCUREMENT",
        "attached_domain_ids": ["LOGISTICS"],
        "graph_label": "XTestClass",  # 必须 = header 类名
    }
    resp = await client.post("/api/v1/business-objects", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "XTEST"
    assert body["name"] == "测试对象"
    assert body["primaryDomainId"] == "PROCUREMENT"
    assert body["attachedDomainIds"] == ["LOGISTICS"]

    resp = await client.get("/api/v1/business-objects/XTEST")
    assert resp.status_code == 200
    assert resp.json()["code"] == "XTEST"

    # 更新附域集合（全量替换语义）
    resp = await client.put(
        "/api/v1/business-objects/XTEST",
        json={"attached_domain_ids": ["LOGISTICS", "QUALITY"]},
    )
    assert resp.status_code == 200
    assert sorted(resp.json()["attachedDomainIds"]) == ["LOGISTICS", "QUALITY"]


@pytest.mark.asyncio
async def test_create_attached_equals_primary_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    headerId = await _newHeaderClass(dbSession, "XTestClass")
    resp = await client.post(
        "/api/v1/business-objects",
        json={
            "code": "XTESTDP",
            "name": "附域重复",
            "header_class_id": headerId,
            "primary_domain_id": "PROCUREMENT",
            "graph_label": "XTestClass",
            "attached_domain_ids": ["PROCUREMENT"],
        },
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_unknown_domain_returns_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/business-objects",
        json={
            "code": "XTESTUD",
            "name": "未知域",
            "header_class_id": 1,
            "primary_domain_id": "NOT_A_DOMAIN",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_missing_primary_domain_returns_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/business-objects",
        json={"code": "XTESTNP", "name": "缺主域", "header_class_id": 1},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_code_returns_409(client: AsyncClient) -> None:
    # MATERIAL 由 autouse seed 预置 → 直接撞唯一约束
    resp = await client.post(
        "/api/v1/business-objects",
        json={
            "code": "MATERIAL",
            "name": "物料",
            "header_class_id": 1,
            "primary_domain_id": "PROCUREMENT",
        },
    )
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
        json={
            "code": "invalid",
            "name": "X",
            "header_class_id": 1,
            "primary_domain_id": "PROCUREMENT",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_graph_label_mismatch_returns_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """graph_label 与 header 类名不一致 → 422。"""
    headerId = await _newHeaderClass(dbSession, "XTestClass")
    resp = await client.post(
        "/api/v1/business-objects",
        json={
            "code": "XTESTGL",
            "name": "标签不一致",
            "header_class_id": headerId,
            "primary_domain_id": "PROCUREMENT",
            "graph_label": "OtherLabel",  # 类名是 XTestClass
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_returns_204(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    headerId = await _newHeaderClass(dbSession, "XTestClass")
    resp = await client.post(
        "/api/v1/business-objects",
        json={
            "code": "XTEST2",
            "name": "待删对象",
            "header_class_id": headerId,
            "primary_domain_id": "PROCUREMENT",
            "graph_label": "XTestClass",
        },
    )
    assert resp.status_code == 201
    resp = await client.delete("/api/v1/business-objects/XTEST2")
    assert resp.status_code == 204
    resp = await client.get("/api/v1/business-objects/XTEST2")
    assert resp.status_code == 404
