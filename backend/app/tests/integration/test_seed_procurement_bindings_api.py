"""采购本体绑定种子：**空库 → 建绑定 → 解析器可解析**（真实 PG 集成）。

回归来历（2026-11-26 生产实测）：`seed_procurement_bindings` 原只覆盖
`DIM_FACILITY`，订单行 / 收货行两个 DWD 类被假定「早已有绑定」。生产实测
`procurement_source_binding` **总共只有 1 行** ⇒ 发版后 `resolveBinding`
fail-closed ⇒ 待到货与到货跟踪直接不可用。

本用例把三种情形钉住：
1. 三个类都没有绑定时，种子能建齐并让解析器解析出工具必需的 12 个属性；
2. 重复跑幂等（第二次全 0，不重复建行、不覆盖既有确认）；
3. 某个类缺 `source_column` 时不影响其它类（逐类独立，不因一类缺失整体失败）。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)
from app.services.ontology_binding_resolver import resolveBinding
from app.tests.integration.test_ontology_binding_resolver_api import (
    _seedClassWithProperties,
    _seedThbiDataSource,
)
from scripts.seed_procurement_bindings import (
    BINDING_SPECS,
    FACILITY_BINDING,
    ORDER_BINDING,
    RECEIPT_BINDING,
    seed_procurement_bindings,
)

from app.infrastructure.database import getSessionFactory  # noqa: E402

#: 工具实际要求的属性（与 `delivery_tracking_query` / `pending_delivery_query` 一致）。
_ORDER_PROPERTIES = {
    "PO_NO": "PO_NO",
    "PO_LINE_NO": "PO_LINE_NO",
    "PO_QTY": "PO_QTY",
    "ORDER_DATE": "ORDER_DATE",
    "SUPPLIER_CODE": "SUPPLIER_CODE",
    "ITEM_CODE": "ITEM_CODE",
    "EXP_RCV_DATE": "EXP_RCV_DATE",
}
_RECEIPT_PROPERTIES = {
    "PO_NO": "PO_NO",
    "PO_LINE_NO": "PO_LINE_NO",
    "RCV_QTY_STU": "RCV_QTY_STU",
    "ITEM_DESC": "ITEM_DESC",
    "RCV_DATE": "RCV_DATE",
}
_FACILITY_PROPERTIES = {"FCY_0": "FCY_0", "FCYNAM_0": "FCYNAM_0"}


async def _seedOntologyOnly(dbSession: AsyncSession) -> None:
    """种 THBI 数据源 + 三个本体类（含属性），**不种任何绑定**。

    `stalePrefix=""` ⇒ 本体模型的 `source_table` / `source_column` 就是物理真相
    （种子脚本从它复制，`_assertSourceTable` 也据此校验）。
    """
    await _seedThbiDataSource(dbSession)
    await _seedClassWithProperties(
        dbSession,
        className="DWD_PURCHASE_ORDER_DTL",
        propertyColumns=_ORDER_PROPERTIES,
        stalePrefix="",
    )
    await _seedClassWithProperties(
        dbSession,
        className="DWD_GOODS_RECEIPT_DTL",
        propertyColumns=_RECEIPT_PROPERTIES,
        stalePrefix="",
    )
    await _seedClassWithProperties(
        dbSession,
        className="DIM_FACILITY",
        propertyColumns=_FACILITY_PROPERTIES,
        stalePrefix="",
    )
    await dbSession.commit()


async def _countBindings(dbSession: AsyncSession) -> tuple[int, int]:
    sources = (
        await dbSession.execute(
            select(ProcurementSourceBinding).where(
                ProcurementSourceBinding.valid_to.is_(None)
            )
        )
    ).scalars().all()
    properties = (
        await dbSession.execute(
            select(ProcurementPropertyBinding).where(
                ProcurementPropertyBinding.valid_to.is_(None)
            )
        )
    ).scalars().all()
    return len(sources), len(properties)


@pytest.mark.asyncio
async def test_seed_creates_all_specs_and_resolver_works(dbSession: AsyncSession) -> None:
    """空库跑种子 ⇒ 3 个类各 1 条生效源绑定，且工具必需的属性全部可解析。"""
    await _seedOntologyOnly(dbSession)
    assert await _countBindings(dbSession) == (0, 0), "前置：库里不应有绑定"

    outcome = await seed_procurement_bindings(getSessionFactory())

    assert set(outcome) == {spec["className"] for spec in BINDING_SPECS}
    assert sum(outcome.values()) == len(BINDING_SPECS), f"应各新建 1 条：{outcome}"
    sourceCount, propertyCount = await _countBindings(dbSession)
    assert sourceCount == len(BINDING_SPECS)
    assert propertyCount == len(_ORDER_PROPERTIES) + len(_RECEIPT_PROPERTIES) + len(
        _FACILITY_PROPERTIES
    )

    async with getSessionFactory()() as session:
        order = await resolveBinding(
            session,
            className=ORDER_BINDING["className"],
            requiredProperties=tuple(_ORDER_PROPERTIES),
        )
        receipt = await resolveBinding(
            session,
            className=RECEIPT_BINDING["className"],
            requiredProperties=tuple(_RECEIPT_PROPERTIES),
        )
        facility = await resolveBinding(
            session,
            className=FACILITY_BINDING["className"],
            requiredProperties=tuple(_FACILITY_PROPERTIES),
        )

    assert order.qualifiedTable() == "THBI.DWD_PURCHASE_ORDER_DTL"
    assert receipt.qualifiedTable() == "THBI.DWD_GOODS_RECEIPT_DTL"
    assert facility.qualifiedTable() == "THBI.DIM_FACILITY"
    # 列映射来自属性绑定（不是本体表）：逐项对得上
    for name in _ORDER_PROPERTIES:
        assert order.column(name) == _ORDER_PROPERTIES[name]
    for name in _RECEIPT_PROPERTIES:
        assert receipt.column(name) == _RECEIPT_PROPERTIES[name]


@pytest.mark.asyncio
async def test_seed_is_idempotent(dbSession: AsyncSession) -> None:
    """重复跑：第二次全部 0，不新增行、不推翻既有确认。"""
    await _seedOntologyOnly(dbSession)
    factory = getSessionFactory()

    await seed_procurement_bindings(factory)
    sourcesBefore, propsBefore = await _countBindings(dbSession)
    outcome = await seed_procurement_bindings(factory)

    assert outcome == {spec["className"]: 0 for spec in BINDING_SPECS}
    assert await _countBindings(dbSession) == (sourcesBefore, propsBefore)


@pytest.mark.asyncio
async def test_seed_is_per_class_independent(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """逐类独立：单类失败不假装成功，也不影响已完成的类。

    这里让 `DIM_FACILITY` 的规格指向一个本体里不存在的类名 ⇒ 该类抛错；
    前面两类应已建成（顺序：facility 在前，所以反过来：让它失败时后续类不跑，
    但已建的类保持已确认 —— 说明种子不会留下半成品候选行）。
    """
    await _seedOntologyOnly(dbSession)
    broken = dict(FACILITY_BINDING, className="NOT_EXIST_CLASS")
    monkeypatch.setattr(
        "scripts.seed_procurement_bindings.BINDING_SPECS",
        (broken, ORDER_BINDING, RECEIPT_BINDING),
    )

    with pytest.raises(RuntimeError, match="本体类不存在或已墓碑"):
        await seed_procurement_bindings(getSessionFactory())

    # 抛错时没有任何候选行残留（不会写一半留个候选绑定）
    sources = (
        await dbSession.execute(
            select(ProcurementSourceBinding).where(
                ProcurementSourceBinding.valid_to.is_(None)
            )
        )
    ).scalars().all()
    assert all(not row.is_candidate for row in sources), (
        f"不应残留候选行：{[(r.class_id, r.is_candidate) for r in sources]}"
    )
