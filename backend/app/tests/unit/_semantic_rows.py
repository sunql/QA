"""语义编译器测试共用的行构造器与规范化序列化（纯测试工具）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.domain.models import (
    BusinessObject,
    DataDomain,
    DataSource,
    KpiCatalog,
    OntologyClass,
    OntologyJoin,
    OntologyMetric,
    OntologyMetricSource,
    OntologyProperty,
)
from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)


def canonicalJson(payload: Any) -> str:
    """与 `_schemaVersion` 同款规范化序列化：键序稳定、无空白、保留中文。"""
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


#: 绑定行的默认生效时刻（**过去**）—— 库侧 `valid_from` 是 `now()` 默认值，
#: 测试里显式给一个过去的时刻 ⇒ 「当前生效」这条判据真的被走到，
#: 而不是靠「字段为 None 时按已生效处理」的兜底蒙混过关。
DEFAULT_VALID_FROM = datetime(2020, 1, 1, tzinfo=UTC)


# 快照里**不得出现**的键（自增 id / 行身份 / 时间戳 / 实例态）。
#
# 为什么单列一份、两处守卫共用：字节确定性铁律要求「同一治理内容两次编译字节一致」，
# 而这两类字段都会随环境/时间/状态变化 ⇒ 一旦入档，DoD 第 2 条直接被破坏。
# ⚠️ 必须**单点定义**：两处各写一份清单 ⇒ 任一处漏项只被另一处覆盖，
# 看上去有守卫、实则那个漏项没人管（这正是「清单漂移 ⇒ 假绿」的典型形态）。
FORBIDDEN_MANIFEST_KEYS: frozenset[str] = frozenset(
    {
        # 自增 id / 行身份
        "class_id",
        "classId",
        "datasourceId",
        "targetClassId",
        "sourceBindingId",
        "propertyId",
        # 时间戳 / 实例态
        "validFrom",
        "validTo",
        "confirmedBy",
        "confirmedTime",
        "isCandidate",
        "createdBy",
        "createdTime",
        "updatedTime",
    }
)


def makeClass(
    class_id: int,
    name: str,
    *,
    alias: str | None = None,
    sourceTable: str | None = None,
    objectType: str | None = None,
    objectOwner: str | None = None,
    parentClassId: int | None = None,
    validTo: datetime | None = None,
) -> OntologyClass:
    return OntologyClass(
        id=class_id,
        class_name=name,
        class_alias=alias,
        source_table=sourceTable,
        object_type=objectType,
        object_owner=objectOwner,
        parent_class_id=parentClassId,
        valid_to=validTo,
    )


def makeProperty(
    class_id: int,
    name: str,
    *,
    propertyId: int | None = None,
    alias: str | None = None,
    businessAliases: list[str] | None = None,
    dataType: str = "STRING",
    isPrimaryKey: bool = False,
    isForeignKey: bool = False,
    refClassId: int | None = None,
    sourceColumn: str | None = None,
    unit: str | None = None,
    allowedValues: list[str] | None = None,
    isNotNull: bool | None = None,
    minValue: str | None = None,
    maxValue: str | None = None,
    regexPattern: str | None = None,
) -> OntologyProperty:
    return OntologyProperty(
        id=propertyId,
        class_id=class_id,
        property_name=name,
        property_alias=alias,
        business_aliases=businessAliases,
        data_type=dataType,
        is_primary_key=isPrimaryKey,
        is_foreign_key=isForeignKey,
        ref_class_id=refClassId,
        source_column=sourceColumn,
        unit=unit,
        allowed_values=allowedValues,
        # 三态：None = 未约束，False = 明确允许为空（不可用 bool() 压平）。
        is_not_null=isNotNull,
        min_value=minValue,
        max_value=maxValue,
        regex_pattern=regexPattern,
    )


def makeJoin(
    joinKey: str,
    sourceClassId: int,
    sourceColumns: list[str],
    targetClassId: int,
    targetColumns: list[str],
    *,
    joinType: str = "INNER",
    relationType: str = "business",
) -> OntologyJoin:
    return OntologyJoin(
        join_key=joinKey,
        source_class_id=sourceClassId,
        source_columns=sourceColumns,
        target_class_id=targetClassId,
        target_columns=targetColumns,
        join_type=joinType,
        relation_type=relationType,
    )


def makeMetric(
    metric_id: int,
    name: str,
    *,
    formula: str = "SUM(x)",
    aggFunction: str = "SUM",
    targetClassId: int | None = None,
    metricType: str = "MEASURE",
    numeratorMeasure: dict | None = None,
    denominatorMeasure: dict | None = None,
    aggregationOrder: str | None = None,
    grainKeys: list[str] | None = None,
    filterExpression: str | None = None,
    dimensionDefaults: dict | None = None,
    status: str = "DRAFT",
    owner: str | None = None,
    executionSource: str | None = None,
    alias: str | None = None,
) -> OntologyMetric:
    return OntologyMetric(
        id=metric_id,
        metric_name=name,
        metric_alias=alias,
        formula=formula,
        agg_function=aggFunction,
        target_class_id=targetClassId,
        metric_type=metricType,
        numerator_measure=numeratorMeasure,
        denominator_measure=denominatorMeasure,
        aggregation_order=aggregationOrder,
        grain_keys=grainKeys,
        filter_expression=filterExpression,
        dimension_defaults=dimensionDefaults,
        status=status,
        owner=owner,
        execution_source=executionSource,
    )


def makeMetricSource(metric_id: int, class_id: int, role: str) -> OntologyMetricSource:
    return OntologyMetricSource(metric_id=metric_id, class_id=class_id, role=role)


def makeKpi(
    kpi_code: str,
    name: str,
    *,
    metricId: int | None = None,
    datasourceId: int | None = None,
    businessDefinition: str | None = None,
    formula: str | None = None,
    numerator: str | None = None,
    denominator: str | None = None,
    grain: str | None = None,
    unit: str | None = None,
    grainKeys: list[str] | None = None,
    numeratorColumns: list[str] | None = None,
    denominatorColumns: list[str] | None = None,
    owner: str | None = None,
    version: str = "v1.0",
    status: str = "PUBLISHED",
    semanticKeywords: list[str] | None = None,
) -> KpiCatalog:
    return KpiCatalog(
        kpi_code=kpi_code,
        kpi_name=name,
        business_definition=businessDefinition,
        formula=formula,
        numerator=numerator,
        denominator=denominator,
        grain=grain,
        unit=unit,
        grain_keys=grainKeys,
        numerator_columns=numeratorColumns,
        denominator_columns=denominatorColumns,
        metric_id=metricId,
        datasource_id=datasourceId,
        owner=owner,
        version=version,
        status=status,
        semantic_keywords=semanticKeywords,
    )


def makeBusinessObject(
    code: str,
    name: str,
    headerClassId: int,
    primaryDomainId: str,
    *,
    graphLabel: str | None = None,
) -> BusinessObject:
    return BusinessObject(
        code=code,
        name=name,
        header_class_id=headerClassId,
        primary_domain_id=primaryDomainId,
        graph_label=graphLabel,
    )


def makeDomain(
    code: str, name: str, *, enabled: bool = True, description: str | None = None
) -> DataDomain:
    return DataDomain(code=code, name=name, enabled=enabled, description=description)


def makeDataSource(ds_id: int, name: str) -> DataSource:
    return DataSource(
        id=ds_id,
        name=name,
        type="ORACLE",
        host="h",
        port=1521,
        database_name="db",
        username="u",
        password_encrypted="x",
    )


def makeSourceBinding(
    binding_id: int,
    class_id: int,
    datasource_id: int,
    schema_name: str,
    source_table: str,
    *,
    binding_role: str = "BASE",
    filter_expression: str | None = None,
    filter_allowlist: list[str] | None = None,
    is_candidate: bool = False,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> ProcurementSourceBinding:
    return ProcurementSourceBinding(
        id=binding_id,
        class_id=class_id,
        datasource_id=datasource_id,
        schema_name=schema_name,
        source_table=source_table,
        binding_role=binding_role,
        filter_expression=filter_expression,
        filter_allowlist=filter_allowlist,
        is_candidate=is_candidate,
        valid_from=valid_from if valid_from is not None else DEFAULT_VALID_FROM,
        valid_to=valid_to,
    )


def makePropertyBinding(
    binding_id: int,
    source_binding_id: int,
    property_id: int,
    source_column: str,
    *,
    key_ordinal: int | None = None,
    transform_expression: str | None = None,
    is_candidate: bool = False,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> ProcurementPropertyBinding:
    return ProcurementPropertyBinding(
        id=binding_id,
        source_binding_id=source_binding_id,
        property_id=property_id,
        source_column=source_column,
        key_ordinal=key_ordinal,
        transform_expression=transform_expression,
        is_candidate=is_candidate,
        valid_from=valid_from if valid_from is not None else DEFAULT_VALID_FROM,
        valid_to=valid_to,
    )
