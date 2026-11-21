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
    """C 方案共享 helper：停用源进入业务查询 ⇒ DataSourceInactiveError（fail-closed）。"""
    from app.domain.exceptions import DataSourceInactiveError
    from app.services import agent_tools_procurement

    ds = SimpleNamespace(id=2, name="test-ds", is_active=False)
    async def _fakeResolve(session, ctx):
        return ds

    monkeypatch.setattr(agent_tools_procurement, "_resolveBusinessDatasource", _fakeResolve)
    with pytest.raises(DataSourceInactiveError):
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


# ── I3-b：contract_document_search（合同类收窄；密级闸 fail-closed）─────────


def test_contract_document_search_registered() -> None:
    from app.services.agent_orchestration_validation import READ_ONLY_HANDLER_REFS
    from app.services.agent_tools import ARG_EXTRACTORS

    assert "contract_document_search" in BUILTIN_HANDLERS
    assert "contract_document_search" in ARG_EXTRACTORS
    # 编排运行能绑定/调用它的唯一依据就是这份白名单
    assert "contract_document_search" in READ_ONLY_HANDLER_REFS


@pytest.mark.asyncio
async def test_contract_document_search_narrows_to_contract_type(monkeypatch) -> None:
    from app.services import agent_tools_procurement as mod

    captured: dict = {}

    async def _fakeSearch(session, user, **kwargs):
        captured.update(kwargs)
        return [
            SimpleNamespace(
                model_dump=lambda **_: {"ref": "DOC-1", "snippet": "交付延迟按日 0.5% 计违约金"}
            )
        ]

    monkeypatch.setattr(mod, "searchProcurementDocuments", _fakeSearch)
    ctx = SimpleNamespace(user=SimpleNamespace(userId="u1"))

    result = await mod.contractDocumentSearchHandler(
        None, {"question": "合同里的交付违约金怎么算", "topK": 3}, ctx
    )

    assert captured["documentTypes"] == ["CONTRACT"]
    assert captured["topK"] == 3
    assert result.data["documentTypes"] == ["CONTRACT"]
    assert result.data["total"] == 1


