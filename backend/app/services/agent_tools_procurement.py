"""M3-1 采购只读工具（设计稿 §5.4 首期工具集的第一批）。

- ``material_search``   → entity_mapping MATERIAL 检索（AutoComplete 语义 + trigram）
- ``supplier_search``   → 双路：``materialKey`` ⇒ Neo4j SUPPLIES 反查（图路，
  35 万边）；否则名称/编码走 entity_mapping——两条路**都只读、零 LLM**

与 ``agent_tools_wiki`` 同款分工：handler 引擎在代码、元数据（description /
input_schema / handler_ref）在 DB ``agent_tool_config``（admin 维护）。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError
from app.domain.models import DataSource
from app.infrastructure import neo4j_client
from app.services.agent_tool_types import AgentToolContext, ToolResult
from app.services.entity_mapping_service import EntityMappingService
from app.services.research_sql_runner import assertReadonlySql

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


# ---------------------------------------------------------------------------
# M3-2：业务库只读查询工具（purchase_history_query）
# ---------------------------------------------------------------------------

_HISTORY_TABLE = "DWD_PURCHASE_ORDER_LINE"
_MONTHS_BACK_MIN, _MONTHS_BACK_MAX = 1, 36


def buildPurchaseHistorySql(
    *,
    supplierKey: str,
    materialKey: str | None,
    monthsBack: int,
    limit: int,
    today: date,
) -> str:
    """采购历史明细 SQL 模板（确定性，非 LLM 生成；单语句只读 + 行数上限）。

    列名来自本体映射（class 20 `DWD_PURCHASE_ORDER_LINE`：SUPPLIER_CODE /
    MATERIAL_CODE / PO_NO / ORDER_QTY / UNIT_PRICE / CREATION_DATE）。
    时间窗 = `CREATION_DATE >= today - monthsBack 月`（DATE 字面量，Oracle/PG 通吃）；
    行数上限 `FETCH FIRST`（Oracle 12c+ / PG 通用）。
    """
    monthsBack = max(_MONTHS_BACK_MIN, min(monthsBack, _MONTHS_BACK_MAX))
    limit = max(1, min(limit, _MAX_LIMIT))
    since = _minusMonths(today, monthsBack)
    conds = [f"SUPPLIER_CODE = '{_esc(supplierKey)}'"]
    if materialKey:
        conds.append(f"MATERIAL_CODE = '{_esc(materialKey)}'")
    conds.append(f"CREATION_DATE >= DATE '{since.isoformat()}'")
    where = " AND ".join(conds)
    return (
        f"SELECT SUPPLIER_CODE, PO_NO, MATERIAL_CODE, ORDER_QTY, UNIT_PRICE, "
        f"CREATION_DATE FROM {_HISTORY_TABLE} WHERE {where} "
        f"ORDER BY CREATION_DATE DESC FETCH FIRST {limit} ROWS ONLY"
    )


def _esc(value: str) -> str:
    """字面量转义（单引号翻倍；模板参数只接受标识符级输入）。"""
    return value.replace("'", "''")


def _minusMonths(base: date, months: int) -> date:
    """date - N 月（无 dateutil 依赖；月末夹齐：11-30 减 3 月 = 08-31）。"""
    import calendar

    year = base.year
    month = base.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


async def _resolveBusinessDatasource(
    session: AsyncSession, ctx: AgentToolContext
) -> DataSource:
    """业务数据源解析：ctx 优先（编排/调用方绑定）⇒ 回退默认活跃业务源。

    **C 方案复用**：`ensureDatasourceActive`（停用源 ⇒ 409，fail-closed）。
    """
    ds = getattr(ctx, "datasource", None)
    if ds is None:
        ds = (
            await session.execute(
                select(DataSource)
                .where(DataSource.is_default.is_(True))
                .where(DataSource.is_active.is_(True))
                .limit(1)
            )
        ).scalars().first()
    if ds is None:
        raise ConflictError("无可用业务数据源（默认活跃源缺失）")
    _ensureActive(ds)
    return ds


def _adapterFor(dsId: int, ds: DataSource):
    """业务库 adapter 获取——**惰性导入** business_db_pool。

    保持模块级 import 图不把 business_db_pool 拉进 CI 收集前闭包
    （其模块级 re.compile 会被 import 期钩子守卫 fail-closed，见
    test_ci_conftest_chain 守卫的判定语义）。
    """
    from app.infrastructure.business_db_pool import get_adapter

    return get_adapter(dsId, ds)


def _ensureActive(ds: DataSource) -> None:
    """停用源 fail-closed（C 方案同语义；#164 合并后切共享 ensureDatasourceActive）。

    handler 侧在 resolve 之后再显式调一次（纵深）：monkeypatch/旁路注入的 ds
    也要过这道闸。
    """
    if ds.is_active is False:
        raise ConflictError(f"数据源 {ds.id} 已停用，不可用于业务查询")


def _camelHistoryRow(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "supplierCode": row.get("SUPPLIER_CODE"),
        "poNo": row.get("PO_NO"),
        "materialCode": row.get("MATERIAL_CODE"),
        "orderQty": row.get("ORDER_QTY"),
        "unitPrice": row.get("UNIT_PRICE"),
        "creationDate": str(row.get("CREATION_DATE")) if row.get("CREATION_DATE") else None,
    }


async def purchaseHistoryHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """采购历史查询（业务库只读）：supplierKey 必填；汇总（COUNT/SUM/MIN/MAX/AVG
    价格区间）+ 明细两段确定性 SQL——经 SQL Guard + 业务 adapter，零 LLM。"""
    supplierKey = str(args.get("supplierKey") or "").strip()
    if not supplierKey:
        return ToolResult(
            data={"rows": [], "summary": {}},
            answer="请提供 supplierKey（供应商编码）。",
        )
    materialKey = str(args.get("materialKey") or "").strip() or None
    monthsBack = int(args.get("monthsBack") or 12)
    limit = int(args.get("limit") or 20)
    today = date.today()

    ds = await _resolveBusinessDatasource(session, ctx)
    _ensureActive(ds)  # 纵深：旁路注入的停用源也拦（测试 stub 路径覆盖）
    adapter = _adapterFor(ds.id, ds)
    detailSql = buildPurchaseHistorySql(
        supplierKey=supplierKey, materialKey=materialKey,
        monthsBack=monthsBack, limit=limit, today=today,
    )
    since = _minusMonths(today, monthsBack)
    conds = [f"SUPPLIER_CODE = '{_esc(supplierKey)}'"]
    if materialKey:
        conds.append(f"MATERIAL_CODE = '{_esc(materialKey)}'")
    conds.append(f"CREATION_DATE >= DATE '{since.isoformat()}'")
    summarySql = (
        f"SELECT COUNT(*) AS ORDER_COUNT, SUM(ORDER_QTY) AS TOTAL_QTY, "
        f"MIN(UNIT_PRICE) AS MIN_PRICE, MAX(UNIT_PRICE) AS MAX_PRICE, "
        f"AVG(UNIT_PRICE) AS AVG_PRICE FROM {_HISTORY_TABLE} "
        f"WHERE {' AND '.join(conds)}"
    )
    for sql in (summarySql, detailSql):
        assertReadonlySql(sql)
    summaryRows = await adapter.execute_read_only(summarySql)
    detailRows = await adapter.execute_read_only(detailSql)
    summary = summaryRows[0] if summaryRows else {}
    rows = [_camelHistoryRow(r) for r in detailRows]
    camelSummary = {
        "orderCount": summary.get("ORDER_COUNT"),
        "totalQty": summary.get("TOTAL_QTY"),
        "minPrice": summary.get("MIN_PRICE"),
        "maxPrice": summary.get("MAX_PRICE"),
        "avgPrice": summary.get("AVG_PRICE"),
    }
    return ToolResult(
        data={"via": "business_db", "datasourceId": ds.id, "summary": camelSummary, "rows": rows},
        answer=(
            f"供应商 {supplierKey} 近 {monthsBack} 个月："
            f"{camelSummary['orderCount']} 张订单行、总量 {camelSummary['totalQty']}、"
            f"价格区间 {camelSummary['minPrice']}–{camelSummary['maxPrice']}；"
            f"明细返回 {len(rows)} 行。"
        ),
    )
