"""M3-1 采购只读工具（设计稿 §5.4 首期工具集的第一批）。

- ``material_search``   → entity_mapping MATERIAL 检索（AutoComplete 语义 + trigram）
- ``supplier_search``   → 双路：``materialKey`` ⇒ Neo4j SUPPLIES 反查（图路，
  35 万边）；否则名称/编码走 entity_mapping——两条路**都只读、零 LLM**

与 ``agent_tools_wiki`` 同款分工：handler 引擎在代码、元数据（description /
input_schema / handler_ref）在 DB ``agent_tool_config``（admin 维护）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure import neo4j_client
from app.services.agent_tool_types import AgentToolContext, ToolResult
from app.services.entity_mapping_service import EntityMappingService

_MAX_LIMIT = 50


def _camelRow(row: Any) -> dict[str, Any]:
    """entity_mapping 行 → camelCase dict（与 API 契约对齐）。"""
    return {
        "entityType": row.entity_type,
        "enterpriseKey": row.enterprise_key,
        "enterpriseCode": row.enterprise_code,
        "name": row.name,
        "sourceSystem": row.source_system,
        "sourceCode": row.source_code,
        "matchRule": row.match_rule,
    }


async def materialSearchHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """物料检索：编码/名称/统一键 → 标准物料 + 跨系统映射。"""
    q = str(args.get("q") or "").strip()
    limit = max(1, min(int(args.get("limit") or 10), _MAX_LIMIT))
    if not q:
        return ToolResult(data={"materials": []}, answer="未命中：请提供物料编码或名称关键词。")
    rows = await EntityMappingService().searchMappings(
        session, q=q, entityType="MATERIAL", limit=limit
    )
    materials = [_camelRow(r) for r in rows]
    if not materials:
        return ToolResult(data={"materials": []}, answer=f"未命中关键词「{q}」的物料。")
    head = "、".join(
        f"{m['enterpriseCode']}（{m['name'] or m['enterpriseCode']}）"
        for m in materials[:3]
    )
    return ToolResult(
        data={"materials": materials, "total": len(materials)},
        answer=f"命中 {len(materials)} 个物料：{head}" + ("…" if len(materials) > 3 else ""),
    )


async def supplierSearchHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """供应商检索：materialKey ⇒ 图路（SUPPLIES 反查）；否则名称/编码检索。"""
    materialKey = str(args.get("materialKey") or "").strip()
    q = str(args.get("q") or "").strip()
    limit = max(1, min(int(args.get("limit") or 20), _MAX_LIMIT))

    if materialKey:
        suppliers = await findSuppliersOfMaterial(materialKey, limit)
        if not suppliers:
            return ToolResult(
                data={"suppliers": [], "via": "graph_supplies"},
                answer=f"物料 {materialKey} 在供应关系图中没有已登记的供应商。",
            )
        head = "、".join(f"{s['code']}（{s['name'] or s['code']}）" for s in suppliers[:3])
        return ToolResult(
            data={"suppliers": suppliers, "via": "graph_supplies", "total": len(suppliers)},
            answer=f"物料 {materialKey} 的在册供应商 {len(suppliers)} 家：{head}"
            + ("…" if len(suppliers) > 3 else ""),
        )

    if q:
        rows = await EntityMappingService().searchMappings(
            session, q=q, entityType="SUPPLIER", limit=limit
        )
        suppliers = [_camelRow(r) for r in rows]
        if not suppliers:
            return ToolResult(
                data={"suppliers": [], "via": "entity_mapping"},
                answer=f"未命中关键词「{q}」的供应商。",
            )
        head = "、".join(
            f"{s['enterpriseCode']}（{s['name'] or s['enterpriseCode']}）"
            for s in suppliers[:3]
        )
        return ToolResult(
            data={"suppliers": suppliers, "via": "entity_mapping", "total": len(suppliers)},
            answer=f"命中 {len(suppliers)} 家供应商：{head}" + ("…" if len(suppliers) > 3 else ""),
        )

    return ToolResult(
        data={"suppliers": [], "via": "none"},
        answer="请提供 materialKey（按供应关系查）或 q（按名称/编码查）。",
    )


async def findSuppliersOfMaterial(materialKey: str, limit: int = 20) -> list[dict]:
    """neo4j_client 同名 helper 的薄转发（测试 monkeypatch 点 + 调用侧统一入口）。"""
    return await neo4j_client.findSuppliersOfMaterialAsync(materialKey, limit)
