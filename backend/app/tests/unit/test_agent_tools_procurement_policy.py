"""M3-4 收尾批：`quotation_query`（年度报价明细）+ `procurement_policy_search`
（M2-3 文档检索接工具 + ctx 扩 user）。

判据要点：
1. **quotation_query 落点 = `DWD_PURCHASE_QUOTATION_YEARLY_DTL`**（源侧实测：
   167,909 行，ITEM_CODE/SUPPLIER_CODE/SEG_YEAR/UNIT_PRICE_EX_TAX 100% 填充）——
   `DWD_SUPPLIER_PRICE_LIST` 的 MATERIAL_CODE **全 NULL**（source-capability.md:35）
   ⇒ 不可用，这是本工具换表的依据；
2. **policy_search 的密级闸 fail-closed**：ctx.user 缺失 ⇒ 不检索（文档检索无
   密级核验 = 越权面）⇒ 空结果 + 明确提示；ctx.user 在 ⇒ 走 M2-3 全链
   （clearance 经 searchProcurementDocuments 透传）；
3. **ctx 扩 user**：AgentToolContext 加可选 user；runtime.run 透传（API 层的
   _user 依赖现状未使用 ⇒ 接上）；
4. **注册 + 三处集合守卫同步**（第 5/6 个工具）。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.services.agent_tool_types import AgentToolContext
from app.services.agent_tools import BUILTIN_HANDLERS

# ── 1. 注册 ───────────────────────────────────────────────────────────────


def test_m34_tools_registered() -> None:
    assert "quotation_query" in BUILTIN_HANDLERS
    assert "procurement_policy_search" in BUILTIN_HANDLERS
    from app.services.agent_tools import ARG_EXTRACTORS

    assert "quotation_query" in ARG_EXTRACTORS
    assert "procurement_policy_search" in ARG_EXTRACTORS


# ── 2. quotation_query ────────────────────────────────────────────────────


def test_build_quotation_sql_shape() -> None:
    from app.services.agent_tools_procurement import buildQuotationSql

    sql = buildQuotationSql(
        supplierKey="SUP001", materialKey=None, segmentYear=None, limit=20,
    )
    assert "DWD_PURCHASE_QUOTATION_YEARLY_DTL" in sql
    assert "SUPPLIER_CODE = 'SUP001'" in sql
    assert "FETCH FIRST 20 ROWS ONLY" in sql.upper()
    assert ";" not in sql

    sql2 = buildQuotationSql(
        supplierKey=None, materialKey="ITM-1", segmentYear=2026, limit=5,
    )
    assert "ITEM_CODE = 'ITM-1'" in sql2
    assert "SEG_YEAR = 2026" in sql2
    assert "SUPPLIER_CODE = " not in sql2


@pytest.mark.asyncio
async def test_quotation_query_handler_executes_readonly(monkeypatch) -> None:
    from app.services import agent_tools_procurement

    captured: list[str] = []

    class _FakeAdapter:
        async def execute_read_only(self, sql: str):
            captured.append(sql)
            return [
                {"ITEM_CODE": "ITM-1", "SUPPLIER_CODE": "SUP001",
                 "SEG_YEAR": 2026, "UNIT_PRICE_EX_TAX": 88.5,
                 "SEG_START_DATE": date(2026, 1, 1),
                 "SEG_END_DATE": date(2026, 12, 31)},
            ]

    ds = SimpleNamespace(id=1, name="THBI", is_active=True)

    async def _fakeResolve(session, ctx):
        return ds

    monkeypatch.setattr(agent_tools_procurement, "_resolveBusinessDatasource", _fakeResolve)
    monkeypatch.setattr(
        agent_tools_procurement, "_adapterFor", lambda dsId, ds: _FakeAdapter()
    )

    result = await agent_tools_procurement.quotationQueryHandler(
        SimpleNamespace(),
        {"supplierKey": "SUP001", "segmentYear": 2026, "limit": 20},
        SimpleNamespace(),
    )
    assert len(captured) == 1
    assert result.data["via"] == "business_db"
    assert result.data["rows"][0]["itemCode"] == "ITM-1"
    assert result.data["rows"][0]["unitPriceExTax"] == 88.5
    assert "2026" in result.answer


@pytest.mark.asyncio
async def test_quotation_query_requires_selector(dbSession) -> None:
    from app.services.agent_tools_procurement import quotationQueryHandler

    result = await quotationQueryHandler(dbSession, {}, SimpleNamespace())
    assert result.data["rows"] == []
    assert result.answer, "缺选择器 ⇒ 友好提示"


# ── 3. procurement_policy_search（ctx.user 密级闸 fail-closed）────────────


@pytest.mark.asyncio
async def test_policy_search_without_user_is_fail_closed(dbSession) -> None:
    from app.services.agent_tools_procurement import procurementPolicySearchHandler


    result = await procurementPolicySearchHandler(
        dbSession, {"question": "询价必须邀请几家供应商"}, SimpleNamespace()
    )
    assert result.data["evidence"] == []
    assert "密级" in result.answer, "无身份 ⇒ 不检索（越权面），提示必须明确"


@pytest.mark.asyncio
async def test_policy_search_with_user_returns_evidence(dbSession, monkeypatch) -> None:
    from app.services import agent_tools_procurement
    from app.services.agent_tools_procurement import procurementPolicySearchHandler

    user = SimpleNamespace(userId="u1", roles=("admin",), departments=("procurement",))
    captured: dict = {}

    async def _fakeSearch(session, usr, *, query, documentTypes=None,
                          entityKeys=None, effectiveAt=None, topK=5):
        captured["user"] = usr
        captured["query"] = query
        from app.domain.knowledge_schemas import KnowledgeEvidenceRead

        return [KnowledgeEvidenceRead(
            channel="RAG", kind="DOCUMENT", ref="DOC-1", title="采购制度 v1",
            snippet="询价应当邀请不少于三家供应商",
        )]

    monkeypatch.setattr(
        agent_tools_procurement, "searchProcurementDocuments", _fakeSearch
    )

    result = await procurementPolicySearchHandler(
        dbSession,
        {"question": "询价必须邀请几家供应商", "limit": 5},
        SimpleNamespace(user=user),  # ctx 带 user
    )
    assert captured["user"] is user
    assert captured["query"] == "询价必须邀请几家供应商"
    assert len(result.data["evidence"]) == 1
    assert result.data["evidence"][0]["ref"] == "DOC-1"
    assert "1" in result.answer


# ── 4. ctx.user 透传（runtime 接线）──────────────────────────────────────


def test_runtime_run_passes_user_to_ctx(monkeypatch) -> None:
    """runtime.run 的 user 参数必须落到 AgentToolContext.user（密级闸的数据源）。"""
    import inspect

    from app.services.agent_runtime_service import AgentRuntimeService

    # 直接断言 run 签名含 user 参数 + ctx 构造引用它（源码级，避免拉起全链）
    runSrc = inspect.getsource(AgentRuntimeService.run)
    assert "user: Any | None = None" in runSrc or "user=user" in runSrc, (
        "AgentRuntimeService.run 必须接受并透传 user"
    )
    ctxFields = AgentToolContext.__dataclass_fields__  # type: ignore[attr-defined]
    assert "user" in ctxFields, "AgentToolContext 必须有 user 字段"
