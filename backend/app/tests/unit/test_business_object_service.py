"""BusinessObjectService 单元测试（mock session）."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.exceptions import (
    BusinessObjectGraphLabelMismatchError,
    ConflictError,
    NotFoundError,
)
from app.domain.schemas import (
    BusinessObjectCreate,
)
from app.services.business_object_service import BusinessObjectService
from app.services.messages_zh import (
    MSG_BUSINESS_OBJECT_CODE_EXISTS,
    MSG_BUSINESS_OBJECT_IN_USE,
    MSG_BUSINESS_OBJECT_NOT_FOUND,
)


@pytest.fixture
def svc() -> BusinessObjectService:
    return BusinessObjectService()


@pytest.fixture
def session() -> AsyncMock:
    s = AsyncMock()
    s.execute = AsyncMock()
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    s.add = MagicMock()
    return s


# --- getObject --------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_object_not_found_raises(svc: BusinessObjectService, session: AsyncMock) -> None:
    session.get.return_value = None  # session.get is used for pk lookup, not execute
    with pytest.raises(NotFoundError) as exc:
        await svc.getObject(session, "SUPPLIER")
    assert "SUPPLIER" in str(exc.value)


# --- createObject -----------------------------------------------------------

@pytest.mark.asyncio
async def test_create_object_duplicate_code_raises_conflict(
    svc: BusinessObjectService, session: AsyncMock
) -> None:
    session.get.return_value = MagicMock()  # exists
    with pytest.raises(ConflictError):
        await svc.createObject(
            session,
            BusinessObjectCreate(code="SUPPLIER", name="供应商"),
            actor="alice",
        )


@pytest.mark.asyncio
async def test_create_object_graph_label_mismatch_raises(
    svc: BusinessObjectService, session: AsyncMock
) -> None:
    # 1) duplicate check via session.get: not exists
    # 2) header_class lookup via session.execute
    session.get.return_value = None
    session.execute.side_effect = [
        MagicMock(scalar_one_or_none=MagicMock(return_value=MagicMock(class_name="Supplier"))),  # header class
    ]
    with pytest.raises(BusinessObjectGraphLabelMismatchError) as exc:
        await svc.createObject(
            session,
            BusinessObjectCreate(
                code="SUPPLIER", name="供应商",
                header_class_id=1, graph_label="Material",  # 不一致
            ),
            actor="alice",
        )


# --- updateObject -----------------------------------------------------------

@pytest.mark.asyncio
async def test_update_object_not_found_raises(
    svc: BusinessObjectService, session: AsyncMock
) -> None:
    session.get.return_value = None
    with pytest.raises(NotFoundError) as exc:
        await svc.updateObject(
            session, "UNKNOWN", MagicMock(name="X")  # dto fields don't matter when not found
        )
    assert "UNKNOWN" in str(exc.value)


# --- deleteObject -----------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_object_referenced_raises_conflict(
    svc: BusinessObjectService, session: AsyncMock
) -> None:
    # 1) getObject via session.get returns row
    # 2) reference check via session.execute returns non-empty count (x3 for 3 tables)
    row = MagicMock()
    row.code = "SUPPLIER"
    session.get.return_value = row
    session.execute.side_effect = [
        MagicMock(scalar_one=MagicMock(return_value=5)),  # entity_mapping
        MagicMock(scalar_one=MagicMock(return_value=0)),  # feature_definition
        MagicMock(scalar_one=MagicMock(return_value=0)),  # document_entity_relation
    ]
    with pytest.raises(ConflictError) as exc:
        await svc.deleteObject(session, "SUPPLIER")
    assert "SUPPLIER" in str(exc.value)
