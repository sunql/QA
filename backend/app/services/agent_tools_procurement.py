"""M3-1 采购只读工具（设计稿 §5.4 首期工具集的第一批）。

- ``material_search``   → entity_mapping MATERIAL 检索（AutoComplete 语义 + trigram）
- ``supplier_search``   → 双路：``materialKey`` ⇒ Neo4j SUPPLIES 反查（图路，
  35 万边）；否则名称/编码走 entity_mapping——两条路**都只读、零 LLM**

与 ``agent_tools_wiki`` 同款分工：handler 引擎在代码、元数据（description /
input_schema / handler_ref）在 DB ``agent_tool_config``（admin 维护）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError
from app.domain.models import DataSource
from app.infrastructure import neo4j_client
from app.services.agent_tool_types import AgentToolContext, ToolResult
from app.services.entity_mapping_service import EntityMappingService
from app.services.knowledge.document_search import searchProcurementDocuments
from app.services.research_sql_runner import assertReadonlySql

_MAX_LIMIT = 50


def _col(row: Any, name: str) -> Any:
    """**大小写不敏感**地取列值。

    ⚠️ 为什么必须这样：Oracle 的 ``cursor.description`` 返回大写标识符，而
    ``business_db_pool`` 统一把它们**小写化**（见该文件 :698）⇒ 用大写 key
    去 get **永远拿到 None**。

    2026-11-22 实测踩到：``quotation_query`` / ``purchase_history`` 两个
    工具从 Oracle 取到行后，``row.get("SUPPLIER_CODE")`` 全是 None ⇒
    **它们在生产上从没返回过真实数据**（一直返回"命中 0 条"）。
    """
    if not isinstance(row, dict):
        return None
    if name in row:
        return row[name]
    lowered = name.lower()
    for key, value in row.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return None


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
            # 名称问法兜底：用户问的是「哪个供应商能供 <物料名>」时，关键词是**物料名**，
            # 按 SUPPLIER 检索必然 0 命中。旧行为把缺口甩回给用户（"请提供物料编码"），
            # 2026-11-21 实测「FE-5顶丝M8×1×15」即为此类：物料名可解析到编码，
            # 图里也有 4 家在册供应商 ⇒ 名称→编码→供应关系是工具该自己走的一步。
            resolved, materialMatches = await _suppliersByMaterialName(
                session, q, limit
            )
            if resolved:
                head = "、".join(
                    f"{s['code']}（{s['name'] or s['code']}）" for s in resolved[:3]
                )
                materialText = "、".join(
                    f"{m['code']}（{m['name']}）" for m in materialMatches[:3]
                )
                return ToolResult(
                    data={
                        "suppliers": resolved,
                        "via": "material_name",
                        "total": len(resolved),
                        "materials": materialMatches,
                    },
                    answer=(
                        f"关键词「{q}」解析到物料 {materialText}；"
                        f"这些物料在供应关系图中的在册供应商 {len(resolved)} 家：{head}"
                        + ("…" if len(resolved) > 3 else "")
                    ),
                )
            if materialMatches:
                materialText = "、".join(
                    f"{m['code']}（{m['name']}）" for m in materialMatches[:3]
                )
                return ToolResult(
                    data={
                        "suppliers": [],
                        "via": "material_name",
                        "materials": materialMatches,
                    },
                    answer=(
                        f"关键词「{q}」解析到物料 {materialText}，"
                        "但供应关系图里没有登记这些物料的供应商（可换用物料编码再查，"
                        "或该物料确实尚无合格供应商）。"
                    ),
                )
            return ToolResult(
                data={"suppliers": [], "via": "entity_mapping"},
                answer=f"未命中关键词「{q}」的供应商或物料。",
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


#: 物料名解析上限：一个名称关键词可能命中多个规格/料号。
_MATERIAL_NAME_RESOLVE_LIMIT = 5


async def _suppliersByMaterialName(
    session: AsyncSession, q: str, limit: int
) -> tuple[list[dict], list[dict]]:
    """物料名关键词 → 物料编码 → 供应关系图反查。

    返回 ``(去重后的供应商, 命中的物料摘要)``。物料摘要即使无供应商也返回
    ——「物料存在但没有在册供应商」和「物料都搜不到」是两种不同的结论。
    """
    rows = await EntityMappingService().searchMappings(
        session, q=q, entityType="MATERIAL", limit=_MATERIAL_NAME_RESOLVE_LIMIT
    )
    materials: list[dict] = []
    suppliersByKey: dict[str, dict] = {}
    for row in rows:
        key = str(
            getattr(row, "source_key", None)
            or getattr(row, "enterprise_code", None)
            or getattr(row, "enterprise_key", "")
        )
        if not key:
            continue
        found = await findSuppliersOfMaterial(key, limit)
        materials.append(
            {
                "key": key,
                "code": getattr(row, "enterprise_code", None) or key,
                "name": getattr(row, "name", None) or "",
                "supplierCount": len(found),
            }
        )
        for supplier in found:
            suppliersByKey.setdefault(str(supplier.get("key")), supplier)
    return list(suppliersByKey.values()), materials


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
    """停用源 fail-closed——**C 方案已进 main ⇒ 切共享 helper**（纵深二次过闸保留）。

    handler 侧在 resolve 之后再显式调一次（纵深）：monkeypatch/旁路注入的 ds
    也要过这道闸。
    """
    from app.services.datasource_service import ensureDatasourceActive

    ensureDatasourceActive(ds)


def _camelHistoryRow(row: dict[str, Any]) -> dict[str, Any]:
    """Oracle 行 → camelCase（列名大小写不敏感，见 ``_col``）。"""
    creationDate = _col(row, "CREATION_DATE")
    return {
        "supplierCode": _col(row, "SUPPLIER_CODE"),
        "poNo": _col(row, "PO_NO"),
        "materialCode": _col(row, "MATERIAL_CODE"),
        "orderQty": _col(row, "ORDER_QTY"),
        "unitPrice": _col(row, "UNIT_PRICE"),
        "creationDate": str(creationDate) if creationDate else None,
    }


def buildQuotationSql(
    *,
    supplierKey: str | None,
    materialKey: str | None,
    segmentYear: int | None,
    limit: int,
) -> str:
    """年度报价明细 SQL 模板（确定性）。

    落点 = `DWD_PURCHASE_QUOTATION_YEARLY_DTL`（源侧实测 167,909 行，
    ITEM_CODE/SUPPLIER_CODE/SEG_YEAR/UNIT_PRICE_EX_TAX 100% 填充）；
    `DWD_SUPPLIER_PRICE_LIST` 的 MATERIAL_CODE 全 NULL（source-capability.md:35）
    ⇒ 不可用——换表依据。
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    conds: list[str] = []
    if supplierKey:
        conds.append(f"SUPPLIER_CODE = '{_esc(supplierKey)}'")
    if materialKey:
        conds.append(f"ITEM_CODE = '{_esc(materialKey)}'")
    if segmentYear:
        conds.append(f"SEG_YEAR = {int(segmentYear)}")
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    # CURRENCY_CODE / UOM / PRICE_LIST_CODE 一并取：**比价时要按报价单分
    # （含税/不含税）、按单位分组**（2026-11-22 实测得出）。
    return (
        f"SELECT SUPPLIER_CODE, ITEM_CODE, SEG_YEAR, UNIT_PRICE_EX_TAX, "
        f"SEG_START_DATE, SEG_END_DATE, CURRENCY_CODE, UOM, PRICE_LIST_CODE "
        f"FROM DWD_PURCHASE_QUOTATION_YEARLY_DTL"
        f"{where} ORDER BY SEG_YEAR DESC, ITEM_CODE "
        f"FETCH FIRST {limit} ROWS ONLY"
    )


