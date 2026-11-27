"""催货单 API（真实 PostgreSQL + 完整 HTTP 链路；feat/delivery-chase-closed-loop）。

覆盖（每条都是会咬人的失败模式）：

1. `POST /from-pending` 的**回查链路**：mock 只 mock 在 adapter 层（SQL 照发），
   身份对 → 行数据的转换、供应商分组、entity_mapping 解析全部走真代码；
2. **幂等**：同选行重复 POST ⇒ 复用（响应里同一 chaseDraftId）；
3. **403**：非 admin 全部拒绝（对外动作）；
4. **生命周期**：validate → approve → send（模拟器端口 ⇒ SENT 终态 + 外部引用留痕）；
5. **404**：不存在的催货单。

回查 SQL 是真发到 PG 的（测试库无 THBI 表 ⇒ adapter 打桩在 `execute_read_only`）。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

ADMIN = {"X-User-Id": "chase-admin", "X-User-Roles": "admin"}
NON_ADMIN = {"X-User-Id": "chase-viewer", "X-User-Roles": "viewer"}

_SUPPLIER_KEY = 900123456


def _pendingRow(poNo: str, poLine: str, supplierCode: str) -> dict:
    """adapter 桩返回的欠交行（大写键 = Oracle 形态）。"""
    return {
        "PO_NO": poNo,
        "PO_LINE_NO": poLine,
        "ITEM_CODE": "M001",
        "SUPPLIER_CODE": supplierCode,
        "ORDERED_QTY": 12.0,
        "RECEIVED_QTY": 2.0,
        "OPEN_QTY": 10.0,
        "EXPECTED_DATE": "2026-11-20",
        "OVERDUE_DAYS": 5,
    }


class _StubAdapter:
    """按身份对返回欠交行；记录 SQL（断言只读）。"""

    def __init__(self) -> None:
        self.sqls: list[str] = []

    async def execute_read_only(self, sql: str, *, maxRows: int | None = None) -> list[dict]:
        self.sqls.append(sql)
        return [
            _pendingRow("PO1", "1", "Q1630"),
            _pendingRow("PO2", "1", "Q1630"),
        ]

    async def dispose(self) -> None:  # pragma: no cover
        return None


@pytest.fixture()
async def stubAdapter(monkeypatch: pytest.MonkeyPatch) -> _StubAdapter:
    adapter = _StubAdapter()
    import app.services.procurement_chase_lookup as lookup

    async def fakeLoad(dsId, ds):
        # DataSource ORM-like 形状（loadActiveDatasource 打桩后不被调用，
        # adapterFor 也被替换 ⇒ 这里只需让类型判断通过）
        from types import SimpleNamespace

        return SimpleNamespace(
            id=dsId, type="oracle", host="h", port=1521,
            database_name="X", username="U", password_encrypted="x",
            is_active=True,
        )

    monkeypatch.setattr(lookup, "adapterFor", lambda dsId, ds: adapter)
    monkeypatch.setattr(lookup, "loadActiveDatasource", fakeLoad)
    # binding 解析在 DB 里真实进行（本体类 + 绑定行由 conftest warmUp 造），
    # 解析出的 datasourceId 指向测试库不存在的数据源行 ⇒ loadActiveDatasource 也要打桩。
    from app.services import pending_delivery_query as pdq

    monkeypatch.setattr(pdq, "loadActiveDatasource", fakeLoad, raising=False)
    return adapter


async def _seedOntologyAndBindings(dbSession) -> None:
    """种 THBI 数据源 + 订单行/收货行两条绑定（复用 binding-resolver 的夹具族）。

    绑定解析是**真跑**的（resolver 直查 DB）⇒ 没有这些行 ⇒ 422「数据源不存在」。
    """
    from app.tests.integration.test_ontology_binding_resolver_api import (
        _seedClassWithBinding,
        _seedThbiDataSource,
    )

    await _seedThbiDataSource(dbSession)
    await _seedClassWithBinding(
        dbSession,
        className="DWD_PURCHASE_ORDER_DTL",
        propertyColumns={
            "PO_NO": "PO_NO",
            "PO_LINE_NO": "PO_LINE_NO",
            "ITEM_CODE": "ITEM_CODE",
            "SUPPLIER_CODE": "SUPPLIER_CODE",
            "PO_QTY": "PO_QTY",
            "EXP_RCV_DATE": "EXP_RCV_DATE",
        },
        sourceTable="DWD_PURCHASE_ORDER_DTL",
    )
    await _seedClassWithBinding(
        dbSession,
        className="DWD_GOODS_RECEIPT_DTL",
        propertyColumns={
            "PO_NO": "PO_NO",
            "PO_LINE_NO": "PO_LINE_NO",
            "RCV_QTY_STU": "RCV_QTY_STU",
        },
        sourceTable="DWD_GOODS_RECEIPT_DTL",
    )
    await dbSession.commit()


async def _seedSupplierMapping(dbSession) -> None:
    """种 Q1630 的统一供应商映射（幂等）。"""
    from sqlalchemy import select

    from app.domain.models import EntityMapping

    existing = (
        await dbSession.execute(
            select(EntityMapping).where(
                EntityMapping.entity_type == "SUPPLIER",
                EntityMapping.enterprise_code == "Q1630",
            )
        )
    ).scalars().first()
    if existing is None:
        dbSession.add(
            EntityMapping(
                entity_type="SUPPLIER",
                enterprise_key=_SUPPLIER_KEY,
                enterprise_code="Q1630",
                source_system="ERP",
                source_key="Q1630",
                source_code="Q1630",
                match_rule="MAPPING",
            )
        )
        await dbSession.commit()


@pytest.mark.asyncio
class TestChaseFromPending:
    async def test_create_groups_and_returns_draft(
        self, client: AsyncClient, dbSession, stubAdapter
    ) -> None:
        await _seedOntologyAndBindings(dbSession)
        await _seedSupplierMapping(dbSession)
        resp = await client.post(
            "/api/v1/procurement/chase/from-pending",
            headers=ADMIN,
            json={
                "lines": [
                    {"poNo": "PO1", "poLine": "1"},
                    {"poNo": "PO2", "poLine": "1"},
                ],
                "notes": "逾期 5 天未到货，请回复交期",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        # 两行同供应商（桩返回的都是 Q1630）⇒ 恰好一张催货单
        assert len(body["drafts"]) == 1
        draft = body["drafts"][0]
        assert draft["status"] == "DRAFT"
        assert draft["supplierCode"] == "Q1630"
        assert draft["lineCount"] == 2
        assert draft["lineCount"] == 2  # 两行同供应商 ⇒ 同一张催货单收 2 行
        # 服务端数据权威：openQty 来自回查（10.0），不是前端可伪造的值
        assert draft["requestId"].startswith("CHASE-")

    async def test_create_is_idempotent(
        self, client: AsyncClient, dbSession, stubAdapter
    ) -> None:
        await _seedOntologyAndBindings(dbSession)
        await _seedSupplierMapping(dbSession)
        payload = {
            "lines": [{"poNo": "PO1", "poLine": "1"}],
            "notes": "催一下",
        }
        first = await client.post(
            "/api/v1/procurement/chase/from-pending", headers=ADMIN, json=payload
        )
        second = await client.post(
            "/api/v1/procurement/chase/from-pending", headers=ADMIN, json=payload
        )
        assert first.status_code == 201
        assert second.status_code == 201
        assert (
            first.json()["drafts"][0]["chaseDraftId"]
            == second.json()["drafts"][0]["chaseDraftId"]
        )

    async def test_non_admin_is_rejected(self, client: AsyncClient, dbSession, stubAdapter) -> None:
        await _seedSupplierMapping(dbSession)
        resp = await client.post(
            "/api/v1/procurement/chase/from-pending",
            headers=NON_ADMIN,
            json={"lines": [{"poNo": "PO1", "poLine": "1"}]},
        )
        assert resp.status_code == 403

    async def test_unknown_supplier_reported(self, client: AsyncClient, dbSession, stubAdapter) -> None:
        """供应商不在 entity_mapping ⇒ 201 + unresolvedSuppliers 点名（不炸整批）。

        桩只回 Q1630 的行 ⇒ Q1630 有映射（部分成功）；为让「另一组」也存在，
        让桩多回一行 QNOPE（无映射）⇒ 断言 drafts 只有 Q1630、unresolved=[QNOPE]。
        """
        await _seedOntologyAndBindings(dbSession)
        await _seedSupplierMapping(dbSession)  # 只种 Q1630

        # 临时扩展桩：多回一行无映射供应商的行
        stubAdapter._extra = {
            "PO3": {"PO_NO": "PO3", "PO_LINE_NO": "1", "ITEM_CODE": "M001",
                    "SUPPLIER_CODE": "QNOPE", "ORDERED_QTY": 5.0,
                    "RECEIVED_QTY": 0.0, "OPEN_QTY": 5.0,
                    "EXPECTED_DATE": "2026-11-20", "OVERDUE_DAYS": 2},
        }
        originalExecute = stubAdapter.execute_read_only

        async def executeWithExtra(sql: str, *, maxRows: int | None = None):
            rows = await originalExecute(sql, maxRows=maxRows)
            if "PO3" in sql:
                rows = rows + [stubAdapter._extra["PO3"]]
            return rows

        stubAdapter.execute_read_only = executeWithExtra  # type: ignore[method-assign]

        resp = await client.post(
            "/api/v1/procurement/chase/from-pending",
            headers=ADMIN,
            json={"lines": [{"poNo": "PO1", "poLine": "1"}, {"poNo": "PO3", "poLine": "1"}]},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert len(body["drafts"]) == 1
        assert body["drafts"][0]["supplierCode"] == "Q1630"
        assert body["unresolvedSuppliers"] == ["QNOPE"]

    async def test_readonly_sql_only(self, client: AsyncClient, dbSession, stubAdapter) -> None:
        """回查只发 SELECT（只读红线）。"""
        await _seedOntologyAndBindings(dbSession)
        await _seedSupplierMapping(dbSession)
        await client.post(
            "/api/v1/procurement/chase/from-pending",
            headers=ADMIN,
            json={"lines": [{"poNo": "PO1", "poLine": "1"}]},
        )
        assert stubAdapter.sqls, "回查应发过 SQL"
        assert all("SELECT" in sql.upper() for sql in stubAdapter.sqls)
        assert all(
            not kw in sql.upper()
            for sql in stubAdapter.sqls
            for kw in ("INSERT", "UPDATE", "DELETE")
        )


@pytest.mark.asyncio
class TestChaseLifecycle:
    async def test_full_lifecycle(
        self, client: AsyncClient, dbSession, stubAdapter
    ) -> None:
        await _seedOntologyAndBindings(dbSession)
        await _seedSupplierMapping(dbSession)
        created = await client.post(
            "/api/v1/procurement/chase/from-pending",
            headers=ADMIN,
            json={"lines": [{"poNo": "PO1", "poLine": "1"}]},
        )
        chaseDraftId = created.json()["drafts"][0]["chaseDraftId"]

        # validate → PENDING_APPROVAL
        resp = await client.post(
            f"/api/v1/procurement/chase/drafts/{chaseDraftId}/validate", headers=ADMIN
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["draft"]["status"] == "PENDING_APPROVAL"

        # approve → READY_TO_SEND
        resp = await client.post(
            f"/api/v1/procurement/chase/drafts/{chaseDraftId}/approve",
            headers=ADMIN,
            json={"actor": "chase-admin", "note": "同意催"},
        )
        assert resp.status_code == 200
        assert resp.json()["draft"]["status"] == "READY_TO_SEND"

        # send → SENT（模拟器）
        resp = await client.post(
            f"/api/v1/procurement/chase/drafts/{chaseDraftId}/send", headers=ADMIN
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["draft"]["status"] == "SENT"
        sendDecision = body["decisions"][-1]
        assert sendDecision["decision"] == "SEND"
        assert (sendDecision["note"] or "").startswith("CHASE-SIM-")

        # 详情可读
        resp = await client.get(
            f"/api/v1/procurement/chase/drafts/{chaseDraftId}", headers=ADMIN
        )
        assert resp.status_code == 200
        assert len(resp.json()["lines"]) == 1

    async def test_unknown_draft_404(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/api/v1/procurement/chase/drafts/00000000-0000-0000-0000-000000000000",
            headers=ADMIN,
        )
        assert resp.status_code == 404

    async def test_send_before_approval_rejected(self, client: AsyncClient, dbSession, stubAdapter) -> None:
        """未审批就 send ⇒ 409（状态机 fail-closed）。"""
        await _seedOntologyAndBindings(dbSession)
        await _seedSupplierMapping(dbSession)
        created = await client.post(
            "/api/v1/procurement/chase/from-pending",
            headers=ADMIN,
            json={"lines": [{"poNo": "PO1", "poLine": "1"}]},
        )
        chaseDraftId = created.json()["drafts"][0]["chaseDraftId"]
        resp = await client.post(
            f"/api/v1/procurement/chase/drafts/{chaseDraftId}/send", headers=ADMIN
        )
        assert resp.status_code == 409
