"""A1 · id_mapping API integration tests（TDD RED）。

覆盖：
1. 创建 id_mapping → 201 + 返回 unified_id
2. 重复 (business_object, external_id) → 409 Conflict
3. 按 business_object 查询 → 返回匹配记录
4. 按 unified_id 查询 → 返回记录
5. 删除 → 204
6. 不存在的 ID → 404
7. POST 自带任一跨库指针（五列逐一）→ 422，且不落库
8. POST 非法 externalId（含 ':' / 控制字符 / 零宽字符）→ 422，且不落库
9. POST 两段首尾空白 → 201 且落库为归一后的值；同一归一值再次 POST → 409

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

    payload = {"businessObject": "supplier", "externalId": "S001"}
    response = await api_client.post("/api/v1/id-mappings", json=payload)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["unifiedId"] == "obj:supplier:S001"
    assert data["businessObject"] == "supplier"
    assert data["externalId"] == "S001"

    # 跨库落点不是调用方可声明的输入：未传 → 五列全 NULL（由 A2/A3 回填）。
    # 直接查库列而不是响应键：不依赖 to_camel 的产物（neo4j → neo4JNodeId 这种怪键名）。
    row = (
        await db_session.execute(
            text("""
                SELECT pg_table, pg_id, neo4j_node_id, milvus_collection, milvus_id
                FROM id_mapping WHERE unified_id = :uid
            """),
            {"uid": "obj:supplier:S001"},
        )
    ).mappings().one()
    assert dict(row) == {
        "pg_table": None,
        "pg_id": None,
        "neo4j_node_id": None,
        "milvus_collection": None,
        "milvus_id": None,
    }


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_rejects_client_supplied_pointers(
    api_client: AsyncClient, db_session: AsyncSession
):
    """POST 自带跨库指针 → 422（显式拒绝，而非静默丢弃）。"""
    await _truncate_id_mapping(db_session)

    response = await api_client.post(
        "/api/v1/id-mappings",
        json={
            "businessObject": "supplier",
            "externalId": "S009",
            "pgTable": "supplier",
            "pgId": "999",
        },
    )
    assert response.status_code == 422, f"expected 422, got {response.status_code}: {response.text}"

    count = (
        await db_session.execute(text("SELECT count(*) FROM id_mapping"))
    ).scalar_one()
    assert count == 0, "被拒绝的请求不得落库"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "pointerKey, pointerValue",
    [
        ("pgTable", "supplier"),
        ("pgId", "999"),
        ("neo4jNodeId", "node-1"),
        ("milvusCollection", "supplier_vec"),
        ("milvusId", "vec-1"),
    ],
)
async def test_create_rejects_each_client_supplied_pointer(
    api_client: AsyncClient, db_session: AsyncSession, pointerKey: str, pointerValue: str
):
    """五列逐一验证：任何一列都不能由调用方声明（extra="forbid" 覆盖全字段）。"""
    await _truncate_id_mapping(db_session)

    response = await api_client.post(
        "/api/v1/id-mappings",
        json={"businessObject": "supplier", "externalId": "S009", pointerKey: pointerValue},
    )
    assert response.status_code == 422, f"{pointerKey}: {response.text}"

    count = (
        await db_session.execute(text("SELECT count(*) FROM id_mapping"))
    ).scalar_one()
    assert count == 0, f"{pointerKey} 被拒绝的请求不得落库"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "badExternalId",
    [
        pytest.param("S:1", id="separator"),
        pytest.param("S1\x00", id="nul"),
        pytest.param("S1" + chr(0x200B), id="zwsp"),
        pytest.param("S1" + chr(0x00AD), id="soft-hyphen"),
    ],
)
async def test_create_rejects_illegal_external_id(
    api_client: AsyncClient, db_session: AsyncSession, badExternalId: str
):
    """非法字符在真实写路径上被挡住（而不是只在单元测试的函数里）。"""
    await _truncate_id_mapping(db_session)

    response = await api_client.post(
        "/api/v1/id-mappings",
        json={"businessObject": "supplier", "externalId": badExternalId},
    )
    assert response.status_code == 422, response.text

    count = (
        await db_session.execute(text("SELECT count(*) FROM id_mapping"))
    ).scalar_one()
    assert count == 0, "被拒绝的请求不得落库"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_normalizes_whitespace_segments(
    api_client: AsyncClient, db_session: AsyncSession
):
    """两段首尾空白归一后才落库；同一归一值再次 POST → 409（唯一约束生效）。"""
    await _truncate_id_mapping(db_session)

    payload = {"businessObject": " supplier ", "externalId": " S010 "}
    r1 = await api_client.post("/api/v1/id-mappings", json=payload)
    assert r1.status_code == 201, r1.text
    assert r1.json()["unifiedId"] == "obj:supplier:S010"

    row = (
        await db_session.execute(
            text(
                "SELECT business_object, external_id FROM id_mapping "
                "WHERE unified_id = :uid"
            ),
            {"uid": "obj:supplier:S010"},
        )
    ).mappings().one()
    assert dict(row) == {"business_object": "supplier", "external_id": "S010"}

    r2 = await api_client.post("/api/v1/id-mappings", json=payload)
    assert r2.status_code == 409, r2.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_duplicate_raises_409(api_client: AsyncClient, db_session: AsyncSession):
    """相同 (business_object, external_id) → 409 Conflict。"""
    await _truncate_id_mapping(db_session)

    payload = {"businessObject": "supplier", "externalId": "S002"}
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