def _camelQuotationRow(row: dict[str, Any]) -> dict[str, Any]:
    """Oracle 行 → camelCase（列名大小写不敏感，见 ``_col``）。"""
    segmentStart = _col(row, "SEG_START_DATE")
    segmentEnd = _col(row, "SEG_END_DATE")
    return {
        "supplierCode": _col(row, "SUPPLIER_CODE"),
        "itemCode": _col(row, "ITEM_CODE"),
        "segmentYear": _col(row, "SEG_YEAR"),
        "unitPriceExTax": _col(row, "UNIT_PRICE_EX_TAX"),
        # 真数据里还有币种与单位（2026-11-22 实测：DWD_PURCHASE_QUOTATION_YEARLY_DTL
        # 有 currency_code=CNY / uom=UN）——带上，比价时才能判「同币种同单位」
        "currency": _col(row, "CURRENCY_CODE"),
        "uom": _col(row, "UOM"),
        "segmentStart": str(segmentStart) if segmentStart else None,
        "segmentEnd": str(segmentEnd) if segmentEnd else None,
    }


async def quotationQueryHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """报价查询（业务库只读）：supplierKey / materialKey / segmentYear 任一必填。"""
    supplierKey = str(args.get("supplierKey") or "").strip() or None
    materialKey = str(args.get("materialKey") or "").strip() or None
    segmentYear = args.get("segmentYear")
    limit = int(args.get("limit") or 20)
    if not (supplierKey or materialKey or segmentYear):
        return ToolResult(
            data={"rows": []},
            answer="请提供 supplierKey / materialKey / segmentYear 任一。",
        )
    ds = await _resolveBusinessDatasource(session, ctx)
    _ensureActive(ds)
    adapter = _adapterFor(ds.id, ds)
    sql = buildQuotationSql(
        supplierKey=supplierKey, materialKey=materialKey,
        segmentYear=segmentYear, limit=limit,
    )
    assertReadonlySql(sql)
    rows = [_camelQuotationRow(r) for r in await adapter.execute_read_only(sql)]
    if not rows:
        return ToolResult(
            data={"via": "business_db", "rows": []},
            answer="未命中报价明细（检查供应商/物料编码或年度）。",
        )
    head = "、".join(f"{r['itemCode']}@{r['segmentYear']}={r['unitPriceExTax']}" for r in rows[:3])
    return ToolResult(
        data={"via": "business_db", "datasourceId": ds.id, "rows": rows, "total": len(rows)},
        answer=f"命中 {len(rows)} 条报价段：{head}" + ("…" if len(rows) > 3 else ""),
    )


