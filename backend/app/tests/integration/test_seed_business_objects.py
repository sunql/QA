"""seed_business_objects 幂等写入 **5 行** + 可通过 API 查到。

## 契约（SSOT = ``backend/scripts/seed_business_objects.py`` 的模块 docstring）

- **SUPPLIER / MATERIAL / PO / GR 四行必 seed**；
- **IQC** 仅当 ``ontology_class`` 已有 ``IncomingInspection`` 时 seed；
- **NCR 自 0133 收紧 ``header_class_id`` NOT NULL 后不再 seed** —— 无本体类的
  BO 无法落库，其业务语义待本体建模后经 ``/business-objects`` 正式创建；
- **类缺失 ⇒ 该行整行跳过**（不是「建行并留 NULL」）：``_seed_rows`` 过滤掉
  ``header_class_id is None`` 的行。

## 为什么本文件 2026-11-18 重写

旧断言（6 行含 NCR / header_class_id 为 NULL / 「conftest 不 re-seed」）
三处都与现行契约不符：

1. **行数是 5 不是 6** —— NCR 自 0133 起不再 seed（脚本 docstring 明写）；
2. **header_class_id 不再是 NULL** —— 集成测试的 autouse ``warmAgentCaches``
   每个用例前会补种 5 个最小 header 类（``app/tests/_pg_support.seedHeaderOntologyClasses``），
   所以 BO 行总能解析到 header；
3. **「先建行留 NULL、后回填」的场景已不存在** —— 0133 之后类缺失是**整行跳过**，
   不是建行留 NULL ⇒ 回归场景改为「类缺失 ⇒ 无行；类到达 ⇒ 行被建出来且绑定正确」。

重写是**把断言对齐当前设计**，不是放宽断言：行数、绑定、幂等性、图标签
全部仍逐项硬断言。
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select, text as _text

from app.domain.models import BusinessObject
from scripts.seed_business_objects import HEADER_CLASS_NAMES, seedBusinessObjects

# BO code → 本体类 class_name（graph_label 也等于 class_name）
_EXPECTED_HEADERS = {
    "SUPPLIER": "Supplier",
    "MATERIAL": "ItemMaster",
    "PO": "PurchaseOrder",
    "GR": "Receipt",
    "IQC": "IncomingInspection",
}

# 0133 起不再 seed 的 code（无本体类 ⇒ BO 无法落库）
_RETIRED_CODES = ("NCR",)


async def _insertHeaderClasses(dbSession) -> None:
    """补回 5 个 header 本体类。

    ``source_table`` **必须与 ``seed_ontology.CLASSES`` 同款**：后续调
    ``seed_ontology.seed()`` 的用例经 ``_seedClasses`` 按 source_table 查重复用，
    缺了会撞 ``uq_ontology_class_name_version``（class_name, version 唯一约束）。
    CLASSES 没有的类（IncomingInspection）source_table 置 None，无碰撞面。
    """
    import seed_ontology

    sourceTableByName = {c["class_name"]: c["source_table"] for c in seed_ontology.CLASSES}
    for name in HEADER_CLASS_NAMES:
        await dbSession.execute(
            _text(
                """
                INSERT INTO ontology_class
                    (class_name, source_table, object_type, version,
                     valid_from, created_by, created_time, updated_time)
                VALUES (:cn, :st, :ot, 1, now(), 'seed-test-header', now(), now())
                """
            ),
            {
                "cn": name,
                "st": sourceTableByName.get(name),
                "ot": "Master" if name in ("Supplier", "ItemMaster") else "Transaction",
            },
        )
    await dbSession.commit()


@pytest.mark.asyncio
async def test_first_run_inserts_five_mappings(dbSession):
    """5 行必 seed（SUPPLIER/MATERIAL/PO/GR/IQC）；NCR 不 seed。"""
    inserted = await seedBusinessObjects(dbSession)
    assert inserted == 5

    rows = (await dbSession.execute(select(func.count()).select_from(BusinessObject))).scalar()
    assert rows == 5

    codes = sorted((await dbSession.execute(select(BusinessObject.code))).scalars().all())
    assert codes == ["GR", "IQC", "MATERIAL", "PO", "SUPPLIER"]
    for retired in _RETIRED_CODES:
        assert retired not in codes, f"{retired} 自 0133 起不再 seed（无本体类 ⇒ BO 无法落库）"


@pytest.mark.asyncio
async def test_second_run_is_idempotent(dbSession):
    """upsert 语义：第二次执行不改变业务对象的最终状态（每行 ON CONFLICT DO UPDATE 触发）。

    注意：ON CONFLICT DO UPDATE 的 rowcount 包含所有冲突行（即使值未变），所以
    inserted2 == 5 是 PostgreSQL 的正常行为；断言改为「数据状态一致 + 行数仍为 5」。
    """
    await seedBusinessObjects(dbSession)
    inserted2 = await seedBusinessObjects(dbSession)
    assert inserted2 == 5  # 5 行均被 DO UPDATE 触发

    # 用 raw SQL 绕开 SQLAlchemy ORM identity map 缓存（seed 内部 commit 后，
    # session 的 BusinessObject 缓存与最新行状态可能不一致）
    raw = (
        await dbSession.execute(
            _text("SELECT code, graph_label, header_class_id FROM business_object ORDER BY code")
        )
    ).all()
    by_code = {code: (label, hcid) for code, label, hcid in raw}

    assert len(by_code) == 5
    for code, className in _EXPECTED_HEADERS.items():
        assert by_code[code][0] == className
        # 集成测试的 autouse warmAgentCaches 已补种 5 个 header 类 ⇒ 必非 NULL
        assert by_code[code][1] is not None, f"{code}.header_class_id 不应为 NULL"


@pytest.mark.asyncio
async def test_missing_header_class_skips_row_and_creates_it_when_class_arrives(dbSession):
    """回归测试：类缺失时**整行跳过**，类到达后再 seed ⇒ 行被建出来且绑定正确。

    这是 0133 之后的现行语义。旧实现的 bug 是「ON CONFLICT DO NOTHING 导致
    header_class_id 永远不回填」——在 NOT NULL 收紧后该 bug 表现为「类缺失时
    建出一堆 header_class_id 为 NULL 的坏行」；现在的契约是**不建行**，
    类到位后 seed 才建，且绑定到正确的类 id。

    步骤：
      1. 清掉 BO 行与 5 个 header 类
      2. seed ⇒ 0 行（类缺失 ⇒ 整行跳过）
      3. 补回 5 个本体类
      4. seed ⇒ 5 行，header_class_id 逐个对上类 id
      5. 再 seed ⇒ 行数与绑定不变（幂等）
    """
    # 1) 清 BO 行 + 5 个 header 类（BO 先删：header_class_id 是 FK ON DELETE RESTRICT）
    await dbSession.execute(_text("DELETE FROM business_object"))
    placeholders = ", ".join(f":c{i}" for i in range(len(HEADER_CLASS_NAMES)))
    params = {f"c{i}": name for i, name in enumerate(HEADER_CLASS_NAMES)}
    await dbSession.execute(
        _text(f"DELETE FROM ontology_class WHERE class_name IN ({placeholders})"), params
    )
    await dbSession.commit()

    # 2) 类缺失 ⇒ 一行都不建
    inserted1 = await seedBusinessObjects(dbSession)
    assert inserted1 == 0
    rows1 = (await dbSession.execute(_text("SELECT COUNT(*) FROM business_object"))).scalar()
    assert rows1 == 0, "类缺失时不应建出 header_class_id 为 NULL 的坏行"

    # 3) 类到达
    await _insertHeaderClasses(dbSession)

    # 4) 再 seed ⇒ 5 行，绑定到正确的类 id
    inserted2 = await seedBusinessObjects(dbSession)
    assert inserted2 == 5

    raw = (
        await dbSession.execute(
            _text("SELECT code, header_class_id FROM business_object ORDER BY code")
        )
    ).all()
    by_code = {code: hcid for code, hcid in raw}
    class_ids = {
        cn: cid
        for cn, cid in (
            await dbSession.execute(_text("SELECT class_name, id FROM ontology_class"))
        ).all()
    }
    assert len(by_code) == 5
    for code, className in _EXPECTED_HEADERS.items():
        assert by_code[code] == class_ids[className], f"{code} 绑定错类"

    # 5) 幂等：再 seed 不改行数也不改绑定
    inserted3 = await seedBusinessObjects(dbSession)
    assert inserted3 == 5
    raw2 = (
        await dbSession.execute(
            _text("SELECT code, header_class_id FROM business_object ORDER BY code")
        )
    ).all()
    assert {code: hcid for code, hcid in raw2} == by_code


@pytest.mark.asyncio
async def test_codes_are_uppercase(dbSession):
    await seedBusinessObjects(dbSession)
    codes = sorted(
        (await dbSession.execute(select(BusinessObject.code))).scalars().all()
    )
    assert codes == ["GR", "IQC", "MATERIAL", "PO", "SUPPLIER"]


@pytest.mark.asyncio
async def test_graph_labels_resolve_to_class_names(dbSession):
    """5 个业务对象 graph_label = header_class.class_name；NCR 根本不存在。"""
    await seedBusinessObjects(dbSession)
    rows = (await dbSession.execute(select(BusinessObject))).scalars().all()
    by_code = {r.code: r for r in rows}
    for code, className in _EXPECTED_HEADERS.items():
        assert by_code[code].graph_label == className
    for retired in _RETIRED_CODES:
        assert retired not in by_code, f"{retired} 不应存在（0133 起不 seed）"
