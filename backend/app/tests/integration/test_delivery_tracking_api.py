"""到货跟踪工具（集成）：真实 PostgreSQL + 真实本体绑定 + 真实 MDM 供应商检索。

外部业务库（THBI Oracle）在测试环境不可达 ⇒ 只把 **adapter** 换成假件；
本体绑定解析、数据源闸门、MDM（`entity_mapping`）供应商检索全部走真实 PG 行。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import MatchRule, SourceSystem
from app.domain.exceptions import NotFoundError
from app.domain.models import EntityMapping
from app.services import delivery_tracking_query as mod
from app.services.ontology_binding_resolver import BindingResolutionError
from app.tests.integration.test_ontology_binding_resolver_api import (
    _seedClassWithBinding,
    _seedThbiDataSource,
)

#: 订单行类绑定（属性名 → 物理列；与 Task 3 SQL 所需属性一致）。
_ORDER_COLUMNS = {
    "PO_NO": "PO_NO",
    "PO_LINE_NO": "PO_LINE_NO",
    "PO_QTY": "PO_QTY",
    "ORDER_DATE": "ORDER_DATE",
    "SUPPLIER_CODE": "SUPPLIER_CODE",
    "SUPPLIER_NAME": "SUPPLIER_NAME",
    "ITEM_CODE": "ITEM_CODE",
    "EXP_RCV_DATE": "EXP_RCV_DATE",
}
#: 收货行类绑定（含收货聚合需要的 `ITEM_DESC` / `RCV_DATE`）。
_RECEIPT_COLUMNS = {
    "PO_NO": "PO_NO",
    "PO_LINE_NO": "PO_LINE_NO",
    "RCV_QTY_STU": "RCV_QTY_STU",
    "ITEM_DESC": "ITEM_DESC",
    "RCV_DATE": "RCV_DATE",
}

_SUMMARY_ROW = {
    "TOTAL_LINES": 19908,
    "TOTAL_ORDERS": 4844,
    "ORDERED_QTY": 98495265.0,
    "RECEIVED_QTY": 39904579.0,
    "ALL_ARRIVED": 3986,
    "PARTIAL": 448,
    "NOT_ARRIVED": 410,
    "EXCLUDED_ROWS": 0,
}
_SUPPLIER_ROW = {
    "SUPPLIER_CODE": "B125",
    "ERP_SUPPLIER_NAME": "力航-ERP 名",
    "TOTAL_ORDERS": 29,
    "ALL_ARRIVED": 1,
    "PARTIAL": 16,
    "NOT_ARRIVED": 12,
    "ORDERED_QTY": 15155100.0,
    "RECEIVED_QTY": 2176026.0,
    "EXCLUDED_ROWS": 0,
}
#: MDM 无行的供应商 ⇒ 展示回退到 ERP 名（spec 5.7）。
_SUPPLIER_ROW_FALLBACK = {
    "SUPPLIER_CODE": "C1",
    "ERP_SUPPLIER_NAME": "宁波备用件-ERP",
    "TOTAL_ORDERS": 1,
    "ALL_ARRIVED": 0,
    "PARTIAL": 0,
    "NOT_ARRIVED": 1,
    "ORDERED_QTY": 50.0,
    "RECEIVED_QTY": 0.0,
    "EXCLUDED_ROWS": 0,
}
_ORDER_ROW = {
    "PO_NO": "T22611POH0223",
    "PO_LINE_NO": 3000,
    "SUPPLIER_CODE": "B125",
    "ERP_SUPPLIER_NAME": "力航-ERP 名",
    "ORDER_DATE": "2026-10-28",
    "MATERIAL_CODE": "5A2100000002",
    "MATERIAL_DESC": "kun10/kun30/N1/N2方形焊接螺母M6",
    "ORDER_QTY": 1047000.0,
    "RECEIVED_QTY": 118261.0,
    "OPEN_QTY": 928739.0,
    "EXPECTED_DATE": "2026-11-07",
    "RECEIPT_DATE": "2026-11-24",
}


def _withoutErpName(row: dict) -> dict:
    """模拟 SQL 未 SELECT `ERP_SUPPLIER_NAME` 时的行形状。"""
    return {key: value for key, value in row.items() if key != "ERP_SUPPLIER_NAME"}


def _fakeAdapterFor(captured: list[str], *, withErpName: bool = True):
    """按请求形态分派三段结果（业务库在测试环境不可达）。

    `withErpName=False` 时行里不含 ERP 名——用于验证"绑定缺失 ⇒ SQL 不产该列"。
    """

    class _FakeAdapter:
        async def execute_read_only(self, sql: str):
            captured.append(sql)
            if "TOTAL_LINES" in sql:
                return [dict(_SUMMARY_ROW)]
            if "GROUP BY S.SUPPLIER_CODE" in sql:
                rows = [dict(_SUPPLIER_ROW), dict(_SUPPLIER_ROW_FALLBACK)]
            elif "AS TOTAL_ORDERS" in sql:
                return [{"TOTAL_ORDERS": 1}]
            else:
                rows = [dict(_ORDER_ROW)]
            return rows if withErpName else [_withoutErpName(row) for row in rows]

    return _FakeAdapter()


async def _seedSupplier(
    dbSession: AsyncSession,
    *,
    enterpriseKey: int,
    enterpriseCode: str,
    name: str,
    sourceSystem: SourceSystem = SourceSystem.ERP,
) -> None:
    """种一条供应商映射（真实 ORM 行；唯一键 = 类型+企业键+源系统）。

    默认用 `ERP`：生产库 `entity_mapping` 的名称主要挂在 ERP 行（MDM 行为 0）
    ⇒ 测试数据必须与生产同形，否则「按 source_system 过滤」的回归抓不到。
    """
    dbSession.add(
        EntityMapping(
            entity_type="SUPPLIER",
            enterprise_key=enterpriseKey,
            enterprise_code=enterpriseCode,
            source_system=sourceSystem,
            source_key=str(enterpriseKey),
            source_code=enterpriseCode,
            match_rule=(MatchRule.MDM_MASTER if sourceSystem == SourceSystem.MDM else MatchRule.MAPPING),
            name=name,
        )
    )
    await dbSession.commit()


async def _seedDeliveryBindings(
    dbSession: AsyncSession,
    *,
    receiptColumns: dict[str, str] | None = None,
    orderColumns: dict[str, str] | None = None,
) -> None:
    """种 THBI 数据源 + 订单行 / 收货行两条确认生效绑定。"""
    await _seedThbiDataSource(dbSession)
    await _seedClassWithBinding(
        dbSession,
        className=mod.ORDER_CLASS_NAME,
        propertyColumns=_ORDER_COLUMNS if orderColumns is None else orderColumns,
        sourceTable=mod.ORDER_CLASS_NAME,
    )
    await _seedClassWithBinding(
        dbSession,
        className=mod.RECEIPT_CLASS_NAME,
        propertyColumns=_RECEIPT_COLUMNS if receiptColumns is None else receiptColumns,
        sourceTable=mod.RECEIPT_CLASS_NAME,
    )


# ---------------------------------------------------------------- 注册 / 种子


def test_tool_registered_in_registry_and_seed_config() -> None:
    """工具已注册到 handler 引擎与 config 种子（否则规划器调不到）。"""
    from app.services.agent_tools import ARG_EXTRACTORS, BUILTIN_HANDLERS
    from scripts.seed_agent_tool_configs import TOOL_SEEDS

    assert callable(BUILTIN_HANDLERS["delivery_tracking_query"])
    assert "delivery_tracking_query" in ARG_EXTRACTORS
    seed = next(item for item in TOOL_SEEDS if item["name"] == "delivery_tracking_query")
    assert seed["handler_ref"] == "delivery_tracking_query"
    assert seed["arg_extractor_kind"] in ARG_EXTRACTORS
    # THBI 硬规则：数据源不是工具参数
    assert "datasourceId" not in seed["input_schema"]["properties"]


# ---------------------------------------------------------------- handler（真实绑定）


@pytest.mark.asyncio
async def test_handler_resolves_real_bindings_and_returns_three_sections(
    dbSession: AsyncSession, monkeypatch
) -> None:
    """真实 PG 绑定 → 物理表/列；三段结果 + window + MDM 供应商名。"""
    await _seedDeliveryBindings(dbSession)
    await _seedSupplier(
        dbSession,
        enterpriseKey=195779358766766,
        enterpriseCode="B125",
        name="浙江力航汽车部件有限公司",
    )
    capturedSql: list[str] = []
    monkeypatch.setattr(mod, "_getAdapter", lambda datasourceId, ds: _fakeAdapterFor(capturedSql))

    result = await mod.deliveryTrackingHandler(
        dbSession, {"anchorDay": 24, "periodIndex": 0, "limit": 50}, None
    )
    data = result.data

    expectedWindow = mod.resolveWindow(today=date.today(), anchorDay=24, periodIndex=0)
    assert data["window"] == {
        "dateFrom": expectedWindow.dateFrom.isoformat(),
        "dateTo": expectedWindow.dateTo.isoformat(),
        "mode": "anchorDay",
    }
    assert data["via"] == "business_db"
    assert data["datasourceId"] == 1
    assert data["dataLayer"] == "thbi_dwd"
    assert data["summary"]["orderArrivalRate"] == 82.29
    assert (
        data["summary"]["allArrivedOrders"]
        + data["summary"]["partialArrivedOrders"]
        + data["summary"]["notArrivedOrders"]
        == data["summary"]["totalOrders"]
    )
    assert data["bySupplier"][0]["supplierCode"] == "B125"
    assert data["bySupplier"][0]["supplierName"] == "浙江力航汽车部件有限公司"
    assert data["bySupplier"][0]["enterpriseKey"] == 195779358766766
    # MDM 无行 ⇒ 用 SQL 带出的 ERP 名回退（spec 5.7）
    assert data["bySupplier"][1]["supplierCode"] == "C1"
    assert data["bySupplier"][1]["supplierName"] == "宁波备用件-ERP"
    assert data["byOrder"][0]["status"] == "partial"
    assert data["byOrder"][0]["orderDate"] == "2026-10-28"
    assert data["byOrder"][0]["supplierName"] == "浙江力航汽车部件有限公司"
    # 物理表/列来自真实绑定行（不是硬编码）
    assert any("FROM THBI.DWD_PURCHASE_ORDER_DTL O" in sql for sql in capturedSql)
    assert any("FROM THBI.DWD_GOODS_RECEIPT_DTL" in sql for sql in capturedSql)
    # 可选 ERP 名属性被真实绑定解析到 SQL（MDM 无行时的回退来源）
    assert any("MAX(O.SUPPLIER_NAME) AS ERP_SUPPLIER_NAME" in sql for sql in capturedSql)
    assert any("O.SUPPLIER_NAME AS ERP_SUPPLIER_NAME" in sql for sql in capturedSql)


@pytest.mark.asyncio
async def test_erp_supplier_name_binding_is_optional(dbSession: AsyncSession, monkeypatch) -> None:
    """`SUPPLIER_NAME` 未绑定 ⇒ 不回退、不 fail-closed（只是没有 ERP 名）。"""
    await _seedDeliveryBindings(
        dbSession,
        orderColumns={key: value for key, value in _ORDER_COLUMNS.items() if key != "SUPPLIER_NAME"},
    )
    capturedSql: list[str] = []
    monkeypatch.setattr(
        mod,
        "_getAdapter",
        lambda datasourceId, ds: _fakeAdapterFor(capturedSql, withErpName=False),
    )

    result = await mod.deliveryTrackingHandler(dbSession, {"monthsBack": 3}, None)

    assert not any("ERP_SUPPLIER_NAME" in sql for sql in capturedSql)
    # 行里也没有 ERP 名 + MDM 无行 ⇒ 名称为 None（不回退、不 fail-closed）
    assert result.data["bySupplier"][1]["supplierName"] is None


@pytest.mark.asyncio
async def test_supplier_directory_reads_entity_mapping_regardless_of_source(
    dbSession: AsyncSession,
) -> None:
    """名称取 `entity_mapping.name`（SSOT），**不按 `source_system` 筛选**。

    生产库名称主要挂在 `ERP` 行（`MDM` 行为 0）⇒ 若实现按 `source_system=MDM`
    过滤，这里会拿不到名称。无行的 code 返回空（名称缺省）。
    """
    await _seedSupplier(
        dbSession,
        enterpriseKey=1,
        enterpriseCode="B125",
        name="浙江力航汽车部件有限公司",
        sourceSystem=SourceSystem.ERP,
    )

    from app.services.entity_mapping_names import loadMdmDirectory

    directory = await loadMdmDirectory(
        dbSession, entityType="SUPPLIER", codes=("B125", "NOPE")
    )

    assert directory["B125"]["name"] == "浙江力航汽车部件有限公司"
    assert directory["B125"]["enterpriseKey"] == 1
    assert "NOPE" not in directory, "无映射行 ⇒ 名称缺省，不臆造"


@pytest.mark.asyncio
async def test_handler_fails_closed_when_receipt_binding_lacks_aggregate_properties(
    dbSession: AsyncSession,
) -> None:
    """收货绑定缺 `ITEM_DESC` / `RCV_DATE` ⇒ fail-closed（聚合会 SELECT 它们）。"""
    await _seedDeliveryBindings(
        dbSession,
        receiptColumns={
            "PO_NO": "PO_NO",
            "PO_LINE_NO": "PO_LINE_NO",
            "RCV_QTY_STU": "RCV_QTY_STU",
        },
    )

    with pytest.raises(
        BindingResolutionError,
        match=r"缺少必需属性的绑定：\['ITEM_DESC', 'RCV_DATE'\]",
    ):
        await mod.deliveryTrackingHandler(dbSession, {"monthsBack": 3}, None)


# ---------------------------------------------------------------- MDM 供应商检索


@pytest.mark.asyncio
async def test_supplier_resolution_supports_code_key_exact_name_and_fuzzy(
    dbSession: AsyncSession,
) -> None:
    """四段阶梯：编码 → 企业键 → 全名精确 → 简称模糊。"""
    await _seedSupplier(
        dbSession,
        enterpriseKey=195779358766766,
        enterpriseCode="B125",
        name="浙江力航汽车部件有限公司",
    )

    assert await mod._resolveSupplierKey(dbSession, "B125") == (
        195779358766766,
        "B125",
        "浙江力航汽车部件有限公司",
    )
    assert await mod._resolveSupplierKey(dbSession, "195779358766766") == (
        195779358766766,
        "B125",
        "浙江力航汽车部件有限公司",
    )
    assert await mod._resolveSupplierKey(dbSession, "浙江力航汽车部件有限公司") == (
        195779358766766,
        "B125",
        "浙江力航汽车部件有限公司",
    )
    assert await mod._resolveSupplierKey(dbSession, "力航") == (
        195779358766766,
        "B125",
        "浙江力航汽车部件有限公司",
    )


@pytest.mark.asyncio
async def test_supplier_fuzzy_multi_hit_lists_candidates(dbSession: AsyncSession) -> None:
    """简称模糊命中多家 ⇒ NotFoundError 列候选；绝不静默取第一个。"""
    await _seedSupplier(
        dbSession,
        enterpriseKey=1,
        enterpriseCode="A002",
        name="浙江钢创供应链有限公司",
    )
    await _seedSupplier(
        dbSession,
        enterpriseKey=2,
        enterpriseCode="C001",
        name="钢创精工（苏州）有限公司",
    )

    with pytest.raises(NotFoundError) as exc:
        await mod._resolveSupplierKey(dbSession, "钢创")
    message = str(exc.value)
    assert "匹配到多个供应商" in message
    assert "A002" in message and "C001" in message


@pytest.mark.asyncio
async def test_unknown_supplier_raises_not_found(dbSession: AsyncSession) -> None:
    """四段全落空 ⇒ NotFoundError（防 typo 静默查成"全部供应商"）。"""
    await _seedSupplier(
        dbSession,
        enterpriseKey=1,
        enterpriseCode="A002",
        name="浙江钢创供应链有限公司",
    )

    from app.domain.exceptions import ValidationError

    with pytest.raises(ValidationError, match="不存在或尚未在 entity_mapping 建档"):
        await mod._resolveSupplierCodes(dbSession, ("查无此家XYZ",))