async def policyComplianceHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """`policy_compliance_check`：把一条**结论/做法**拿去比对企业制度文档。

    P3 的用途（见评估文档）：Copilot 给出"数据结论"后，用本工具回答
    「这么做合不合规 / 制度怎么说」，从而给出三段式：
    **数据值 + 文档依据 + 是否一致**。

    密级闸 fail-closed：ctx.user 缺失（且无法按 actor 还原）⇒ 不检索。
    """
    claim = str(args.get("claim") or args.get("question") or "").strip()
    if not claim:
        return ToolResult(
            data={"evidence": []}, answer="请提供要核对的结论或做法（claim）。"
        )
    user = await _resolveToolUser(session, ctx)
    if user is None:
        return ToolResult(
            data={"evidence": []},
            answer="无法核验文档密级（缺少用户身份）⇒ 不执行制度核对；请经带身份的入口使用。",
        )
    limit = int(args.get("topK") or args.get("limit") or 3)
    evidence = await searchProcurementDocuments(
        session, user, query=claim, topK=max(1, min(limit, 10))
    )
    if not evidence:
        return ToolResult(
            data={"evidence": [], "compliant": None},
            answer=f"未检索到与「{claim}」相关的制度条款，无法判定合规性（不要臆测）。",
        )
    payload = [e.model_dump(mode="json", by_alias=True) for e in evidence]
    related = sorted(
        {
            f"{entity.type}:{entity.code}"
            for item in evidence
            for entity in getattr(item, "related_entities", ())
        }
    )
    return ToolResult(
        data={"evidence": payload, "relatedEntities": related, "compliant": None},
        answer=(
            f"找到 {len(evidence)} 条相关制度条款，请据此说明「制度怎么说、"
            f"与当前结论是否一致」；若两者冲突，以**查询数据**为准并指出冲突。"
        ),
    )


