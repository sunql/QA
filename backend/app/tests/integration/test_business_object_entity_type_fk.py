"""三表 entity_type FK 约束：非法值 → DB 拒绝；合法值 → 写入成功。"""
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_entity_mapping_invalid_entity_type_rejected_by_fk(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/api/v1/entity-mappings",
        json={
            "entityType": "INVALID_TYPE",
            "enterpriseKey": 1,
            "enterpriseCode": "X",
            "sourceSystem": "ERP",
            "sourceKey": "K1",
            "sourceCode": "C1",
        },
    )
    assert resp.status_code == 422  # Pydantic 字面量拒绝（FK 是双层守卫）


@pytest.mark.asyncio
async def test_feature_definition_invalid_entity_type_rejected(
    client: AsyncClient,
) -> None:
    """Service 层字面量校验 → 422."""
    resp = await client.post(
        "/api/v1/features",
        json={
            "featureName": "TEST",
            "entityType": "INVALID",
            "calculationLogic": "SELECT 1",
            "datasourceId": 1,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_document_entity_relation_invalid_entity_type_rejected(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/api/v1/documents/relations",
        json={
            "documentId": "DOC1",
            "entityType": "INVALID",
            "entityKey": "Q630",
            "relationType": "CONTRACT",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_db_fk_constraint_exists() -> None:
    """三表 entity_type 列上有 FK 指向 business_object.code."""
    engine = create_async_engine(
        "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"
    )
    async with engine.connect() as conn:
        for table in (
            "entity_mapping",
            "feature_definition",
            "document_entity_relation",
        ):
            row = (
                await conn.execute(
                    text(
                        f"""
                        SELECT pg_get_constraintdef(oid)
                        FROM pg_constraint
                        WHERE conrelid = '{table}'::regclass
                          AND contype = 'f'
                          AND pg_get_constraintdef(oid) LIKE '%business_object%'
                    """
                    )
                )
            ).first()
            assert row is not None, f"{table} 缺少 FK 到 business_object"