@pytest.mark.asyncio
async def test_contract_document_search_fails_closed_without_user(monkeypatch) -> None:
    from app.services import agent_tools_procurement as mod

    async def _forbidden(*args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("无用户身份时不允许检索（密级闸 fail-closed）")

    monkeypatch.setattr(mod, "searchProcurementDocuments", _forbidden)

    result = await mod.contractDocumentSearchHandler(
        None, {"question": "合同?"}, SimpleNamespace(user=None)
    )

    assert result.data["evidence"] == []
    assert "密级" in result.answer


@pytest.mark.asyncio
async def test_contract_document_search_requires_question() -> None:
    from app.services import agent_tools_procurement as mod

    result = await mod.contractDocumentSearchHandler(
        None, {"question": "   "}, SimpleNamespace(user=SimpleNamespace(userId="u"))
    )

    assert result.data["evidence"] == []


@pytest.mark.asyncio
async def test_document_tool_resolves_user_from_actor(monkeypatch) -> None:
    """编排路径只给 actor（run.created_by）⇒ 工具要自己还原出用户身份。

    2026-11-21 实测：不还原 ⇒ 合同/制度检索永远停在「缺少用户身份」。
    """
    from app.services import agent_tools_procurement as mod

    captured: dict = {}

    async def _fakeResolve(session, userId):
        captured["resolvedFor"] = userId
        return SimpleNamespace(userId=userId, dbUserId=1, roles=("admin",), departments=())

    async def _fakeSearch(session, user, **kwargs):
        captured["user"] = user
        return []

    monkeypatch.setattr("app.dependencies.buildCurrentUserForUserId", _fakeResolve)
    monkeypatch.setattr(mod, "searchProcurementDocuments", _fakeSearch)

    # session 非 None：身份还原要查库（这里被 _fakeResolve 截获）
    await mod.contractDocumentSearchHandler(
        object(), {"question": "交付违约金"}, SimpleNamespace(user=None, actor="admin")
    )

    assert captured["resolvedFor"] == "admin"
    assert captured["user"] is not None


# ── 名称问法兜底：物料名 → 物料编码 → 供应关系图 ───────────────────────────


def test_describe_material_name_fallback_registered() -> None:
    """``_suppliersByMaterialName`` 是 supplier_search 的名称兜底入口。"""
    from app.services import agent_tools_procurement as mod

    assert hasattr(mod, "_suppliersByMaterialName")


@pytest.mark.asyncio
async def test_supplier_search_falls_back_to_material_name(monkeypatch) -> None:
    """按 SUPPLIER 检索 0 命中时，把关键词当**物料名**解析再走图。

    2026-11-21 实测：「FE-5顶丝M8×1×15」这类问法的关键词是物料名，
    旧行为直接甩「请提供物料编码」给用户，而物料名本可解析到编码。
    """
    from types import SimpleNamespace

    from app.services import agent_tools_procurement as mod

    searchedTypes: list[str | None] = []

    class _Service:
        async def searchMappings(self, session, *, q, entityType=None, limit=20):
            searchedTypes.append(entityType)
            if entityType == "MATERIAL":
                return [
                    SimpleNamespace(
                        source_key="504014710001",
                        enterprise_code="504014710001",
                        enterprise_key=302928064950908,
                        name="FE-5顶丝M8×1×15 T00147100",
                    )
                ]
            return []  # 按供应商检索：0 命中

    async def _fakeSuppliers(materialKey: str, limit: int = 20):
        assert materialKey == "504014710001"
        return [
            {"key": "Q189", "code": "Q189", "name": "威德车业部件有限公司"},
            {"key": "C200", "code": "C200", "name": "宁波伯豪汽配有限公司"},
        ]

    monkeypatch.setattr(mod, "EntityMappingService", lambda: _Service())
    monkeypatch.setattr(mod, "findSuppliersOfMaterial", _fakeSuppliers)

    result = await mod.supplierSearchHandler(None, {"q": "FE-5顶丝M8×1×15"}, SimpleNamespace())

    assert searchedTypes == ["SUPPLIER", "MATERIAL"]
    assert result.data["via"] == "material_name"
    assert [s["key"] for s in result.data["suppliers"]] == ["Q189", "C200"]
    assert result.data["materials"][0]["code"] == "504014710001"
    assert "504014710001" in result.answer


@pytest.mark.asyncio
async def test_supplier_search_material_without_suppliers_is_distinct_from_not_found(
    monkeypatch,
) -> None:
    """「物料存在但无在册供应商」必须与「连物料都没搜到」给出不同结论。"""
    from types import SimpleNamespace

    from app.services import agent_tools_procurement as mod

    class _Service:
        async def searchMappings(self, session, *, q, entityType=None, limit=20):
            if entityType == "MATERIAL":
                return [
                    SimpleNamespace(
                        source_key="AJX0000001",
                        enterprise_code="AJX0000001",
                        enterprise_key=1,
                        name="某物料",
                    )
                ]
            return []

    async def _noSuppliers(materialKey: str, limit: int = 20):
        return []

    monkeypatch.setattr(mod, "EntityMappingService", lambda: _Service())
    monkeypatch.setattr(mod, "findSuppliersOfMaterial", _noSuppliers)

    found = await mod.supplierSearchHandler(None, {"q": "某物料"}, SimpleNamespace())
    assert found.data["materials"] and found.data["suppliers"] == []
    assert "没有登记" in found.answer

    class _EmptyService:
        async def searchMappings(self, session, *, q, entityType=None, limit=20):
            return []

    monkeypatch.setattr(mod, "EntityMappingService", lambda: _EmptyService())
    missing = await mod.supplierSearchHandler(None, {"q": "不存在"}, SimpleNamespace())
    assert "未命中" in missing.answer
    assert missing.data["via"] == "entity_mapping"