async def _resolveToolUser(session: AsyncSession, ctx: AgentToolContext) -> Any | None:
    """工具侧用户身份：优先 ctx.user；否则按 ``ctx.actor``（用户名）还原。

    编排运行的 ctx 只带 actor（run.created_by）⇒ 文档类工具若不还原身份，
    密级闸永远 fail-closed。查不到身份仍返回 None（继续 fail-closed）。
    """
    user = getattr(ctx, "user", None)
    if user is not None:
        return user
    actor = getattr(ctx, "actor", None)
    if not actor or session is None:
        return None
    from app.dependencies import buildCurrentUserForUserId

    return await buildCurrentUserForUserId(session, actor)


#: 比价一次最多几个物料（IN 列表有上限，避免把整个年度报价拉回来）。
_COMPARE_MAX_ITEMS = 20


def buildQuotationCompareSql(itemCodes: list[str], segmentYear: int) -> str:
    """多物料年度报价 SQL（确定性）：一次取齐同年度、指定物料的全部供应商报价。"""
    items = [code for code in dict.fromkeys(itemCodes) if code][:_COMPARE_MAX_ITEMS]
    quoted = ", ".join(f"'{_esc(code)}'" for code in items)
    # PRICE_LIST_CODE / UOM / CURRENCY_CODE 必须取：比价要按报价单分（含税/
    # 不含税不可混比）、按单位分组（2026-11-22 实测得出）。
    return (
        f"SELECT SUPPLIER_CODE, ITEM_CODE, SEG_YEAR, UNIT_PRICE_EX_TAX, "
        f"PRICE_LIST_CODE, UOM, CURRENCY_CODE "
        f"FROM DWD_PURCHASE_QUOTATION_YEARLY_DTL "
        f"WHERE SEG_YEAR = {int(segmentYear)} AND ITEM_CODE IN ({quoted}) "
        f"ORDER BY ITEM_CODE, SUPPLIER_CODE"
    )


async def quotationCompareHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """`quotation_compare`：年度报价的确定性比价矩阵（零 LLM）。

    口径见 `services/procurement_quotation_compare` 模块文档：
    CNY / 未税 / 同物料同年度横向比价 / 相对中位价 20%·30% 阈值。
    """
    from app.services.procurement_quotation_compare import (
        DEFAULT_HIGH_THRESHOLD,
        DEFAULT_LOW_THRESHOLD,
        buildComparison,
        quotesFromRows,
        toDecimal,
    )

    rawItems = args.get("itemKeys") or []
    if isinstance(rawItems, str):
        rawItems = [rawItems]
    items = [str(code).strip() for code in rawItems if str(code).strip()]
    if not items:
        return ToolResult(data={"groups": []}, answer="请提供 itemKeys（物料编码列表）。")
    segmentYear = args.get("segmentYear")
    if segmentYear is None:
        return ToolResult(
            data={"groups": []},
            answer="请提供 segmentYear（报价年度，例如 2026）。",
        )
    low = toDecimal(args.get("lowThresholdPct")) or DEFAULT_LOW_THRESHOLD
    high = toDecimal(args.get("highThresholdPct")) or DEFAULT_HIGH_THRESHOLD

    ds = await _resolveBusinessDatasource(session, ctx)
    _ensureActive(ds)
    adapter = _adapterFor(ds.id, ds)
    sql = buildQuotationCompareSql(items, int(segmentYear))
    assertReadonlySql(sql)
    rows = await adapter.execute_read_only(sql)

    groups = buildComparison(
        quotesFromRows(rows), lowThreshold=low, highThreshold=high
    )
    if not groups:
        return ToolResult(
            data={"groups": [], "currency": "CNY", "priceBasis": "EX_TAX"},
            answer=f"{segmentYear} 年度没有这些物料的报价记录：{'、'.join(items[:5])}…",
        )
    payload = [
        {
            "itemCode": group.itemCode,
            "segmentYear": group.segmentYear,
            "currency": group.currency,
            "priceBasis": group.priceBasis,
            # 分组口径必须随结果出去（前端要能告诉用户「这是在哪个报价单、
            # 哪个单位口径下比的」）
            "priceListCode": group.priceListCode,
            "uom": group.uom,
            "lowestPrice": str(group.lowestPrice),
            "medianPrice": str(group.medianPrice),
            "spreadPct": str(group.spreadPct),
            "bestSupplierCode": group.bestSupplierCode,
            "rows": [
                {
                    "supplierCode": row.supplierCode,
                    "unitPriceExTax": str(row.unitPriceExTax),
                    "deltaVsMinPct": str(row.deltaVsMinPct),
                    "deltaVsMedianPct": str(row.deltaVsMedianPct),
                    "flag": row.flag,
                }
                for row in group.rows
            ],
        }
        for group in groups
    ]
    cheapest = "、".join(f"{g.itemCode}@{g.bestSupplierCode}" for g in groups[:3])
    return ToolResult(
        data={"groups": payload, "currency": "CNY", "priceBasis": "EX_TAX"},
        answer=f"{segmentYear} 年度完成 {len(groups)} 个物料的比价；最低价：{cheapest}。",
    )


