"""待到货场景的轻量统计端点（欢迎态业务现状卡）。

设计动机（2026-11-24）：场景工作区的「业务现状」卡需要**进入即出数**，
走编排 run 要烧几千 token 且等 worker —— 统计卡直接调 handler（只读 SQL），
零 LLM 成本。路由只挂登录守卫；ACL 语义与 Agent 工具面一致（只读）。

2026-11-25 迁本体 + THBI：handler / 明细端点 / 基地端点全部经
`resolveBinding` 拿物理表列（真实 PG 绑定），数据源固定 THBI。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agent_tool_types import ToolResult
from app.tests.integration._agent_runs_api_helpers import OWNER_HEADERS
from app.tests.integration._agent_runs_api_helpers import client as client
from app.tests.integration.test_ontology_binding_resolver_api import (
    _seedClassWithBinding,
    _seedThbiDataSource,
)

SUMMARY_PATH = "/api/v1/procurement/pending-delivery/summary"
SITES_PATH = "/api/v1/procurement/pending-delivery/sites"
LINES_PATH = "/api/v1/procurement/pending-delivery/lines"

_ORDER_COLUMNS = {
    "PO_NO": "PO_NO",
    "PO_LINE_NO": "PO_LINE_NO",
    "PO_QTY": "PO_QTY",
    "ORDER_DATE": "ORDER_DATE",
    "SUPPLIER_CODE": "SUPPLIER_CODE",
    "PUR_SITE_CODE": "PUR_SITE_CODE",
    "EXP_RCV_DATE": "EXP_RCV_DATE",
    "ITEM_CODE": "ITEM_CODE",
}
_RECEIPT_COLUMNS = {
    "PO_NO": "PO_NO",
    "PO_LINE_NO": "PO_LINE_NO",
    "RCV_QTY_STU": "RCV_QTY_STU",
}
_FACILITY_COLUMNS = {"FCY_0": "FCY_0", "FCYNAM_0": "FCYNAM_0"}


async def _seedPendingBindings(dbSession: AsyncSession) -> None:
    """种 THBI 数据源 + 订单行 / 收货行 / 基地三条确认生效绑定。"""
    await _seedThbiDataSource(dbSession)
    await _seedClassWithBinding(
        dbSession,
        className="DWD_PURCHASE_ORDER_DTL",
        propertyColumns=_ORDER_COLUMNS,
        sourceTable="DWD_PURCHASE_ORDER_DTL",
    )
    await _seedClassWithBinding(
        dbSession,
        className="DWD_GOODS_RECEIPT_DTL",
        propertyColumns=_RECEIPT_COLUMNS,
        sourceTable="DWD_GOODS_RECEIPT_DTL",
    )
    await _seedClassWithBinding(
        dbSession,
        className="DIM_FACILITY",
        propertyColumns=_FACILITY_COLUMNS,
        sourceTable="DIM_FACILITY",
    )


async def _fakeLoadDatasource(session, datasourceId):
    return SimpleNamespace(id=datasourceId, name="THBI", is_active=True)


@pytest.fixture(autouse=True)
def _clearPendingCache():
    """待到货同日缓存是模块级全局 ⇒ 用例间必须清理，否则命中缓存跳过查询。"""
    from app.services.pending_delivery_query import _PENDING_RESULT_CACHE

    _PENDING_RESULT_CACHE.clear()
    yield
    _PENDING_RESULT_CACHE.clear()


@pytest.mark.asyncio
async def test_summary_returns_structured_payload(
    client: AsyncClient, monkeypatch
) -> None:
    """默认参数：200 + summary/bySupplier/topLines 三段结构。"""
    captured: dict = {}

    async def _fakeHandler(session, args, ctx):
        captured["args"] = args
        return ToolResult(
            data={
                "summary": {"openLines": 12, "openQty": 5000.0,
                            "overdueLines": 7, "excludedRows": 3},
                "bySupplier": [{"supplierCode": "F109", "openLines": 5,
                                "openQty": 3000.0, "overdueLines": 4}],
                "topLines": [],
            },
            answer="ok",
        )

    from app.api.v1 import procurement_analytics

    monkeypatch.setattr(procurement_analytics, "pendingDeliveryHandler", _fakeHandler)

    resp = await client.get(SUMMARY_PATH, headers=OWNER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["openLines"] == 12
    assert body["bySupplier"][0]["supplierCode"] == "F109"
    assert "topLines" in body
    assert captured["args"]["monthsBack"] == 12


@pytest.mark.asyncio
async def test_summary_honors_query_slots(client: AsyncClient, monkeypatch) -> None:
    """monthsBack / 过滤槽位透传 handler；datasourceId 不再接线（THBI 硬规则）。"""
    captured: dict = {}

    async def _fakeHandler(session, args, ctx):
        captured["args"] = args
        return ToolResult(
            data={"summary": {"openLines": 0, "openQty": 0,
                              "overdueLines": 0, "excludedRows": 0},
                  "bySupplier": [], "topLines": []},
            answer="ok",
        )

    from app.api.v1 import procurement_analytics

    monkeypatch.setattr(procurement_analytics, "pendingDeliveryHandler", _fakeHandler)

    resp = await client.get(
        SUMMARY_PATH,
        headers=OWNER_HEADERS,
        params={"datasourceId": 7, "monthsBack": 3},
    )
    assert resp.status_code == 200
    assert captured["args"]["monthsBack"] == 3
    assert "datasourceId" not in captured["args"], (
        "数据源由本体绑定唯一确定，调用方指定一律不接线"
    )


@pytest.mark.asyncio
async def test_summary_requires_login(client: AsyncClient) -> None:
    """未登录 401/403（与采购域读端点同口径）。"""
    resp = await client.get(SUMMARY_PATH)
    # 守卫面生效即可：未带合法会话 ⇒ 401/403/409（种子用户撞 mustChangePassword）；
    # 无 THBI 绑定/数据源时绑定解析先 fail-closed（422），语义仍是「请求未放行」。
    assert resp.status_code in (401, 403, 409, 422)


@pytest.mark.asyncio
async def test_sites_returns_facility_dictionary(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """基地下拉数据源：基地类绑定（DIM_FACILITY ∩ 订单实际出现过的基地）。"""
    await _seedPendingBindings(dbSession)
    captured: dict = {}

    class _FakeAdapter:
        async def execute_read_only(self, sql: str):
            captured["sql"] = sql
            return [
                {"FCY_0": "T0", "FCYNAM_0": "泰鸿万立"},
                {"FCY_0": "T3", "FCYNAM_0": "模具厂"},
            ]

    from app.api.v1 import procurement_analytics

    monkeypatch.setattr(
        procurement_analytics, "_adapterFor", lambda dsId, ds: _FakeAdapter()
    )
    monkeypatch.setattr(
        procurement_analytics, "_loadActiveDatasource", _fakeLoadDatasource
    )

    resp = await client.get(SITES_PATH, headers=OWNER_HEADERS)
    assert resp.status_code == 200
    rows = resp.json()
    assert rows[0] == {"code": "T0", "name": "泰鸿万立"}
    sql = captured["sql"]
    assert "THBI.DIM_FACILITY" in sql and "EXISTS" in sql
    assert "THBI.DWD_PURCHASE_ORDER_DTL" in sql


@pytest.mark.asyncio
async def test_summary_passes_filter_slots(client: AsyncClient, monkeypatch) -> None:
    """多选供应商/物料/基地 + 日期段透传 handler（与高级参数同语义）。"""
    captured: dict = {}

    async def _fakeHandler(session, args, ctx):
        captured["args"] = args
        return ToolResult(
            data={"summary": {"openLines": 0, "openQty": 0,
                              "overdueLines": 0, "excludedRows": 0},
                  "bySupplier": [], "topLines": []},
            answer="ok",
        )

    from app.api.v1 import procurement_analytics

    monkeypatch.setattr(procurement_analytics, "pendingDeliveryHandler", _fakeHandler)

    resp = await client.get(
        SUMMARY_PATH,
        headers=OWNER_HEADERS,
        params={
            "supplierKeys": "F109,B125",
            "materialKeys": "502447700001",
            "siteKeys": "T3",
            "dateFrom": "2026-01-01",
            "dateTo": "2026-06-30",
        },
    )
    assert resp.status_code == 200
    assert captured["args"]["supplierKeys"] == ["F109", "B125"]
    assert captured["args"]["materialKeys"] == ["502447700001"]
    assert captured["args"]["siteKeys"] == ["T3"]
    assert captured["args"]["dateFrom"] == "2026-01-01"
    assert captured["args"]["dateTo"] == "2026-06-30"


@pytest.mark.asyncio
async def test_summary_rejects_bad_date(client: AsyncClient) -> None:
    """非法日期段 ⇒ 422（fail-closed）。"""
    resp = await client.get(
        SUMMARY_PATH,
        headers=OWNER_HEADERS,
        params={"dateFrom": "2026/01/01"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_summary_ignores_datasource_hint_and_uses_thbi(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """调用方给 datasourceId ⇒ 忽略；执行数据源由本体绑定固定为 THBI（id=1）。"""
    await _seedPendingBindings(dbSession)
    picked: dict = {}

    class _FakeAdapter:
        async def execute_read_only(self, sql: str):
            if "EXCLUDED_ROWS" in sql:
                return [{"OPEN_LINES": 0, "OPEN_QTY": 0,
                         "OVERDUE_LINES": 0, "EXCLUDED_ROWS": 0}]
            return []

    def _captureAdapter(dsId, ds):
        picked["dsId"] = dsId
        return _FakeAdapter()

    monkeypatch.setattr(
        "app.services.pending_delivery_query._adapterFor", _captureAdapter
    )
    monkeypatch.setattr(
        "app.services.pending_delivery_query._loadActiveDatasource",
        _fakeLoadDatasource,
    )

    resp = await client.get(
        SUMMARY_PATH, headers=OWNER_HEADERS, params={"datasourceId": 10}
    )
    assert resp.status_code == 200
    assert picked["dsId"] == 1, "执行数据源必须是本体绑定的 THBI，而不是请求参数"


@pytest.mark.asyncio
async def test_summary_datasource_failure_is_friendly_502(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """数据源侧失败 ⇒ 502 带友好 detail（未处理异常会绕过 CORS 头）。"""
    await _seedPendingBindings(dbSession)

    class _BoomAdapter:
        async def execute_read_only(self, sql: str):
            raise RuntimeError("(1248, 'Every derived table must have its own alias')")

    monkeypatch.setattr(
        "app.services.pending_delivery_query._adapterFor",
        lambda dsId, ds: _BoomAdapter(),
    )
    monkeypatch.setattr(
        "app.services.pending_delivery_query._loadActiveDatasource",
        _fakeLoadDatasource,
    )

    resp = await client.get(
        SUMMARY_PATH, headers=OWNER_HEADERS, params={"datasourceId": 10}
    )
    assert resp.status_code == 502
    assert "待到货查询失败" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_lines_endpoint_returns_paged_rows(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """明细页端点：返回 {total, rows}（分页），行含 MDM 供应商名与物料名。"""
    await _seedPendingBindings(dbSession)
    captured: list[str] = []

    class _FakeAdapter:
        async def execute_read_only(self, sql: str):
            captured.append(sql)
            if sql.strip().upper().startswith("SELECT COUNT(*)"):
                return [{"TOTAL_ROWS": 42}]
            return [
                {
                    "PO_NO": "T22511POH1251Q", "PO_LINE_NO": 1000,
                    "ITEM_CODE": "502447700001", "SUPPLIER_CODE": "F109",
                    "ORDERED_QTY": 1000000.0, "RECEIVED_QTY": 9600.0,
                    "OPEN_QTY": 990400.0, "EXPECTED_DATE": "2025-11-28",
                    "OVERDUE_DAYS": 360,
                },
            ]

    async def _fakeNames(session, *, entityType, codes):
        if entityType == "SUPPLIER":
            return {"F109": "上海纳特汽车标准件有限公司"}
        return {"502447700001": "X13 平头铆螺母"}

    monkeypatch.setattr(
        "app.api.v1.procurement_analytics._adapterFor",
        lambda dsId, ds: _FakeAdapter(),
    )
    monkeypatch.setattr(
        "app.api.v1.procurement_analytics._loadActiveDatasource",
        _fakeLoadDatasource,
    )
    monkeypatch.setattr(
        "app.services.pending_delivery_query.loadMdmNames", _fakeNames
    )

    resp = await client.get(
        LINES_PATH,
        headers=OWNER_HEADERS,
        params={"offset": 20, "limit": 20, "minOverdueDays": 30},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 42
    assert body["rows"][0]["supplierName"] == "上海纳特汽车标准件有限公司"
    assert body["rows"][0]["materialDesc"] == "X13 平头铆螺母"
    assert body["rows"][0]["overdueDays"] == 360
    # 分页与逾期过滤真的进了 SQL；表名来自绑定（THBI 数仓层）
    assert "OFFSET 20" in captured[-1]
    assert ">= 30" in captured[-1]
    assert "THBI.DWD_PURCHASE_ORDER_DTL" in captured[-1]


@pytest.mark.asyncio
async def test_lines_endpoint_rejects_bad_date(client: AsyncClient) -> None:
    resp = await client.get(
        LINES_PATH, headers=OWNER_HEADERS, params={"dateFrom": "2026/01/01"}
    )
    assert resp.status_code == 422
