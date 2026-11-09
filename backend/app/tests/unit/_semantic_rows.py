"""语义编译器测试共用的行构造器与规范化序列化（纯测试工具）。"""

from __future__ import annotations

import json
from datetime import datetime
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


def canonicalJson(payload: Any) -> str:
    """与 `_schemaVersion` 同款规范化序列化：键序稳定、无空白、保留中文。"""
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
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
    alias: str | None = None,
    businessAliases: list[str] | None = None,
    dataType: str = "STRING",
    isPrimaryKey: bool = False,
    isForeignKey: bool = False,
    refClassId: int | None = None,
    sourceColumn: str | None = None,
    unit: str | None = None,
    allowedValues: list[str] | None = None,
) -> OntologyProperty:
    return OntologyProperty(
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
