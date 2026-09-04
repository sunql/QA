"""seed_business_objects 幂等写入 6 行 + 可通过 API 查到。"""
import pytest
from sqlalchemy import func, select

from app.domain.models import BusinessObject
from scripts.seed_business_objects import seedBusinessObjects


@pytest.mark.asyncio
async def test_first_run_inserts_six_mappings(dbSession):
    inserted = await seedBusinessObjects(dbSession)
    assert inserted == 6

    rows = (await dbSession.execute(select(func.count()).select_from(BusinessObject))).scalar()
    assert rows == 6


@pytest.mark.asyncio
async def test_second_run_is_idempotent(dbSession):
    await seedBusinessObjects(dbSession)
    inserted2 = await seedBusinessObjects(dbSession)
    assert inserted2 == 0

    rows = (await dbSession.execute(select(func.count()).select_from(BusinessObject))).scalar()
    assert rows == 6


@pytest.mark.asyncio
async def test_codes_are_uppercase(dbSession):
    await seedBusinessObjects(dbSession)
    codes = sorted(
        (await dbSession.execute(select(BusinessObject.code))).scalars().all()
    )
    assert codes == ["GR", "IQC", "MATERIAL", "NCR", "PO", "SUPPLIER"]


@pytest.mark.asyncio
async def test_graph_labels_resolve_to_class_names(dbSession):
    """5 个业务对象 graph_label = header_class.class_name；NCR 为 NULL."""
    await seedBusinessObjects(dbSession)
    rows = (await dbSession.execute(select(BusinessObject))).scalars().all()
    by_code = {r.code: r for r in rows}
    assert by_code["SUPPLIER"].graph_label == "Supplier"
    assert by_code["MATERIAL"].graph_label == "ItemMaster"
    assert by_code["PO"].graph_label == "PurchaseOrder"
    assert by_code["GR"].graph_label == "Receipt"
    assert by_code["IQC"].graph_label == "IncomingInspection"
    assert by_code["NCR"].graph_label is None
    assert by_code["NCR"].header_class_id is None
