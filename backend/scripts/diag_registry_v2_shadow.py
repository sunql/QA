"""P3a D20：影子召回比对（v1 不过滤 vs v2 带治理维度过滤）。

为什么不用自然语言问句
----------------------
本地没有可用的 embedding 服务（8000 端口是 QA System 自身，不是 embedding 服务）
⇒ 无法为问句生成查询向量。本脚本改用 **v1 集合里已有的向量**做查询向量
（自查询：拿样本行自己的向量当查询）。
向量逐字节相同 ⇒ v1 与 v2 的差异**只可能**来自过滤表达式 ⇒ 差异可解释。

判据的由来（2026-11-15 实测，关键）
------------------------------------
**不能拿「v1 的召回」当基准**：v1 集合的 HNSW 是**近似检索**，
而同一段文本在 v1 里被写进了多个 ``ontology_id``（实测 698 组向量逐字节相同），
图遍历陷在重复簇里 ⇒ v1 的近似误差**比过滤本身的影响还大**：

    query 3706（property）
      精确真值（范围筛选后 top5）：3706, 3704, 3705, 3707, 3703
      v1 ANN（不过滤）：             3704, 3707, 3709, 3708, 898   ← 连 d=0 的自己都漏了
      v2 过滤检索：                  3706, 3704, 3705, 3707, 3703   ← 与真值完全一致

所以「差异可解释」的基准必须是**精确真值**：用 numpy 在 v1 向量上暴力算
「先按范围筛、再取 topK」，然后要求 **v2 的过滤检索 == 真值**。

四条硬断言（任一不成立 ⇒ 退出码非 0）
------------------------------------
1. **过滤正确**：v2 过滤检索结果 == 精确真值（范围筛选后 topK）
   （少召回 = `v2Missing`；多召回 = `v2Extra`；两者都必须为 0）。
2. **无未授权泄漏**：带范围的召回里**不得**出现范围外的行，
   也不得出现「未归域」的行（未归域在范围过滤下必须 fail-closed）。
3. **向量未漂移**：v2 行的向量必须与 v1 行逐字节相同
   （回填不重算向量 —— 否则差异里会混入向量漂移）。
4. **差异可解释**：v1 与 v2 的每一条差异都必须落进四个桶之一：
   「未归域被排除 / 归他域被排除 / 基线超窗（ANN 误差） / 基线漏召回（ANN 误差）」。

⚠️ **ontology_id 是「按类型编号」的**，不是全局编号：
class 的 121 与 property 的 121 是两个实体。维度真值表必须按
``(type, ontology_id)`` 建索引，否则会拿别的类型的维度去判本类型的行
（实测会造出假泄漏与假异常）。

用法
----
    MILVUS_URI=http://localhost:19530 \
        uv run python scripts/diag_registry_v2_shadow.py \
            --source-db default --target-db qa_registry_v2 \
            --report-md /tmp/registry_v2_shadow.md
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import numpy as np
from pymilvus import MilvusClient

from app.infrastructure import milvus_client as milvus
from app.infrastructure.milvus_registry_v2 import REGISTRY_V2_COLLECTION_NAMES

V1_COLLECTION_NAMES: dict[str, str] = {
    "class": milvus._CLASS_COLLECTION_NAME,
    "property": milvus._PROPERTY_COLLECTION_NAME,
    "metric": milvus._METRIC_COLLECTION_NAME,
    "kpi": milvus._KPI_COLLECTION_NAME,
}

ALL_TYPES: tuple[str, ...] = ("class", "property", "metric", "kpi")

# 三个可过滤维度
DIMENSION_DOMAIN = "domain"
DIMENSION_ORG = "org"
DIMENSION_ENTITY = "entity"
ALL_DIMENSIONS: tuple[str, ...] = (DIMENSION_DOMAIN, DIMENSION_ORG, DIMENSION_ENTITY)

# 距离容差：Milvus 用 float32 算 L2，numpy 用 float64 ⇒ 末位有几个 1e-7 量级的差
_DISTANCE_TOLERANCE = 1e-4

# 单页读取行数
_READ_PAGE = 1000


# ---------------------------------------------------------------------------
# 纯逻辑（单测覆盖）
# ---------------------------------------------------------------------------


def buildScopeExpr(values: tuple[str, ...]) -> str | None:
    """域范围的过滤表达式（多值域码用 OR 枚举）。

    ⚠️ **不能用** ``not array_contains``（D18 实测：它会命中空数组行 ⇒
    未归域的行反而被放进来）。
    """
    if not values:
        return None
    return " or ".join(
        f'array_contains(domain_codes, "{value}")' for value in values
    )


def buildOrgScopeExpr(values: tuple[str, ...]) -> str | None:
    """组织范围的过滤表达式（结构同域，但打在 ``org_codes`` 上）。"""
    if not values:
        return None
    return " or ".join(
        f'array_contains(org_codes, "{value}")' for value in values
    )


def buildEntityScopeExpr(values: tuple[str, ...]) -> str | None:
    """业务对象范围的过滤表达式（标量字段 ⇒ 用 ``==``）。"""
    if not values:
        return None
    return " or ".join(f'entity_type == "{value}"' for value in values)


def classifyDropped(
    droppedIds: list[int],
    scopeValues: tuple[str, ...],
    dims: dict[int, tuple[str, ...]],
) -> dict[str, int]:
    """把「被过滤掉的行」归因到三类（外加「本该留下」的计数）。

    - ``unmapped``：该行**没有这一维**（未归域）⇒ 归因缺口（本体没归域）；
    - ``otherScope``：该行归了别的范围 ⇒ **预期的策略排除**；
    - ``missingInV2``：v2 里压根没有这一行 ⇒ 数据缺口；
    - ``inScope``：该行在范围内却仍被过滤 ⇒ **异常**（必须为 0）。
    """
    counts = {"inScope": 0, "unmapped": 0, "otherScope": 0, "missingInV2": 0}
    scopeSet = set(scopeValues)
    for ontologyId in droppedIds:
        rowDims = dims.get(ontologyId)
        if rowDims is None:
            counts["missingInV2"] += 1
            continue
        if not rowDims:
            counts["unmapped"] += 1
            continue
        if scopeSet.intersection(rowDims):
            counts["inScope"] += 1
        else:
            counts["otherScope"] += 1
    return counts


def detectLeakage(
    filteredIds: list[int],
    scopeValues: tuple[str, ...],
    dims: dict[int, tuple[str, ...]],
) -> list[int]:
    """找出「带范围过滤却仍然召回」的行 —— 那就是泄漏。

    三种都算泄漏：范围外的行、**未归域**的行（fail-closed 被破坏）、
    v2 里不存在的 id（数据不自洽）。
    """
    leaks: list[int] = []
    scopeSet = set(scopeValues)
    for ontologyId in filteredIds:
        rowDims = dims.get(ontologyId)
        if rowDims is None or not rowDims:
            leaks.append(ontologyId)
            continue
        if not scopeSet.intersection(rowDims):
            leaks.append(ontologyId)
    return leaks


def compareScopeResults(
    truthTopK: list[tuple[int, float]],
    v1Hits: list[tuple[int, float]],
    v2Hits: list[tuple[int, float]],
    scopeValues: tuple[str, ...],
    dims: dict[int, tuple[str, ...]],
    *,
    allDistances: dict[int, float] | None = None,
    tolerance: float = _DISTANCE_TOLERANCE,
) -> dict[str, Any]:
    """比对「精确真值」与「v2 过滤检索」，并把 v1→v2 的差异归因。

    参数
    ----
    ``truthTopK``  精确真值：先按范围筛、再按精确 L2 取前 K ⇒ ``[(id, 距离)]``。
    ``v1Hits``     基线（不过滤，ANN）。
    ``v2Hits``     被测（带范围过滤）。
    ``dims``       该类型的维度真值（``id → (取值...)``）。

    返回
    ----
    - ``v2Missing``：真值里有、v2 却没召回（**异常**，必须为 0）；
    - ``v2Extra``：v2 召回了真值里没有的（**异常**，必须为 0）；
    - ``leaks``：越权召回（**异常**，必须为 0）；
    - ``attribution``：v1→v2 差异的四桶归因；
    - ``baselineMissed``：真值里有、v1 ANN 里没有的（基线近似误差，可解释）。
    """
    truthIds = {i for i, _ in truthTopK}
    v1Ids = {i for i, _ in v1Hits}
    v2Ids = {i for i, _ in v2Hits}
    distances = dict(allDistances or {})
    for ontologyId, distance in truthTopK:
        distances.setdefault(ontologyId, distance)

    truthKth = truthTopK[-1][1] if truthTopK else float("inf")
    v2Kth = v2Hits[-1][1] if v2Hits else float("-inf")

    # ① v2 少召回：真值里的行没进 v2 结果，且它不比 v2 窗口末位差
    v2Missing: list[int] = []
    for ontologyId, distance in truthTopK:
        if ontologyId in v2Ids:
            continue
        closerThanWindow = distance < v2Kth + tolerance
        fewerThanTruth = len(v2Ids) < len(truthIds) and distance <= truthKth + tolerance
        if closerThanWindow or fewerThanTruth:
            v2Missing.append(ontologyId)

    # ② v2 多召回：v2 召回的行不在真值里，且它比真值窗口末位还差
    v2Extra = [
        ontologyId
        for ontologyId, distance in v2Hits
        if ontologyId not in truthIds and distance > truthKth + tolerance
    ]

    leaks = detectLeakage([i for i, _ in v2Hits], scopeValues, dims)

    # ③ v1→v2 差异归因（v1 有、v2 没有的行）
    attribution = {
        "unmapped": 0,
        "otherScope": 0,
        "missingInV2": 0,
        "v2MissingInScope": 0,
        "tieBreak": 0,
        "baselineOver": 0,
    }
    scopeSet = set(scopeValues)
    missingSet = set(v2Missing)
    for ontologyId, _ in v1Hits:
        if ontologyId in v2Ids:
            continue
        rowDims = dims.get(ontologyId)
        if rowDims is None:
            attribution["missingInV2"] += 1
        elif not rowDims:
            attribution["unmapped"] += 1
        elif not scopeSet.intersection(rowDims):
            attribution["otherScope"] += 1
        elif ontologyId in missingSet:
            attribution["v2MissingInScope"] += 1
        elif ontologyId in truthIds:
            attribution["tieBreak"] += 1
        else:
            attribution["baselineOver"] += 1

    # ④ 基线近似误差：真值里有、v1 ANN 里没有的
    baselineMissed = sorted((v2Ids - v1Ids) & truthIds)

    return {
        "v2Missing": sorted(v2Missing),
        "v2Extra": sorted(v2Extra),
        "leaks": leaks,
        "attribution": attribution,
        "baselineMissed": baselineMissed,
    }


# ---------------------------------------------------------------------------
# Milvus 侧
# ---------------------------------------------------------------------------


def _readRows(
    client: MilvusClient,
    collection: str,
    *,
    outputFields: list[str],
) -> list[dict[str, Any]]:
    """读集合全量（分页）。"""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.query(
            collection,
            filter="id >= 0",
            output_fields=outputFields,
            limit=_READ_PAGE,
            offset=offset,
        )
        if not page:
            break
        rows.extend(page)
        offset += len(page)
        if len(page) < _READ_PAGE:
            break
    return rows


def _search(
    client: MilvusClient,
    collection: str,
    vector: list[float],
    *,
    topK: int,
    expr: str | None,
) -> list[tuple[int, float]]:
    """检索并返回 ``[(ontology_id, distance)]``（按距离升序）。"""
    kwargs: dict[str, Any] = {
        "data": [vector],
        "limit": topK,
        "output_fields": ["ontology_id"],
    }
    if expr:
        kwargs["filter"] = expr
    results = client.search(collection, **kwargs)
    hits = results[0] if results else []
    found: list[tuple[int, float]] = []
    for hit in hits:
        entity = hit.get("entity") or {}
        found.append((int(entity.get("ontology_id")), float(hit.get("distance", 0.0))))
    return found


def _scopeExprFor(dimension: str, values: tuple[str, ...]) -> str | None:
    if dimension == DIMENSION_DOMAIN:
        return buildScopeExpr(values)
    if dimension == DIMENSION_ORG:
        return buildOrgScopeExpr(values)
    return buildEntityScopeExpr(values)


def _dimensionOf(row: dict[str, Any], dimension: str) -> tuple[str, ...]:
    """取某一维度的取值（元组；标量维度也包成 1 元组）。"""
    if dimension == DIMENSION_ENTITY:
        value = str(row.get("entity_type") or "")
        return (value,) if value else ()
    return tuple(row.get(dimension + "_codes") or ())


def _exactDistances(
    matrix: np.ndarray,
    ids: list[int],
    queryVector: np.ndarray,
) -> dict[int, float]:
    """暴力精确 L2（Milvus 的 L2 = 平方欧氏距离）⇒ ``{id: 距离}``。"""
    diff = matrix - queryVector
    squared = np.einsum("ij,ij->i", diff, diff)
    return {
        ontologyId: float(distance)
        for ontologyId, distance in zip(ids, squared, strict=True)
    }


def _newScopeResult(dimension: str, value: str) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "value": value,
        "queries": 0,
        "truthRows": 0,
        "baselineHits": 0,
        "filteredHits": 0,
        "v2Missing": 0,
        "v2Extra": 0,
        "baselineMissed": 0,
        "attribution": {
            "unmapped": 0, "otherScope": 0, "missingInV2": 0,
            "v2MissingInScope": 0, "tieBreak": 0, "baselineOver": 0,
        },
        "leaks": [],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> dict[str, Any]:
    """执行影子比对，返回结构化结果。"""
    topK = int(args.top_k)
    samplePerType = int(args.sample_per_type)
    uri = args.uri or milvus._getUri()

    v1Client = MilvusClient(uri=uri, db_name=args.source_db)
    v2Client = MilvusClient(uri=uri, db_name=args.target_db)
    report: dict[str, Any] = {
        "sourceDb": args.source_db,
        "targetDb": args.target_db,
        "topK": topK,
        "coverage": {},
        "vectorDrift": [],
        "scopes": [],
        "leaks": [],
    }

    try:
        # ① 读 v2（带治理维度）→ 维度真值（**按类型分表**）+ 向量
        #    dims[dimension][type][ontology_id] = (值...)
        dims: dict[str, dict[str, dict[int, tuple[str, ...]]]] = {
            dimension: {type_: {} for type_ in ALL_TYPES}
            for dimension in ALL_DIMENSIONS
        }
        scopeValues: dict[str, set[str]] = {
            dimension: set() for dimension in ALL_DIMENSIONS
        }
        matrices: dict[str, np.ndarray] = {}
        idList: dict[str, list[int]] = {}
        typeCoverage: dict[str, dict[str, int]] = {}

        for type_ in ALL_TYPES:
            v2Rows = _readRows(
                v2Client,
                REGISTRY_V2_COLLECTION_NAMES[type_],
                outputFields=[
                    "ontology_id", "name", "domain_codes", "org_codes",
                    "entity_type", "embedding",
                ],
            )
            v1Rows = _readRows(
                v1Client,
                V1_COLLECTION_NAMES[type_],
                outputFields=["ontology_id", "embedding"],
            )
            v2ById = {int(r["ontology_id"]): r for r in v2Rows}
            v1ById: dict[int, list[float]] = {}
            for row in v1Rows:
                ontologyId = int(row["ontology_id"])
                v1ById.setdefault(ontologyId, list(row["embedding"]))

            # ③ 向量未漂移（回填不重算 ⇒ 逐字节相同）
            for ontologyId, row in v2ById.items():
                source = v1ById.get(ontologyId)
                if source is None:
                    report["vectorDrift"].append(
                        {"type": type_, "ontology_id": ontologyId, "reason": "missing_in_v1"}
                    )
                elif source != list(row["embedding"]):
                    report["vectorDrift"].append(
                        {"type": type_, "ontology_id": ontologyId, "reason": "vector_differs"}
                    )

            ids = sorted(v1ById)
            idList[type_] = ids
            matrices[type_] = np.array(
                [v1ById[ontologyId] for ontologyId in ids], dtype=np.float64
            ) if ids else np.zeros((0, 0))

            withDomain = 0
            for ontologyId, row in v2ById.items():
                for dimension in ALL_DIMENSIONS:
                    values = _dimensionOf(row, dimension)
                    dims[dimension][type_][ontologyId] = values
                    scopeValues[dimension].update(values)
                if _dimensionOf(row, DIMENSION_DOMAIN):
                    withDomain += 1
            typeCoverage[type_] = {
                "total": len(v2ById),
                "withDomain": withDomain,
                "withoutDomain": len(v2ById) - withDomain,
                "withOrg": sum(
                    1 for r in v2ById.values() if _dimensionOf(r, DIMENSION_ORG)
                ),
                "withEntity": sum(
                    1 for r in v2ById.values() if _dimensionOf(r, DIMENSION_ENTITY)
                ),
                "v1Rows": len(v1Rows),
                "v1Distinct": len(v1ById),
            }

        total = sum(c["total"] for c in typeCoverage.values())
        withDomain = sum(c["withDomain"] for c in typeCoverage.values())
        report["coverage"] = {
            "total": total,
            "withDomain": withDomain,
            "withoutDomain": total - withDomain,
            "withOrg": sum(c["withOrg"] for c in typeCoverage.values()),
            "withEntity": sum(c["withEntity"] for c in typeCoverage.values()),
            "byType": typeCoverage,
        }

        # ② 逐范围比对（真值 = 精确 L2；v1 = ANN 基线；v2 = 带范围过滤）
        distanceCache: dict[tuple[str, int], dict[int, float]] = {}
        for dimension in ALL_DIMENSIONS:
            for value in sorted(scopeValues[dimension]):
                scopeTuple = (value,)
                expr = _scopeExprFor(dimension, scopeTuple)
                scopeResult = _newScopeResult(dimension, value)
                for type_ in ALL_TYPES:
                    typeDims = dims[dimension][type_]
                    ids = idList[type_]
                    matrix = matrices[type_]
                    if not ids:
                        continue
                    scopeIds = {
                        ontologyId for ontologyId, values in typeDims.items()
                        if value in values
                    }
                    # 查询向量：优先取**自身就在该范围内**的行（=「有权限的查询」）
                    candidates = [
                        ontologyId for ontologyId in ids if ontologyId in scopeIds
                    ]
                    if not candidates:
                        candidates = list(ids)
                    for queryId in candidates[:samplePerType]:
                        cacheKey = (type_, queryId)
                        if cacheKey not in distanceCache:
                            distanceCache[cacheKey] = _exactDistances(
                                matrix, ids, matrix[ids.index(queryId)]
                            )
                        distances = distanceCache[cacheKey]

                        # 精确真值：先按范围筛、再按距离取 topK
                        truthTopK = sorted(
                            (
                                (ontologyId, distances[ontologyId])
                                for ontologyId in scopeIds
                                if ontologyId in distances
                            ),
                            key=lambda item: (item[1], item[0]),
                        )[:topK]

                        baseline = _search(
                            v1Client, V1_COLLECTION_NAMES[type_],
                            matrix[ids.index(queryId)].tolist(),
                            topK=topK, expr=None,
                        )
                        filtered = _search(
                            v2Client, REGISTRY_V2_COLLECTION_NAMES[type_],
                            matrix[ids.index(queryId)].tolist(),
                            topK=topK, expr=expr,
                        )

                        comparison = compareScopeResults(
                            truthTopK, baseline, filtered, scopeTuple, typeDims,
                            allDistances=distances,
                        )
                        scopeResult["queries"] += 1
                        scopeResult["truthRows"] += len(truthTopK)
                        scopeResult["baselineHits"] += len(baseline)
                        scopeResult["filteredHits"] += len(filtered)
                        scopeResult["v2Missing"] += len(comparison["v2Missing"])
                        scopeResult["v2Extra"] += len(comparison["v2Extra"])
                        scopeResult["baselineMissed"] += len(comparison["baselineMissed"])
                        for key, count in comparison["attribution"].items():
                            scopeResult["attribution"][key] += count
                        scopeResult["leaks"].extend(comparison["leaks"])
                scopeResult["leaks"] = sorted(set(scopeResult["leaks"]))
                report["scopes"].append(scopeResult)
                report["leaks"].extend(
                    {"dimension": dimension, "value": value, "ontology_id": leak}
                    for leak in scopeResult["leaks"]
                )
    finally:
        v1Client.close()
        v2Client.close()

    return report


def _renderMarkdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# P3a D20 影子召回比对报告")
    lines.append("")
    lines.append(
        f"> 源库（v1，只读）：`{report['sourceDb']}`；目标库（v2）：`{report['targetDb']}`；"
        f"topK={report['topK']}"
    )
    lines.append("")
    lines.append(
        "> 判据：**v2 的过滤检索 == 精确真值**（numpy 暴力 L2，先按范围筛、再取 topK）。"
    )
    lines.append(
        "> 不拿 v1 的召回当基准 —— v1 是 HNSW 近似检索，"
        "同一段文本被写进多个 `ontology_id`（实测 698 组向量逐字节相同）"
        "⇒ 基线的近似误差比过滤本身的影响还大。"
    )
    lines.append("")

    coverage = report["coverage"]
    lines.append("## 1. 维度覆盖（归因缺口基线）")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| v2 可见行数 | {coverage['total']} |")
    lines.append(f"| 已归域（有 domain_codes） | {coverage['withDomain']} |")
    lines.append(f"| **未归域（归因缺口来源）** | **{coverage['withoutDomain']}** |")
    lines.append(f"| 已挂组织（org_codes） | {coverage['withOrg']} |")
    lines.append(f"| 已挂业务对象（entity_type） | {coverage['withEntity']} |")
    lines.append("")
    lines.append("| 类型 | v2 可见行数 | 已归域 | 未归域 | 已挂组织 | 已挂实体 | v1 物理行数 | v1 去重后 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for type_, counts in coverage["byType"].items():
        lines.append(
            f"| {type_} | {counts['total']} | {counts['withDomain']} | "
            f"{counts['withoutDomain']} | {counts['withOrg']} | {counts['withEntity']} | "
            f"{counts['v1Rows']} | {counts['v1Distinct']} |"
        )
    lines.append("")

    lines.append("## 2. 逐范围比对（真值 vs v2 过滤检索；v1 ANN 作基线参照）")
    lines.append("")
    lines.append(
        "| 维度 | 范围 | 查询数 | 真值行数 | 基线命中 | 过滤后命中 | **v2漏召回** | **v2多召回** | 未归域排除 | 归他域排除 | 基线超窗 | 基线漏召回 | **泄漏** |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for scope in report["scopes"]:
        attribution = scope["attribution"]
        lines.append(
            f"| {scope['dimension']} | {scope['value']} | {scope['queries']} | "
            f"{scope['truthRows']} | {scope['baselineHits']} | {scope['filteredHits']} | "
            f"**{scope['v2Missing']}** | **{scope['v2Extra']}** | "
            f"{attribution['unmapped']} | {attribution['otherScope']} | "
            f"{attribution['baselineOver']} | {scope['baselineMissed']} | "
            f"**{len(scope['leaks'])}** |"
        )
    lines.append("")
    lines.append(
        "- 「未归域排除 / 归他域排除」= 范围过滤**按预期**排除的行；"
    )
    lines.append(
        "- 「基线超窗 / 基线漏召回」= **基线 ANN 的近似误差**（v1 集合里同一段文本有多个 "
        "`ontology_id`，HNSW 图遍历陷在重复簇里）⇒ 与过滤无关；"
    )
    lines.append(
        "- 「v2漏召回 / v2多召回 / 泄漏」= **异常**，必须为 0。"
    )
    lines.append("")

    lines.append("## 3. 硬断言")
    lines.append("")
    drift = report["vectorDrift"]
    leaks = report["leaks"]
    v2Missing = sum(scope["v2Missing"] for scope in report["scopes"])
    v2Extra = sum(scope["v2Extra"] for scope in report["scopes"])
    lines.append(
        f"- 过滤正确（v2 == 精确真值）："
        f"{'✅ 通过' if (v2Missing == 0 and v2Extra == 0) else f'❌ 漏召回 {v2Missing} / 多召回 {v2Extra}'}"
        "（少召回 = 该召回没召回；多召回 = 不该召回却召回了）"
    )
    lines.append(
        f"- 无未授权泄漏：{'✅ 通过' if not leaks else f'❌ {len(leaks)} 条越权召回'}"
        "（带范围过滤的召回里没有范围外的行，也没有未归域的行）"
    )
    lines.append(
        f"- 向量未漂移：{'✅ 通过' if not drift else f'❌ {len(drift)} 条不一致'}"
        "（v2 向量与 v1 逐字节相同 ⇒ 回填没有重算向量）"
    )
    lines.append("")
    return "\n".join(lines)


def _parseArgs(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P3a D20 影子召回比对")
    parser.add_argument("--uri", default="", help="Milvus URI（缺省取应用配置 MILVUS_URI）")
    parser.add_argument("--source-db", required=True, help="v1 所在库（只读）")
    parser.add_argument("--target-db", required=True, help="v2 所在库")
    parser.add_argument("--top-k", type=int, default=5, help="每次检索的 topK")
    parser.add_argument("--sample-per-type", type=int, default=5, help="每类型每范围的样本查询数")
    parser.add_argument("--report-md", default="", help="markdown 报告输出路径")
    parser.add_argument("--report-json", default="", help="JSON 报告输出路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parseArgs(list(sys.argv[1:] if argv is None else argv))
    report = run(args)

    markdown = _renderMarkdown(report)
    print(markdown)
    if args.report_md:
        with open(args.report_md, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        print(f"markdown 报告：{args.report_md}")
    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(f"JSON 报告：{args.report_json}")

    v2Missing = sum(scope["v2Missing"] for scope in report["scopes"])
    v2Extra = sum(scope["v2Extra"] for scope in report["scopes"])
    if report["leaks"] or report["vectorDrift"] or v2Missing or v2Extra:
        print(
            "❌ 硬断言未通过："
            f"泄漏 {len(report['leaks'])} 条 / 向量漂移 {len(report['vectorDrift'])} 条 / "
            f"v2 漏召回 {v2Missing} 条 / v2 多召回 {v2Extra} 条",
            file=sys.stderr,
        )
        return 1
    print("✅ 硬断言全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
