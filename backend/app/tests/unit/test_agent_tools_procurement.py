"""M3-1 采购只读工具（`material_search` / `supplier_search`）单元守卫。

判据要点（设计稿 §5.4 工具契约）：
1. **三参契约**（`AgentHandler`）：`(session, args, ctx) -> ToolResult`，
   data JSON-serializable（camelCase）+ answer 用户可读；
2. **supplier_search 双路**：`materialKey` ⇒ Neo4j `SUPPLIES` 反查
   （Supplier-SUPPLIES->Material，35 万边）；无 materialKey ⇒ 名称/编码走
   entity_mapping（`searchMappings` AutoComplete 语义）——两条路都**只读**；
3. **白名单登记**：新工具必须进 `BUILTIN_HANDLERS`（`_VALID_HANDLER_REFS`
   从它派生 ⇒ 漏登记 = 配置引用时校验失败）；extractor 同步登记；
4. **零 LLM / 零写**：两个工具都是确定性只读检索（与 supplier_360 同款纪律）。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.domain.enums import MatchRule, SourceSystem
from app.domain.schemas import EntityMappingCreate
from app.services.agent_tools import BUILTIN_HANDLERS
from app.services.entity_mapping_service import EntityMappingService


def _actor():
    return SimpleNamespace(userId="m3-1-test", departments=())  # type: ignore[arg-type]


async def _seedMaterial(session, code: str, key: int, name: str) -> None:
    await EntityMappingService().createMapping(
        session,
        EntityMappingCreate(
            entity_type="MATERIAL",
            enterprise_key=key,
            enterprise_code=code,
            source_system=SourceSystem.ERP,
            source_key=f"ERP-{key}",
            source_code=f"ERP-{code}",
            match_rule=MatchRule.MDM_MASTER,
            name=name,
        ),
        actor=_actor(),  # type: ignore[arg-type]
    )


# ── 1. 白名单登记 ─────────────────────────────────────────────────────────


def test_procurement_tools_registered_in_builtin_whitelist() -> None:
    assert "material_search" in BUILTIN_HANDLERS, (
        "material_search 必须进 BUILTIN_HANDLERS（_VALID_HANDLER_REFS 从它派生）"
    )
    assert "supplier_search" in BUILTIN_HANDLERS
    from app.services.agent_tools import ARG_EXTRACTORS

    assert "material_search" in ARG_EXTRACTORS and "supplier_search" in ARG_EXTRACTORS


# ── 2. material_search handler（真实 PG）─────────────────────────────────


@pytest.mark.asyncio
async def test_material_search_handler_returns_matches(dbSession) -> None:
    from app.services.agent_tools_procurement import materialSearchHandler

    await _seedMaterial(dbSession, "ITM-STEEL-001", 500001, "不锈钢板")
    await _seedMaterial(dbSession, "ITM-BOLT-002", 500002, "六角螺栓")
    await dbSession.commit()

    result = await materialSearchHandler(
        dbSession, {"q": "不锈钢", "limit": 10}, SimpleNamespace()
    )
    materials = result.data["materials"]
    assert len(materials) == 1
    assert materials[0]["enterpriseCode"] == "ITM-STEEL-001"
    assert materials[0]["name"] == "不锈钢板"
    assert "不锈钢板" in result.answer, "answer 必须用户可读"
    assert result.tokens_used == 0, "零 LLM 纪律"


@pytest.mark.asyncio
async def test_material_search_empty_query_returns_empty(dbSession) -> None:
    from app.services.agent_tools_procurement import materialSearchHandler

    result = await materialSearchHandler(dbSession, {"q": "  "}, SimpleNamespace())
    assert result.data["materials"] == []
    assert "未命中" in result.answer


# ── 3. supplier_search handler ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_supplier_search_by_name_via_entity_mapping(dbSession) -> None:
    from app.services.agent_tools_procurement import supplierSearchHandler

    await EntityMappingService().createMapping(
        dbSession,
        EntityMappingCreate(
            entity_type="SUPPLIER",
            enterprise_key=900001,
            enterprise_code="SUP001",
            source_system=SourceSystem.ERP,
            source_key="ERP-900001",
            source_code="ERP-SUP001",
            match_rule=MatchRule.MDM_MASTER,
            name="泰鸿钢铁",
        ),
        actor=_actor(),  # type: ignore[arg-type]
    )
    await dbSession.commit()

    result = await supplierSearchHandler(dbSession, {"q": "泰鸿"}, SimpleNamespace())
    suppliers = result.data["suppliers"]
    assert len(suppliers) == 1 and suppliers[0]["enterpriseCode"] == "SUP001"
    assert result.data["via"] == "entity_mapping"


@pytest.mark.asyncio
async def test_supplier_search_by_material_key_uses_graph(monkeypatch) -> None:
    """materialKey ⇒ Neo4j SUPPLIES 反查（graph 路优先于名称检索）。"""
    from app.services import agent_tools_procurement

    async def _fake(materialKey: str, limit: int = 20) -> list[dict]:
        assert materialKey == "500001"
        return [
            {"key": "900001", "code": "SUP001", "name": "泰鸿钢铁"},
            {"key": "900002", "code": "SUP002", "name": "二钢"},
        ]

    monkeypatch.setattr(agent_tools_procurement, "findSuppliersOfMaterial", _fake)

    result = await agent_tools_procurement.supplierSearchHandler(
        SimpleNamespace(), {"materialKey": "500001", "limit": 20}, SimpleNamespace()
    )
    suppliers = result.data["suppliers"]
    assert [s["code"] for s in suppliers] == ["SUP001", "SUP002"]
    assert result.data["via"] == "graph_supplies", "必须自证走了哪条路"
    assert "2" in result.answer


@pytest.mark.asyncio
async def test_supplier_search_requires_some_selector(dbSession) -> None:
    from app.services.agent_tools_procurement import supplierSearchHandler

    result = await supplierSearchHandler(dbSession, {}, SimpleNamespace())
    assert result.data["suppliers"] == []
    assert result.answer, "空选择器 ⇒ 友好提示而非异常"


# ── M3-2：purchase_history_query（业务库只读管线）─────────────────────────


def test_purchase_history_registered() -> None:
    assert "purchase_history_query" in BUILTIN_HANDLERS
    from app.services.agent_tools import ARG_EXTRACTORS

    assert "purchase_history_query" in ARG_EXTRACTORS


def test_build_purchase_history_sql_shape_and_limits() -> None:
    from app.services.agent_tools_procurement import buildPurchaseHistorySql

    sql = buildPurchaseHistorySql(
        supplierKey="SUP001", materialKey=None, monthsBack=12, limit=20,
        today=date(2026, 11, 20),
    )
    assert sql.strip().upper().startswith("SELECT")
    assert "DWD_PURCHASE_ORDER_LINE" in sql
    assert "SUPPLIER_CODE = 'SUP001'" in sql, "字面量内联（adapter 无参数通道，单引号转义）"
    assert "FETCH FIRST 20 ROWS ONLY" in sql.upper()
    assert ";" not in sql, "单语句纪律（禁多语句）"
    # 时间窗：monthsBack=12 ⇒ 起点为 2025-11-20（DATE 字面量 Oracle/PG 通吃）
    assert "DATE '2025-11-20'" in sql
    # materialKey 缺省 ⇒ 无物料条件
    assert "MATERIAL_CODE = " not in sql

    sql2 = buildPurchaseHistorySql(
        supplierKey="SUP001", materialKey="ITM-1", monthsBack=3, limit=5,
        today=date(2026, 11, 20),
    )
    assert "MATERIAL_CODE = 'ITM-1'" in sql2
    assert "FETCH FIRST 5 ROWS ONLY" in sql2.upper()
    assert "2026-08-20" in sql2


@pytest.mark.asyncio
async def test_purchase_history_handler_executes_readonly(monkeypatch) -> None:
    """handler：adapter 只读执行两段 SQL（汇总+明细），数据 camelCase。"""
    from app.services import agent_tools_procurement

    captured: list[str] = []

    class _FakeAdapter:
        async def execute_read_only(self, sql: str):
            captured.append(sql)
            if "COUNT" in sql.upper():
                return [{
                    "ORDER_COUNT": 3, "TOTAL_QTY": 100.0,
                    "MIN_PRICE": 10.0, "MAX_PRICE": 12.0, "AVG_PRICE": 11.0,
                }]
            return [
                {"PO_NO": "PO-1", "MATERIAL_CODE": "ITM-1",
                 "ORDER_QTY": 30.0, "UNIT_PRICE": 10.0,
                 "CREATION_DATE": __import__("datetime").date(2026, 10, 1)},
                {"PO_NO": "PO-2", "MATERIAL_CODE": "ITM-1",
                 "ORDER_QTY": 70.0, "UNIT_PRICE": 12.0,
                 "CREATION_DATE": __import__("datetime").date(2026, 11, 1)},
            ]

    ds = SimpleNamespace(id=1, name="THBI", is_active=True)
    async def _fakeResolve(session, ctx):
        return ds

    monkeypatch.setattr(agent_tools_procurement, "_resolveBusinessDatasource", _fakeResolve)
    monkeypatch.setattr(
        agent_tools_procurement, "_adapterFor", lambda dsId, ds: _FakeAdapter()
    )

    result = await agent_tools_procurement.purchaseHistoryHandler(
        SimpleNamespace(),
        {"supplierKey": "SUP001", "monthsBack": 12, "limit": 20},
        SimpleNamespace(),
    )
    assert len(captured) == 2, "汇总 + 明细两段只读 SQL"
    assert result.data["via"] == "business_db"
    assert result.data["summary"]["orderCount"] == 3
    assert result.data["summary"]["maxPrice"] == 12.0
    assert len(result.data["rows"]) == 2
    assert result.data["rows"][0]["poNo"] == "PO-1"
    assert result.tokens_used == 0


@pytest.mark.asyncio
async def test_purchase_history_rejects_inactive_datasource(monkeypatch) -> None:
    """C 方案复用：停用源进入业务查询 ⇒ DataSourceInactiveError（fail-closed）。"""
    from app.domain.exceptions import ConflictError
    from app.services import agent_tools_procurement

    ds = SimpleNamespace(id=2, name="test-ds", is_active=False)
    async def _fakeResolve(session, ctx):
        return ds

    monkeypatch.setattr(agent_tools_procurement, "_resolveBusinessDatasource", _fakeResolve)
    with pytest.raises(ConflictError):
        await agent_tools_procurement.purchaseHistoryHandler(
            SimpleNamespace(),
            {"supplierKey": "SUP001"},
            SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_purchase_history_requires_supplier_key(dbSession) -> None:
    from app.services.agent_tools_procurement import purchaseHistoryHandler

    result = await purchaseHistoryHandler(dbSession, {}, SimpleNamespace())
    assert result.data["rows"] == [] and result.data["summary"] == {}
    assert result.answer, "缺 supplierKey ⇒ 友好提示"
