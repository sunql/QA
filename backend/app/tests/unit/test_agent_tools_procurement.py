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
