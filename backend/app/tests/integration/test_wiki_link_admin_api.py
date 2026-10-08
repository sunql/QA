"""wiki-ontology-link Task 3: Admin Wiki ↔ Ontology 链接管理 API 集成测试.

运行：cd backend && pytest app/tests/integration/test_wiki_link_admin_api.py -v
"""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

AUTH_HEADERS = {"X-User-Id": "admin", "X-User-Roles": "admin"}


@pytest.fixture(autouse=True)
async def seed_wiki_page(client: AsyncClient, dbSession: AsyncSession) -> None:
    """Seed RBAC baseline and wiki_page for wiki_ontology_link FK + admin role.

    Depends on client (pgApiClient sets up factory) and dbSession (per-test session).
    """
    from app.domain.wiki_models import WikiPage
    from scripts.seed_rbac import seedRbacBaseline
    from sqlalchemy import select
    # Seed RBAC baseline (admin role + admin user + binding)
    await seedRbacBaseline(dbSession)
    # Seed wiki_page row for FK constraint
    page_row = (await dbSession.execute(select(WikiPage).where(WikiPage.page_id == "p001"))).scalar_one_or_none()
    if page_row is None:
        dbSession.add(WikiPage(page_id="p001", title="Test Page", content="Test content", status="published"))
    await dbSession.commit()


async def test_list_wiki_links_returns_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/admin/wiki-links", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_wiki_link_returns_201(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/admin/wiki-links",
        headers=AUTH_HEADERS,
        json={
            "page_id": "p001",
            "chunk_id": None,
            "ontology_type": "class",
            "ontology_id": 12,
            "weight": 1.0,
            "note": None,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["page_id"] == "p001"
    assert body["ontology_type"] == "class"


async def test_create_duplicate_returns_409(client: AsyncClient) -> None:
    payload = {
        "page_id": "p001",
        "chunk_id": None,
        "ontology_type": "class",
        "ontology_id": 12,
        "weight": 1.0,
    }
    await client.post("/api/v1/admin/wiki-links", headers=AUTH_HEADERS, json=payload)
    resp = await client.post("/api/v1/admin/wiki-links", headers=AUTH_HEADERS, json=payload)
    assert resp.status_code == 409


async def test_revoke_wiki_link_returns_200(client: AsyncClient) -> None:
    create = await client.post(
        "/api/v1/admin/wiki-links",
        headers=AUTH_HEADERS,
        json={"page_id": "p001", "chunk_id": None, "ontology_type": "class", "ontology_id": 12, "weight": 1.0},
    )
    link_id = create.json()["id"]
    resp = await client.delete(f"/api/v1/admin/wiki-links/{link_id}", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["revoked_time"] is not None


async def test_update_wiki_link_weight(client: AsyncClient) -> None:
    create = await client.post(
        "/api/v1/admin/wiki-links",
        headers=AUTH_HEADERS,
        json={"page_id": "p001", "chunk_id": None, "ontology_type": "class", "ontology_id": 12, "weight": 1.0},
    )
    link_id = create.json()["id"]
    resp = await client.patch(
        f"/api/v1/admin/wiki-links/{link_id}",
        headers=AUTH_HEADERS,
        json={"weight": 0.5},
    )
    assert resp.status_code == 200
    # weight must serialize as a JSON number (not string) for frontend contract
    assert isinstance(resp.json()["weight"], (int, float))
    assert resp.json()["weight"] == 0.5


async def test_list_linkable_targets_filters_by_type(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/admin/wiki-links/linkables?type=class&q=SUPPLIER",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all(item["type"] == "class" for item in body)


async def test_create_wiki_link_invalid_type_returns_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/admin/wiki-links",
        headers=AUTH_HEADERS,
        json={"page_id": "p001", "chunk_id": None, "ontology_type": "unknown", "ontology_id": 12, "weight": 1.0},
    )
    assert resp.status_code == 422


async def test_create_wiki_link_unauthorized_returns_403(client: AsyncClient) -> None:
    """Non-admin user gets 403 from getAdminOnlyActor."""
    resp = await client.post(
        "/api/v1/admin/wiki-links",
        headers={"X-User-Id": "not-admin", "X-User-Roles": "user"},
        json={"page_id": "p001", "chunk_id": None, "ontology_type": "class", "ontology_id": 12, "weight": 1.0},
    )
    assert resp.status_code == 403


async def test_list_links_includes_ontology_name_and_alias(
    client: AsyncClient, dbSession: AsyncSession,
) -> None:
    """列表必须带本体对象名 —— 否则界面只能显示裸 ID，与本体管理对不上。"""
    from datetime import datetime

    from app.domain.models import OntologyClass

    dbSession.add(OntologyClass(
        id=701, class_name="DWD_ARRIVAL_ORDER_DTL", class_alias="到货单",
        version=1, valid_from=datetime.now(),
    ))
    await dbSession.commit()

    created = await client.post(
        "/api/v1/admin/wiki-links", headers=AUTH_HEADERS,
        json={"page_id": "p001", "chunk_id": None, "ontology_type": "class",
              "ontology_id": 701, "weight": 1.0},
    )
    assert created.status_code == 201, f"Got {created.status_code}: {created.text}"

    resp = await client.get(
        "/api/v1/admin/wiki-links?page_id=p001", headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["ontology_name"] == "DWD_ARRIVAL_ORDER_DTL"
    assert rows[0]["ontology_alias"] == "到货单"


async def test_list_links_missing_ontology_yields_none(client: AsyncClient) -> None:
    """对象不存在（已删）→ 两个字段为 None，接口不 500。"""
    created = await client.post(
        "/api/v1/admin/wiki-links", headers=AUTH_HEADERS,
        json={"page_id": "p001", "chunk_id": None, "ontology_type": "class",
              "ontology_id": 999999, "weight": 1.0},
    )
    assert created.status_code == 201, f"Got {created.status_code}: {created.text}"

    resp = await client.get(
        "/api/v1/admin/wiki-links?page_id=p001", headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    rows = resp.json()
    assert rows[0]["ontology_name"] is None
    assert rows[0]["ontology_alias"] is None


async def test_create_metric_link_accepted(
    client: AsyncClient, dbSession: AsyncSession,
) -> None:
    """API 必须接受 ontology_type=metric（修复前被 422 pattern 拒绝）。"""
    from app.domain.models import OntologyMetric

    dbSession.add(OntologyMetric(
        id=802, metric_name="KPI_PURCHASE_CYCLE_TIME", metric_alias="采购周期",
        formula="AVG(x)", agg_function="AVG",
    ))
    await dbSession.commit()

    resp = await client.post(
        "/api/v1/admin/wiki-links", headers=AUTH_HEADERS,
        json={"page_id": "p001", "chunk_id": None, "ontology_type": "metric",
              "ontology_id": 802, "weight": 1.0},
    )
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"
    assert resp.json()["ontology_type"] == "metric"

    # 列表必须能解析出指标名（Task 4 的解析表已含 metric）
    listed = await client.get(
        "/api/v1/admin/wiki-links?page_id=p001", headers=AUTH_HEADERS,
    )
    assert listed.json()[0]["ontology_name"] == "KPI_PURCHASE_CYCLE_TIME"


async def test_create_link_rejects_unknown_type(client: AsyncClient) -> None:
    """放开的只有三个值；非法类型仍 422（双向断言）。"""
    resp = await client.post(
        "/api/v1/admin/wiki-links", headers=AUTH_HEADERS,
        json={"page_id": "p001", "chunk_id": None, "ontology_type": "join",
              "ontology_id": 1, "weight": 1.0},
    )
    assert resp.status_code == 422
