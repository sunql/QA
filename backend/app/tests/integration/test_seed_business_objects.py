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
    """upsert 语义：第二次执行不改变业务对象的最终状态（每行 ON CONFLICT DO UPDATE 触发）.

    注意：ON CONFLICT DO UPDATE 的 rowcount 包含所有冲突行（即使值未变），所以
    inserted2 == 6 是 PostgreSQL 的正常行为；断言改为「数据状态一致 + 行数仍为 6」。
    """
    from sqlalchemy import text as _text

    await seedBusinessObjects(dbSession)
    inserted2 = await seedBusinessObjects(dbSession)
    assert inserted2 == 6  # 6 行均被 DO UPDATE 触发

    # 业务不变量：行数 = 6 + 6 行的 graph_label 等于硬编码期望值
    # 用 raw SQL 绕开 SQLAlchemy ORM identity map 缓存（与 test_backfills 同因）
    raw = (await dbSession.execute(
        _text("SELECT code, graph_label, header_class_id FROM business_object ORDER BY code")
    )).all()
    by_code = {code: (label, hcid) for code, label, hcid in raw}

    for code, expected_label in [
        ("SUPPLIER", "Supplier"),
        ("MATERIAL", "ItemMaster"),
        ("PO", "PurchaseOrder"),
        ("GR", "Receipt"),
        ("IQC", "IncomingInspection"),
    ]:
        assert by_code[code][0] == expected_label
        # ontology_class 此时为空（conftest 不 re-seed），header_class_id 应为 NULL
        assert by_code[code][1] is None
    assert by_code["NCR"][0] is None
    assert by_code["NCR"][1] is None


@pytest.mark.asyncio
async def test_backfills_header_class_id_when_ontology_added_later(dbSession):
    """回归测试：模拟「先 seed business_object、ontology_class 后到」的真实场景.

    场景：
      1. ontology_class 为空（迁移未跑）→ 第一次 seed 把 5 行的 header_class_id 写成 NULL
      2. 迁移补上 5 个 ontology_class 行
      3. 第二次 seed 应回填 header_class_id（不是 DO NOTHING 静默跳过）

    旧实现的 bug：ON CONFLICT DO NOTHING 导致 step 3 不写回，header_class_id 永远为 NULL。
    """
    from app.domain.models import OntologyClass

    # step 1: ontology_class 空，seed 一次（header_class_id 全 NULL）
    inserted1 = await seedBusinessObjects(dbSession)
    assert inserted1 == 6  # 全部 DO UPDATE（conftest 已 pre-seed 6 行）

    rows = (await dbSession.execute(select(BusinessObject))).scalars().all()
    by_code_before = {r.code: r for r in rows}
    # 没有 ontology_class 时 header_class_id 应为 NULL
    assert by_code_before["SUPPLIER"].header_class_id is None
    assert by_code_before["MATERIAL"].header_class_id is None

    # step 2: 补 5 行 ontology_class（模拟 0039/0040 迁移就绪）
    for class_name, source in [
        ("Supplier", "DWD_SUPPLIER"),
        ("ItemMaster", "DWD_MATERIAL"),
        ("PurchaseOrder", "DWD_PURCHASE_ORDER"),
        ("Receipt", "DWD_GOODS_RECEIPT"),
        ("IncomingInspection", "DWD_INCOMING_INSPECTION"),
    ]:
        dbSession.add(
            OntologyClass(
                class_name=class_name,
                source_table=source,
                object_type="Master" if class_name in ("Supplier", "ItemMaster") else "Transaction",
                valid_from=__import__("datetime").datetime.now(__import__("datetime").UTC),
            )
        )
    await dbSession.commit()

    # step 3: 重跑 seed（应回填 5 个 NULL → 非 NULL）
    await seedBusinessObjects(dbSession)

    # 用 raw SQL 验证 DB 真实状态，绕开 SQLAlchemy 2.x ORM identity map 缓存
    # （seedBusinessObjects 内部 commit 后，session 的 BusinessObject 缓存与最新行
    # 状态不一致；用 text() 走直连拿权威值）
    from sqlalchemy import text as _text

    raw = (await dbSession.execute(
        _text("SELECT code, header_class_id FROM business_object ORDER BY code")
    )).all()
    by_code_after = {code: hcid for code, hcid in raw}

    classes_raw = (await dbSession.execute(
        _text("SELECT class_name, id FROM ontology_class")
    )).all()
    classes = {cn: cid for cn, cid in classes_raw}

    assert by_code_after["SUPPLIER"] == classes["Supplier"]
    assert by_code_after["MATERIAL"] == classes["ItemMaster"]
    assert by_code_after["PO"] == classes["PurchaseOrder"]
    assert by_code_after["GR"] == classes["Receipt"]
    assert by_code_after["IQC"] == classes["IncomingInspection"]
    # NCR 故意保持 NULL（无本体类）
    assert by_code_after["NCR"] is None


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
