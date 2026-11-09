"""Manifest 的指标 / KPI / 业务对象 / 数据域分区（P1a / D5-D6）。

与 `semantic_compiler.py` 的分工：那边编译本体核心（类/属性/JOIN），这里编译
指标两层（技术口径 `metrics` + 业务 `kpis`）以及 BO/域。共享原语见
`semantic_manifest_common.py`。
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Sequence
from typing import Any

from app.domain.models import (
    BusinessObject,
    BusinessObjectDomain,
    DataDomain,
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyMetricSource,
)
from app.services.semantic_manifest_common import (
    asList,
    requireClass,
    resolveActiveName,
    sortBy,
)

# ---------------------------------------------------------------------------
# 指标（两层：技术口径 + 业务 KPI）
# ---------------------------------------------------------------------------


def groupKpiCodesByMetricId(kpis: Sequence[KpiCatalog]) -> dict[int, list[str]]:
    grouped: dict[int, list[str]] = {}
    for row in kpis:
        if row.metric_id is not None:
            grouped.setdefault(row.metric_id, []).append(row.kpi_code)
    return {metricId: sorted(codes) for metricId, codes in grouped.items()}


def compileMetrics(
    metrics: Sequence[OntologyMetric],
    sources: Iterable[OntologyMetricSource],
    allClassById: dict[int, OntologyClass],
    activeNameById: dict[int, str],
    kpiCodesByMetricId: dict[int, list[str]],
) -> list[dict[str, Any]]:
    sourcesByMetricId = groupMetricSources(sources, metrics, allClassById, activeNameById)
    compiled = [
        compileMetric(
            row,
            allClassById,
            sourcesByMetricId.get(row.id, ()),
            kpiCodesByMetricId.get(row.id, []),
        )
        for row in metrics
    ]
    return sortBy(compiled, key=lambda item: item["metricName"])


def groupMetricSources(
    sources: Iterable[OntologyMetricSource],
    metrics: Sequence[OntologyMetric],
    allClassById: dict[int, OntologyClass],
    activeNameById: dict[int, str],
) -> dict[int, list[dict[str, str]]]:
    """按 metric 分组；指向墓碑类的源整体丢弃（类已不在活跃模型中）。"""
    metricIds = {row.id for row in metrics}
    grouped: dict[int, list[dict[str, str]]] = {}
    for row in sources:
        if row.metric_id not in metricIds:
            raise ValueError(
                f"本体数据异常：ontology_metric_source metric_id={row.metric_id} "
                "不存在于 ontology_metric"
            )
        owner = requireClass(row.class_id, allClassById, "class_id")
        if owner.valid_to is not None:
            continue
        grouped.setdefault(row.metric_id, []).append(
            {"className": activeNameById[owner.id], "role": row.role}
        )
    return {
        metricId: sorted(items, key=lambda item: (item["role"], item["className"]))
        for metricId, items in grouped.items()
    }


# `MeasureSpecSchema` 的 snake_case 键在 Manifest 里统一成 camelCase。
_MEASURE_KEY_RENAMES = {"class_alias": "classAlias"}


def compileMetric(
    row: OntologyMetric,
    allClassById: dict[int, OntologyClass],
    sources: Sequence[dict[str, str]],
    kpiCodes: list[str],
) -> dict[str, Any]:
    return {
        "metricName": row.metric_name,
        "metricAlias": row.metric_alias,
        "formula": row.formula,
        "aggFunction": row.agg_function,
        "metricType": row.metric_type,
        "targetClassName": resolveActiveName(
            row.target_class_id, allClassById, "target_class_id"
        ),
        # 必须复制：与 measure 同理，直接把 ORM 行上的 JSON 放进快照会让下游
        # 任何规范化写污染会话里的实体（不可变约束）。
        "dimensionDefaults": copy.deepcopy(row.dimension_defaults),
        "grainKeys": asList(row.grain_keys),
        "aggregationOrder": row.aggregation_order,
        # 结构化轨道（P1 指标技术形态）：分子/分母度量整份入档，只把内嵌的
        # 自增 class_id 换成 className（见 resolveMeasure）。
        "numeratorMeasure": resolveMeasure(
            row.numerator_measure, allClassById, "numerator_measure"
        ),
        "denominatorMeasure": resolveMeasure(
            row.denominator_measure, allClassById, "denominator_measure"
        ),
        "filterExpression": row.filter_expression,
        "status": row.status,
        "owner": row.owner,
        "executionSource": row.execution_source,
        "sources": list(sources),
        "kpiCodes": list(kpiCodes),
    }


def resolveMeasure(
    measure: dict[str, Any] | None,
    allClassById: dict[int, OntologyClass],
    fieldName: str,
) -> dict[str, Any] | None:
    """度量定义整份保留，但把内嵌 `class_id` 归一成 `className`。

    `MeasureSpecSchema` 存的是自增主键（JSON 内嵌引用，DB 加不了外键）。Manifest
    是自包含快照：带上实例相关 id 会破坏「同样治理内容 ⇒ 同样字节」，回放时还会
    在新环境里解析到另一张表。归一规则与 `refClassName` 一致 —— 墓碑类 → null，
    完全不存在的 id → 抛错。

    返回**新字典**：绝不原地改 ORM 行上的 JSON（那会污染会话身份映射）。
    """
    if measure is None:
        return None
    if not isinstance(measure, dict):
        raise ValueError(
            f"本体数据异常：{fieldName} 必须是对象，实际是 {type(measure).__name__}"
        )
    normalized = dict(measure)
    rawClassId = normalized.pop("class_id", None)
    for snakeKey, camelKey in _MEASURE_KEY_RENAMES.items():
        if snakeKey not in normalized:
            continue
        if camelKey in normalized:
            raise ValueError(
                f"本体数据异常：{fieldName} 同时存在 {snakeKey!r} 与 {camelKey!r}"
            )
        normalized[camelKey] = normalized.pop(snakeKey)
    if "className" in normalized:
        raise ValueError(f"本体数据异常：{fieldName} 不应自带 className")
    normalized["className"] = resolveActiveName(
        rawClassId, allClassById, f"{fieldName}.class_id"
    )
    return normalized


def compileKpis(
    kpis: Sequence[KpiCatalog],
    metricNameById: dict[int, str],
    datasourceNameById: dict[int, str],
) -> list[dict[str, Any]]:
    compiled = [
        compileKpi(row, metricNameById, datasourceNameById) for row in kpis
    ]
    return sortBy(compiled, key=lambda item: item["kpiCode"])


def compileKpi(
    row: KpiCatalog,
    metricNameById: dict[int, str],
    datasourceNameById: dict[int, str],
) -> dict[str, Any]:
    # 弱关联：KPI 可先于技术指标存在，也可能尚未绑定数据源。
    metricName = None
    if row.metric_id is not None:
        metricName = metricNameById.get(row.metric_id)
        if metricName is None:
            raise ValueError(
                f"本体数据异常：kpi_catalog.metric_id={row.metric_id} 不存在于 ontology_metric"
            )
    datasourceName = None
    if row.datasource_id is not None:
        datasourceName = datasourceNameById.get(row.datasource_id)
        if datasourceName is None:
            raise ValueError(
                f"本体数据异常：kpi_catalog.datasource_id={row.datasource_id} "
                "不存在于 data_source"
            )
    # 刻意不含 match_threshold：Numeric/Decimal 无法被 stdlib json 序列化，
    # 且它属检索调参，不是语义定义。
    return {
        "kpiCode": row.kpi_code,
        "kpiName": row.kpi_name,
        "businessDefinition": row.business_definition,
        "formula": row.formula,
        "numerator": row.numerator,
        "denominator": row.denominator,
        "grain": row.grain,
        "unit": row.unit,
        "grainKeys": asList(row.grain_keys),
        "numeratorColumns": asList(row.numerator_columns),
        "denominatorColumns": asList(row.denominator_columns),
        "owner": row.owner,
        "version": row.version,
        "status": row.status,
        "semanticKeywords": asList(row.semantic_keywords),
        "metricName": metricName,
        # 只记名字：`datasource_id` 是自增主键，入档会让跨环境字节不一致。
        "datasourceName": datasourceName,
    }


# ---------------------------------------------------------------------------
# 业务对象 / 数据域
# ---------------------------------------------------------------------------


def compileDomains(domains: Iterable[DataDomain]) -> list[dict[str, Any]]:
    """域词表整份入档（含 enabled 标志）：运行时据此过滤，不必回查 DB。"""
    compiled = [
        {
            "code": row.code,
            "name": row.name,
            "description": row.description,
            "enabled": bool(row.enabled),
        }
        for row in domains
    ]
    return sortBy(compiled, key=lambda item: item["code"])


def compileBusinessObjects(
    businessObjects: Iterable[BusinessObject],
    attached: Iterable[BusinessObjectDomain],
    allClassById: dict[int, OntologyClass],
    activeNameById: dict[int, str],
    domainCodes: set[str],
) -> list[dict[str, Any]]:
    allBos = list(businessObjects)
    boCodes = {row.code for row in allBos}
    attachedByBo = groupAttachedDomains(attached, boCodes, domainCodes)
    compiled = [
        compileBusinessObject(
            row, allClassById, activeNameById, domainCodes, attachedByBo.get(row.code, [])
        )
        for row in allBos
    ]
    return sortBy(compiled, key=lambda item: item["code"])


def groupAttachedDomains(
    attached: Iterable[BusinessObjectDomain],
    boCodes: set[str],
    domainCodes: set[str],
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for row in attached:
        if row.bo_code not in boCodes:
            raise ValueError(
                f"本体数据异常：business_object_domain bo_code={row.bo_code} "
                "不存在于 business_object"
            )
        if row.domain_id not in domainCodes:
            raise ValueError(
                f"本体数据异常：business_object_domain domain_id={row.domain_id} "
                "不存在于 data_domain"
            )
        grouped.setdefault(row.bo_code, []).append(row.domain_id)
    return {code: sorted(items) for code, items in grouped.items()}


def compileBusinessObject(
    row: BusinessObject,
    allClassById: dict[int, OntologyClass],
    activeNameById: dict[int, str],
    domainCodes: set[str],
    attachedDomainCodes: list[str],
) -> dict[str, Any]:
    header = requireClass(row.header_class_id, allClassById, "header_class_id")
    if header.valid_to is not None:
        # BO 的身份就是 header 类：类已删 ⇒ 不能像外键那样编译成 null，只能报错。
        raise ValueError(
            f"本体数据异常：business_object.code={row.code} 的 "
            f"header_class_id={row.header_class_id} 指向已删除类"
        )
    if row.primary_domain_id not in domainCodes:
        raise ValueError(
            f"本体数据异常：business_object.code={row.code} 的 "
            f"primary_domain_id={row.primary_domain_id} 不存在于 data_domain"
        )
    return {
        "code": row.code,
        "name": row.name,
        "headerClassName": activeNameById[header.id],
        "primaryDomainCode": row.primary_domain_id,
        "attachedDomainCodes": list(attachedDomainCodes),
        "graphLabel": row.graph_label,
        "description": row.description,
    }
