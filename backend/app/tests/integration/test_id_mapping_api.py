"""A1 · id_mapping API integration tests（TDD RED）。

覆盖：
1. 创建 id_mapping → 201 + 返回 unified_id
2. 重复 (business_object, external_id) → 409 Conflict
3. 按 business_object 查询 → 返回匹配记录
4. 按 unified_id 查询 → 返回记录
5. 删除 → 204
6. 不存在的 ID → 404

依赖：真实 PostgreSQL（qa_metadata_test），完整 API 链路。
"""
from __future__ import annotations

import pytest

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def api_client(pg_client: AsyncClient) -> AsyncClient:
    """返回已认证的异步 HTTP 客户端。"""
    return pg_client


async def _truncate_id_mapping(session: AsyncSession) -> None:
    """每个测试前清空 id_mapping 表。"""
    await session.execute(text("TRUNCATE TABLE id_mapping RESTART IDENTITY CASCADE"))
    await session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_id_mapping_returns_201(api_client: AsyncClient, db_session: AsyncSession):
    """POST /api/v1/id-mappings → 201，返回 unified_id 格式 obj:{bo}:{ext_id}。"""
    await _truncate_id_mapping(db_session)

    payload = {
        "businessObject": "supplier",
        "externalId": "S001",
        "pgTable": "supplier",
        "pgId": "123",
    }
    response = await api_client.post("/api/v1/id-mappings", json=payload)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["unifiedId"].startswith("obj:supplier:S001")
    assert data["businessObject"] == "supplier"
    assert data["externalId"] == "S001"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_duplicate_raises_409(api_client: AsyncClient, db_session: AsyncSession):
    """相同 (business_object, external_id) → 409 Conflict。"""
    await _truncate_id_mapping(db_session)

    payload = {
        "businessObject": "supplier",
        "externalId": "S002",
        "pgTable": "supplier",
        "pgId": "456",
    }
    r1 = await api_client.post("/api/v1/id-mappings", json=payload)
    assert r1.status_code == 201

    r2 = await api_client.post("/api/v1/id-mappings", json=payload)
    assert r2.status_code == 409, f"expected 409, got {r2.status_code}"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_by_business_object(api_client: AsyncClient, db_session: AsyncSession):
    """GET /api/v1/id-mappings?businessObject=supplier → 返回匹配记录。"""
    await _truncate_id_mapping(db_session)

    # 插入两条 supplier
    for ext_id in ("S003", "S004"):
        await db_session.execute(
            text("""
                INSERT INTO id_mapping (unified_id, business_object, external_id, pg_table, pg_id)
                VALUES (:uid, 'supplier', :ext, 'supplier', :pg_id)
            """),
            {"uid": f"obj:supplier:{ext_id}", "ext": ext_id, "pg_id": ext_id},
        )
    # 插入一条 customer（不应返回）
    await db_session.execute(
        text("""
            INSERT INTO id_mapping (unified_id, business_object, external_id, pg_table, pg_id)
            VALUES (:uid, 'customer', 'C001', 'customer', '789')
        """),
        {"uid": "obj:customer:C001"},
    )
    await db_session.commit()

    response = await api_client.get("/api/v1/id-mappings?businessObject=supplier")
    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 2
    assert all(item["businessObject"] == "supplier" for item in items)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_by_unified_id(api_client: AsyncClient, db_session: AsyncSession):
    """GET /api/v1/id-mappings/{unified_id} → 返回记录。"""
    await _truncate_id_mapping(db_session)
    unified_id = "obj:supplier:S005"
    await db_session.execute(
        text("""
            INSERT INTO id_mapping (unified_id, business_object, external_id, pg_table, pg_id)
            VALUES (:uid, 'supplier', 'S005', 'supplier', '999')
        """),
        {"uid": unified_id},
    )
    await db_session.commit()

    response = await api_client.get(f"/api/v1/id-mappings/{unified_id}")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["unifiedId"] == unified_id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_not_found_returns_404(api_client: AsyncClient, db_session: AsyncSession):
    """GET /api/v1/id-mappings/obj:does:not-exist → 404。"""
    await _truncate_id_mapping(db_session)
    response = await api_client.get("/api/v1/id-mappings/obj:does:not-exist")
    assert response.status_code == 404, f"expected 404, got {response.status_code}"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_delete_id_mapping_returns_204(api_client: AsyncClient, db_session: AsyncSession):
    """DELETE /api/v1/id-mappings/{unified_id} → 204，再次 GET → 404。"""
    await _truncate_id_mapping(db_session)
    unified_id = "obj:supplier:S006"
    await db_session.execute(
        text("""
            INSERT INTO id_mapping (unified_id, business_object, external_id, pg_table, pg_id)
            VALUES (:uid, 'supplier', 'S006', 'supplier', '666')
        """),
        {"uid": unified_id},
    )
    await db_session.commit()

    response = await api_client.delete(f"/api/v1/id-mappings/{unified_id}")
    assert response.status_code == 204, f"expected 204, got {response.status_code}"

    response2 = await api_client.get(f"/api/v1/id-mappings/{unified_id}")
    assert response2.status_code == 404