async def procurementPolicySearchHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """采购制度检索（M2-3 文档检索接工具）。

    **密级闸 fail-closed**：ctx.user 缺失 ⇒ 不检索（文档检索无密级核验 = 越权面）。
    """
    question = str(args.get("question") or "").strip()
    if not question:
        return ToolResult(data={"evidence": []}, answer="请提供要检索的制度问题。")
    user = await _resolveToolUser(session, ctx)
    if user is None:
        return ToolResult(
            data={"evidence": []},
            answer="无法核验文档密级（缺少用户身份）⇒ 不执行制度检索；请经带身份的入口使用。",
        )
    limit = int(args.get("limit") or 5)
    evidence = await searchProcurementDocuments(
        session, user, query=question, topK=max(1, min(limit, 20))
    )
    if not evidence:
        return ToolResult(
            data={"evidence": []},
            answer=f"未命中与「{question}」相关的制度文档。",
        )
    return ToolResult(
        data={
            "evidence": [e.model_dump(mode="json", by_alias=True) for e in evidence],
            "total": len(evidence),
        },
        answer=f"命中 {len(evidence)} 条制度证据（含密级与生效期溯源）。",
    )


#: 合同类文档的类型集合（工具语义「合同」= 文档目录里的 CONTRACT）。
CONTRACT_DOCUMENT_TYPES = ("CONTRACT",)


async def contractDocumentSearchHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """合同文档检索（设计稿 §5.2 `contract_document_search`）。

    与 `procurement_policy_search` 同一条文档检索链（RAG + 密级闸 + catalog
    核验），只是把 `documentTypes` 收窄到 **CONTRACT**。密级闸同样
    **fail-closed**：ctx.user 缺失就不检索。
    """
    question = str(args.get("question") or "").strip()
    if not question:
        return ToolResult(data={"evidence": []}, answer="请提供要检索的合同问题。")
    user = await _resolveToolUser(session, ctx)
    if user is None:
        return ToolResult(
            data={"evidence": []},
            answer="无法核验文档密级（缺少用户身份）⇒ 不执行合同检索；请经带身份的入口使用。",
        )
    limit = int(args.get("topK") or args.get("limit") or 5)
    evidence = await searchProcurementDocuments(
        session,
        user,
        query=question,
        documentTypes=list(CONTRACT_DOCUMENT_TYPES),
        topK=max(1, min(limit, 20)),
    )
    if not evidence:
        return ToolResult(
            data={"evidence": [], "documentTypes": list(CONTRACT_DOCUMENT_TYPES)},
            answer=f"未命中与「{question}」相关的合同文档（已按 CONTRACT 类型过滤）。",
        )
    return ToolResult(
        data={
            "evidence": [e.model_dump(mode="json", by_alias=True) for e in evidence],
            "total": len(evidence),
            "documentTypes": list(CONTRACT_DOCUMENT_TYPES),
        },
        answer=f"命中 {len(evidence)} 条合同证据（含密级与生效期溯源）。",
    )


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
        "orderCount": _col(summary, "ORDER_COUNT"),
        "totalQty": _col(summary, "TOTAL_QTY"),
        "minPrice": _col(summary, "MIN_PRICE"),
        "maxPrice": _col(summary, "MAX_PRICE"),
        "avgPrice": _col(summary, "AVG_PRICE"),
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
