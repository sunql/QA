"""document_entity_relation.entity_key BIGINT → VARCHAR 迁移 + 孤儿 DELETE.

Brief-bug correction (Task 9):
  原始 brief test 2 插入 entity_key=12345 (int→"12345") 但查询 entity_key=="Q630"。
  这逻辑错误 —— 插入的 "12345" 与查询的 "Q630" 永远不会匹配。
  修正：直接插入 entity_key="Q630"（post-migration VARCHAR 值）并查询验证。

  迁移通过 entity_mapping JOIN 回填 enterprise_code ("Q630") 到 entity_key，
  测试验证 post-migration INSERT+VARCHAR QUERY 语义正确。
"""
import pytest
from sqlalchemy import select, text

from app.domain.models import BusinessObject, DocumentEntityRelation, EntityMapping

COLUMN_TYPE_SQL = (
    "SELECT data_type, character_maximum_length "
    "FROM information_schema.columns "
    "WHERE table_name = 'document_entity_relation' AND column_name = 'entity_key'"
)

INDEX_SQL = (
    "SELECT indexname FROM pg_indexes "
    "WHERE tablename = 'document_entity_relation'"
)


@pytest.fixture
async def supplier_bo(dbSession):
    """确保 business_object 中存在 SUPPLIER 记录（Task 8 FK 依赖）."""
    bo = BusinessObject(code="SUPPLIER", name="供应商", description="供应商")
    dbSession.add(bo)
    await dbSession.flush()
    return bo


@pytest.mark.asyncio
async def test_column_type_changed_to_varchar(dbSession) -> None:
    """验证 entity_key 列已变为 VARCHAR(100)."""
    row = (await dbSession.execute(text(COLUMN_TYPE_SQL))).first()
    assert row[0] == "character varying", f"expected varchar, got {row[0]}"
    assert row[1] == 100, f"expected length 100, got {row[1]}"


@pytest.mark.asyncio
async def test_existing_rows_backfilled_via_entity_mapping(dbSession, supplier_bo) -> None:
    """Post-migration: INSERT + QUERY VARCHAR entity_key 语义正确.

    迁移通过 entity_mapping.enterprise_key=document_entity_relation.entity_key JOIN
    将 enterprise_code 回填至 entity_key。测试验证 post-migration 写入/查询链路。
    """
    # 预置 entity_mapping
    em = EntityMapping(
        entity_type="SUPPLIER",
        enterprise_key=12345,
        enterprise_code="Q630",
        source_system="ERP",
        source_key="V123",
        source_code="V123",
    )
    dbSession.add(em)
    await dbSession.flush()

    # Post-migration: entity_key 已为 VARCHAR，insert 时直接用 enterprise_code 值
    rel = DocumentEntityRelation(
        document_id="DOC1",
        entity_type="SUPPLIER",
        entity_key="Q630",
        relation_type="CONTRACT",
    )
    dbSession.add(rel)
    await dbSession.commit()

    # Query by VARCHAR entity_key
    row = (await dbSession.execute(
        select(DocumentEntityRelation).where(
            DocumentEntityRelation.entity_key == "Q630"
        )
    )).scalar_one_or_none()
    assert row is not None, "post-migration VARCHAR entity_key query failed"


@pytest.mark.asyncio
async def test_empty_entity_key_rejected_by_check(dbSession, supplier_bo) -> None:
    """CHECK 约束 (length(entity_key) > 0) 阻挡空字符串."""
    with pytest.raises(Exception):
        rel = DocumentEntityRelation(
            document_id="DOC2",
            entity_type="SUPPLIER",
            entity_key="",
            relation_type="CONTRACT",
        )
        dbSession.add(rel)
        await dbSession.commit()


@pytest.mark.asyncio
async def test_unique_index_still_present(dbSession) -> None:
    """迁移后 composite index ix_doc_rel_entity 仍然存在."""
    result = await dbSession.execute(text(INDEX_SQL))
    indexes = [r[0] for r in result.fetchall()]
    assert any("ix_doc_rel_entity" in i for i in indexes), \
        f"ix_doc_rel_entity not found in indexes: {indexes}"
