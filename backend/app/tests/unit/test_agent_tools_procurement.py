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


# ── P3：policy_compliance_check（制度合规核对）────────────────────────────────


def test_policy_compliance_registered() -> None:
    from app.services.agent_orchestration_validation import READ_ONLY_HANDLER_REFS
    from app.services.agent_tools import ARG_EXTRACTORS

    assert "policy_compliance_check" in BUILTIN_HANDLERS
    assert "policy_compliance_check" in ARG_EXTRACTORS
    assert "policy_compliance_check" in READ_ONLY_HANDLER_REFS


@pytest.mark.asyncio
async def test_policy_compliance_returnsEvidenceAndEntities(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.services import agent_tools_procurement as mod

    evidence = SimpleNamespace(
        model_dump=lambda **_: {"ref": "DOC-1", "title": "询价制度"},
        related_entities=(SimpleNamespace(type="ONTOLOGY_CLASS", code="DIM_SUPPLIER"),),
    )

    async def _fakeSearch(session, user, *, query, topK):
        assert "不少于三家" in query
        return [evidence]

    async def _fakeUser(session, ctx):
        return SimpleNamespace(userId="admin", dbUserId=1, roles=("admin",), departments=())

    monkeypatch.setattr(mod, "searchProcurementDocuments", _fakeSearch)
    monkeypatch.setattr(mod, "_resolveToolUser", _fakeUser)

    result = await mod.policyComplianceHandler(
        object(), {"claim": "询价应不少于三家供应商"}, SimpleNamespace()
    )

    assert result.data["evidence"]
    assert result.data["relatedEntities"] == ["ONTOLOGY_CLASS:DIM_SUPPLIER"]
    assert "以**查询数据**为准" in result.answer


@pytest.mark.asyncio
async def test_policy_compliance_failsClosedWithoutUser(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.services import agent_tools_procurement as mod

    async def _forbidden(*args, **kwargs):  # pragma: no cover
        raise AssertionError("无身份不得检索")

    async def _noUser(session, ctx):
        return None

    monkeypatch.setattr(mod, "searchProcurementDocuments", _forbidden)
    monkeypatch.setattr(mod, "_resolveToolUser", _noUser)

    result = await mod.policyComplianceHandler(
        object(), {"claim": "x"}, SimpleNamespace()
    )

    assert result.data["evidence"] == []
    assert "密级" in result.answer


@pytest.mark.asyncio
async def test_policy_compliance_requiresClaim() -> None:
    from types import SimpleNamespace

    from app.services import agent_tools_procurement as mod

    result = await mod.policyComplianceHandler(object(), {}, SimpleNamespace())

    assert result.data["evidence"] == []


# ── 待到货/逾期未到（pending_delivery_query，2026-11-25 迁本体 + THBI） ─────
#
# 口径（与到货跟踪同一套，THBI 探查实测）：
#   欠交量 = PO_QTY - 收货已收量（收货类按 PO_NO + PO_LINE_NO 聚合）
#   健全性 = 欠交量 < 1e6（「数量=物料码」脏数据必须剔除且显式回报）
#   时间窗 = ORDER_DATE >= today - monthsBack 月（DATE 字面量，确定性可测）
#   逾期   = today - EXP_RCV_DATE > 0
#
# 表名 / 列名只来自本体绑定（上面这些属性名经 ResolvedBinding 解析成物理列）。


def _requirePendingModule():
    from app.services import pending_delivery_query as mod

    return mod


def _pendingOrderBinding():
    from app.services.ontology_binding_resolver import ResolvedBinding

    return ResolvedBinding(
        className="DWD_PURCHASE_ORDER_DTL",
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DWD_PURCHASE_ORDER_DTL",
        columns={
            "PO_NO": "PO_NO",
            "PO_LINE_NO": "PO_LINE_NO",
            "PO_QTY": "PO_QTY",
            "ORDER_DATE": "ORDER_DATE",
            "SUPPLIER_CODE": "SUPPLIER_CODE",
            "PUR_SITE_CODE": "PUR_SITE_CODE",
            "EXP_RCV_DATE": "EXP_RCV_DATE",
            "ITEM_CODE": "ITEM_CODE",
        },
    )


def _pendingReceiptBinding():
    from app.services.ontology_binding_resolver import ResolvedBinding

    return ResolvedBinding(
        className="DWD_GOODS_RECEIPT_DTL",
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DWD_GOODS_RECEIPT_DTL",
        columns={
            "PO_NO": "PO_NO",
            "PO_LINE_NO": "PO_LINE_NO",
            "RCV_QTY_STU": "RCV_QTY_STU",
        },
    )


def _pendingFacilityBinding():
    from app.services.ontology_binding_resolver import ResolvedBinding

    return ResolvedBinding(
        className="DIM_FACILITY",
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DIM_FACILITY",
        columns={"FCY_0": "FCY_0", "FCYNAM_0": "FCYNAM_0"},
    )


def _pendingFilters(**kwargs):
    mod = _requirePendingModule()
    return mod.PendingDeliveryFilters(
        orderBinding=_pendingOrderBinding(),
        receiptBinding=_pendingReceiptBinding(),
        facilityBinding=_pendingFacilityBinding(),
        **kwargs,
    )


def test_pending_delivery_registered() -> None:
    assert "pending_delivery_query" in BUILTIN_HANDLERS
    from app.services.agent_tools import ARG_EXTRACTORS

    assert "pending_delivery_query" in ARG_EXTRACTORS


def test_pending_delivery_filters_fuzzy_and_dates() -> None:
    """供应商编码 / 物料编码 / 基地编码+名称 / 日期段：全部从绑定取物理列。"""
    mod = _requirePendingModule()

    filters = _pendingFilters(
        supplierKeys=("F109",),
        materialKeys=("502447700001",),
        siteKeys=("T3", "滨海"),
        dateFrom="2026-01-01",
        dateTo="2026-06-30",
    )
    sql = mod.buildPendingDeliveryLinesSql(
        monthsBack=12, today=date(2026, 11, 23),
        filters=filters, minOverdueDays=None, limit=10,
    )
    # 日期段覆盖 monthsBack（不再出现相对窗口起点）
    assert "DATE '2026-01-01'" in sql and "DATE '2026-06-30'" in sql
    assert "DATE '2025-11-23'" not in sql
    # 物料：只认已解析编码（名称在 handler 侧先经 MDM 解析）
    assert "O.ITEM_CODE IN ('502447700001')" in sql
    # 基地：编码精确 OR 名称模糊（名称需要 DIM_FACILITY 绑定）
    assert "O.PUR_SITE_CODE IN ('T3')" in sql
    assert "FCYNAM_0 LIKE '%滨海%'" in sql
    assert "JOIN THBI.DIM_FACILITY f ON f.FCY_0 = O.PUR_SITE_CODE" in sql
    assert ";" not in sql


def test_pending_delivery_filters_reject_unresolved_material_names() -> None:
    """未解析的中文物料名 fail-closed（迁 THBI 后没有数仓字典 LIKE 兜底）。"""
    from app.domain.exceptions import ValidationError

    mod = _requirePendingModule()
    filters = _pendingFilters(materialKeys=("齿轮",))
    with pytest.raises(ValidationError):
        mod.buildPendingDeliveryLinesSql(
            monthsBack=12, today=date(2026, 11, 23),
            filters=filters, minOverdueDays=None, limit=5,
        )


def test_pending_delivery_filters_reject_bad_date() -> None:
    """日期段格式非法 ⇒ ValidationError（fail-closed，不静默忽略）。"""
    from app.domain.exceptions import ValidationError

    with pytest.raises(ValidationError):
        _pendingFilters(dateFrom="2026/01/01")
    with pytest.raises(ValidationError):
        _pendingFilters(dateTo="2026-13-01")


def test_pending_delivery_in_tool_seed() -> None:
    """工具元数据必须进种子（input_schema 给 planner 出参依据）。"""
    from scripts.seed_agent_tool_configs import TOOL_SEEDS

    entry = next((s for s in TOOL_SEEDS if s["name"] == "pending_delivery_query"), None)
    assert entry is not None, "pending_delivery_query 必须进 TOOL_SEEDS"
    schema = entry["input_schema"]
    assert schema["additionalProperties"] is False
    for key in ("monthsBack", "dateFrom", "dateTo", "supplierKeys",
                "materialKeys", "siteKeys", "datasourceId", "limit"):
        assert key in schema["properties"]


def test_build_pending_delivery_summary_sql() -> None:
    mod = _requirePendingModule()

    sql = mod.buildPendingDeliverySummarySql(
        monthsBack=12, today=date(2026, 11, 23), filters=_pendingFilters()
    )
    # 回归（2026-11-24 生产实测 ORA-00904）：外层 FROM 子查询**没有**别名，
    # 引用内层别名（如 O.SUPPLIER_CODE）即 ORA-00904 ⇒ 500
    assert "GROUP BY O." not in sql
    assert "\n  O." not in sql
    assert sql.strip().upper().startswith("SELECT")
    # 表名来自本体绑定（THBI 数仓层，不是 ZJTH 源层）
    assert "THBI.DWD_PURCHASE_ORDER_DTL" in sql
    assert "THBI.DWD_GOODS_RECEIPT_DTL" in sql
    # 时间窗：12 个月前 = 2025-11-23
    assert "DATE '2025-11-23'" in sql
    # 欠交口径 = PO_QTY - 收货已收量（COALESCE 兜未收货）
    assert "O.PO_QTY - COALESCE(R.RECEIVED_QTY, 0)" in sql
    assert "1000000" in sql
    # 脏数据必须显式计数（不静默丢）
    assert "EXCLUDED_ROWS" in sql
    assert ";" not in sql


def test_build_pending_delivery_lines_sql_filters() -> None:
    mod = _requirePendingModule()

    sql = mod.buildPendingDeliveryLinesSql(
        monthsBack=12, today=date(2026, 11, 23),
        filters=_pendingFilters(), minOverdueDays=None, limit=20,
    )
    assert "SUPPLIER_CODE IN" not in sql, "无供应商 ⇒ 不加过滤"
    assert "FETCH FIRST 20 ROWS ONLY" in sql.upper()
    assert "OVERDUE_DAYS" in sql

    sql2 = mod.buildPendingDeliveryLinesSql(
        monthsBack=3, today=date(2026, 11, 23),
        filters=_pendingFilters(supplierKeys=("F109", "B125")),
        minOverdueDays=30, limit=5,
    )
    assert "O.SUPPLIER_CODE IN ('F109', 'B125')" in sql2

    # BySupplier 外层同样不得引用内层别名
    supplier_sql = mod.buildPendingDeliveryBySupplierSql(
        monthsBack=12, today=date(2026, 11, 23), limit=5, filters=_pendingFilters()
    )
    assert "GROUP BY O." not in supplier_sql
    assert "SUPPLIER_CODE," in supplier_sql
    assert "BPSUPPLIER" not in supplier_sql, "供应商名称改走 MDM，不再 JOIN 数仓字典"
    assert "SUPPLIER_NAME" not in supplier_sql, "名称由装配层从 MDM 补"
    # 明细 SQL 不再 JOIN 物料字典（描述由装配层从 MDM 补）
    assert "MATERIAL_DESC" not in sql2
    assert "ITMMASTER" not in sql2
    assert ">= 30" in sql2, "minOverdueDays 过滤逾期下限"
    assert "DATE '2026-08-23'" in sql2
    assert "FETCH FIRST 5 ROWS ONLY" in sql2.upper()


def test_pending_delivery_mysql_dialect_sql() -> None:
    """MySQL 方言：派生表别名 / NVL→COALESCE / TRUNC→DATEDIFF / LIMIT。"""
    mod = _requirePendingModule()

    summary = mod.buildPendingDeliverySummarySql(
        monthsBack=3, today=date(2026, 11, 24), filters=_pendingFilters(),
        dialect="mysql",
    )
    assert ") t" in summary
    assert "NVL(" not in summary
    assert "TRUNC(" not in summary
    assert "COALESCE(" in summary
    assert "DATEDIFF(" in summary
    assert "FETCH FIRST" not in summary.upper()

    supplier = mod.buildPendingDeliveryBySupplierSql(
        monthsBack=3, today=date(2026, 11, 24), limit=5,
        filters=_pendingFilters(), dialect="mysql",
    )
    assert ") t" in supplier
    assert "TRUNC(" not in supplier
    assert "LIMIT 5" in supplier
    assert "FETCH FIRST" not in supplier.upper()

    lines = mod.buildPendingDeliveryLinesSql(
        monthsBack=3, today=date(2026, 11, 24),
        filters=_pendingFilters(), minOverdueDays=30, limit=20,
        dialect="mysql",
    )
    assert "TRUNC(" not in lines
    assert "DATEDIFF(" in lines
    assert "LIMIT 20" in lines
    assert "FETCH FIRST" not in lines.upper()


def test_pending_delivery_oracle_dialect_default_unchanged() -> None:
    """默认（不传方言）保持现役 Oracle SQL 不变；派生表补裸别名对 Oracle 合法。"""
    mod = _requirePendingModule()

    sql = mod.buildPendingDeliverySummarySql(
        monthsBack=3, today=date(2026, 11, 24), filters=_pendingFilters(),
    )
    assert "NVL(" in sql
    assert "TRUNC(" in sql
    assert ") t" in sql, "派生表补裸别名（无 AS），Oracle/MySQL 双合法"


def _fakeBindingResolver():
    async def _fakeResolve(session, *, className, requiredProperties, datasourceId=1):
        return {
            "DWD_PURCHASE_ORDER_DTL": _pendingOrderBinding(),
            "DWD_GOODS_RECEIPT_DTL": _pendingReceiptBinding(),
            "DIM_FACILITY": _pendingFacilityBinding(),
        }[className]

    return _fakeResolve


async def _fakeLoadDatasource(session, datasourceId):
    return SimpleNamespace(id=datasourceId, name="THBI", is_active=True)


async def _fakeMdmNames(session, *, entityType, codes):
    if entityType == "SUPPLIER":
        return {"F109": "上海纳特汽车标准件有限公司"}
    return {"502447700001": "不锈钢板SUS304"}


class _PendingFakeAdapter:
    def __init__(self, captured: list[str]):
        self.captured = captured

    async def execute_read_only(self, sql: str):
        self.captured.append(sql)
        if "EXCLUDED_ROWS" in sql:
            return [{
                "OPEN_LINES": 12, "OPEN_QTY": 5000.0,
                "OVERDUE_LINES": 7, "EXCLUDED_ROWS": 3,
            }]
        if "GROUP BY SUPPLIER_CODE" in sql.upper():
            return [
                {"SUPPLIER_CODE": "F109", "OPEN_LINES": 5,
                 "OPEN_QTY": 3000.0, "OVERDUE_LINES": 4},
                {"SUPPLIER_CODE": "B170", "OPEN_LINES": 3,
                 "OPEN_QTY": 1200.0, "OVERDUE_LINES": 1},
            ]
        return [
            {"PO_NO": "T22511POH1251Q", "PO_LINE_NO": 1000,
             "ITEM_CODE": "502447700001", "SUPPLIER_CODE": "F109",
             "ORDERED_QTY": 1000000.0, "RECEIVED_QTY": 9600.0,
             "OPEN_QTY": 990400.0, "EXPECTED_DATE": "2025-11-28",
             "OVERDUE_DAYS": 360},
        ]


@pytest.mark.asyncio
async def test_pending_delivery_handler_shapes_result(monkeypatch) -> None:
    """handler：三段只读 SQL（汇总/按供应商/TOP 行），脏数据行数显式回报。"""
    mod = _requirePendingModule()
    captured: list[str] = []
    monkeypatch.setattr(mod, "resolveBinding", _fakeBindingResolver())
    monkeypatch.setattr(mod, "_loadActiveDatasource", _fakeLoadDatasource)
    monkeypatch.setattr(mod, "loadMdmNames", _fakeMdmNames)
    monkeypatch.setattr(
        mod, "_adapterFor", lambda dsId, ds: _PendingFakeAdapter(captured)
    )
    mod._PENDING_RESULT_CACHE.clear()

    result = await mod.pendingDeliveryHandler(
        None, {"monthsBack": 12}, SimpleNamespace()
    )
    assert len(captured) == 3, "汇总 + 按供应商 + TOP 行，三段只读 SQL"
    assert result.data["datasourceId"] == 1, "数据源固定 THBI（绑定解析）"
    assert result.data["summary"]["openLines"] == 12
    assert result.data["summary"]["excludedRows"] == 3, "脏数据必须显式回报"
    assert result.data["bySupplier"][0]["supplierCode"] == "F109"
    assert result.data["bySupplier"][0]["supplierName"] == "上海纳特汽车标准件有限公司"
    assert result.data["bySupplier"][1]["supplierName"] is None, "MDM 无行 ⇒ 名称缺省"
    assert result.data["topLines"][0]["supplierName"] == "上海纳特汽车标准件有限公司"
    assert result.data["topLines"][0]["materialDesc"] == "不锈钢板SUS304"
    assert result.data["topLines"][0]["overdueDays"] == 360
    assert "欠交" in result.answer and "12" in result.answer


@pytest.mark.asyncio
async def test_pending_delivery_handler_rejects_datasource_arg() -> None:
    """THBI 硬规则：调用方给 datasourceId 一律拒绝（不是工具参数）。"""
    from app.domain.exceptions import ValidationError

    mod = _requirePendingModule()
    with pytest.raises(ValidationError):
        await mod.pendingDeliveryHandler(
            None, {"monthsBack": 12, "datasourceId": 7}, SimpleNamespace()
        )


@pytest.mark.asyncio
async def test_pending_delivery_material_name_resolved_via_mdm(monkeypatch) -> None:
    """中文物料名 ⇒ 先经 MDM 解析成编码再进 SQL（不再有数仓字典 LIKE 兜底）。"""
    mod = _requirePendingModule()
    captured: list[str] = []

    import app.services.entity_mapping_service as ems

    class _FakeMapping:
        async def searchMappings(self, session, *, q, entityType, limit):
            assert entityType == "MATERIAL" and q == "不锈钢板"
            return [SimpleNamespace(enterprise_code="AJE0000781")]

    monkeypatch.setattr(ems, "EntityMappingService", _FakeMapping)
    monkeypatch.setattr(mod, "resolveBinding", _fakeBindingResolver())
    monkeypatch.setattr(mod, "_loadActiveDatasource", _fakeLoadDatasource)
    monkeypatch.setattr(mod, "loadMdmNames", _fakeMdmNames)
    monkeypatch.setattr(
        mod, "_adapterFor", lambda dsId, ds: _PendingFakeAdapter(captured)
    )
    mod._PENDING_RESULT_CACHE.clear()

    await mod.pendingDeliveryHandler(
        None, {"monthsBack": 12, "materialKeys": ["不锈钢板"]}, SimpleNamespace()
    )
    lines_sql = next(sql for sql in captured if "ORDER BY OPEN_QTY DESC" in sql.upper())
    assert "O.ITEM_CODE IN ('AJE0000781')" in lines_sql, "MDM 解析出的编码必须进 IN"
    assert "ITMDES" not in lines_sql, "不再有物料描述字典 LIKE 兜底"


@pytest.mark.asyncio
async def test_pending_delivery_same_day_cache(monkeypatch) -> None:
    """同参数同日二次调用 ⇒ 不再查库（复用缓存），换参数 ⇒ 重新查询。"""
    mod = _requirePendingModule()
    calls = {"count": 0}

    class _CountingAdapter(_PendingFakeAdapter):
        async def execute_read_only(self, sql: str):
            calls["count"] += 1
            return await super().execute_read_only(sql)

    monkeypatch.setattr(mod, "resolveBinding", _fakeBindingResolver())
    monkeypatch.setattr(mod, "_loadActiveDatasource", _fakeLoadDatasource)
    monkeypatch.setattr(mod, "loadMdmNames", _fakeMdmNames)
    monkeypatch.setattr(
        mod, "_adapterFor", lambda dsId, ds: _CountingAdapter([])
    )
    mod._PENDING_RESULT_CACHE.clear()

    await mod.pendingDeliveryHandler(None, {"monthsBack": 12}, SimpleNamespace())
    first = calls["count"]
    # 同参数二次调用 ⇒ 命中缓存，不再查库
    await mod.pendingDeliveryHandler(None, {"monthsBack": 12}, SimpleNamespace())
    assert calls["count"] == first, "同参数同日应命中缓存"
    # 换参数 ⇒ 重新查询
    await mod.pendingDeliveryHandler(None, {"monthsBack": 6}, SimpleNamespace())
    assert calls["count"] > first
    mod._PENDING_RESULT_CACHE.clear()


def test_purchase_history_and_quotation_mysql_dialect() -> None:
    """采购历史 / 报价两个工具也要方言安全（MySQL 8.0.31 前无 FETCH FIRST）。"""
    from app.services.agent_tools_procurement import (
        buildPurchaseHistorySql,
        buildQuotationSql,
    )

    history = buildPurchaseHistorySql(
        supplierKey="SUP001", materialKey=None, monthsBack=12, limit=20,
        today=date(2026, 11, 24), dialect="mysql",
    )
    assert "LIMIT 20" in history, "MySQL 行数上限必须用 LIMIT"
    assert "FETCH FIRST" not in history.upper()

    quotation = buildQuotationSql(
        supplierKey="SUP001", materialKey=None, segmentYear=2026, limit=10,
        dialect="mysql",
    )
    assert "LIMIT 10" in quotation
    assert "FETCH FIRST" not in quotation.upper()

    # Oracle 默认不变
    oracleHistory = buildPurchaseHistorySql(
        supplierKey="SUP001", materialKey=None, monthsBack=12, limit=20,
        today=date(2026, 11, 24),
    )
    assert "FETCH FIRST 20 ROWS ONLY" in oracleHistory.upper()
    oracleQuotation = buildQuotationSql(
        supplierKey="SUP001", materialKey=None, segmentYear=2026, limit=10,
    )
    assert "FETCH FIRST 10 ROWS ONLY" in oracleQuotation.upper()


def test_pending_delivery_lines_sql_supports_offset_and_count() -> None:
    """明细页需要分页：lines SQL 带 offset，另有 count SQL 出总数。"""
    mod = _requirePendingModule()

    lines = mod.buildPendingDeliveryLinesSql(
        monthsBack=12, today=date(2026, 11, 24),
        filters=_pendingFilters(), minOverdueDays=None, limit=50, offset=100,
    )
    assert "OFFSET 100" in lines
    assert "FETCH FIRST 50 ROWS ONLY" in lines.upper()

    count = mod.buildPendingDeliveryCountSql(
        monthsBack=12, today=date(2026, 11, 24), filters=_pendingFilters(),
    )
    assert count.strip().upper().startswith("SELECT COUNT(*)")
    assert "O.PO_QTY - COALESCE(R.RECEIVED_QTY, 0) > 0" in count
    # 脏数据阈值同样生效（否则总数与明细对不上）
    assert "1000000" in count


def test_pending_delivery_lines_offset_mysql_dialect() -> None:
    """MySQL 方言：LIMIT n OFFSET m（不是 Oracle 的 FETCH ... OFFSET）。"""
    mod = _requirePendingModule()

    sql = mod.buildPendingDeliveryLinesSql(
        monthsBack=12, today=date(2026, 11, 24),
        filters=_pendingFilters(), minOverdueDays=None, limit=20, offset=40,
        dialect="mysql",
    )
    assert "LIMIT 20 OFFSET 40" in sql
    assert "FETCH FIRST" not in sql.upper()
