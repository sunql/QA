"""Golden Manifest（P1a / D6）：钉死发布快照的完整线上形态。

golden 是**策展的等形行集**，不是生产全量快照：

- 覆盖每一个 Manifest 字段（墓碑过滤、多源角色、指标两层、BO/域）；
- 体量可评审（KB 级）—— 生产全量约 965KB，一有本体改动 diff 就没法看了；
- 与其他用例同款纯行输入 ⇒ 不依赖数据库，CI 可跑。

真实库的全量编译由 `Harness/changes/feat-semantic-release-a/daily/D06.md`
的冒烟记录（分区计数 + sha256 + 确定性）覆盖。

**反向守卫**：本文件显式列出每个分区的键集合。只更新 golden 文件而漏改编译器
（或少字段）会在这里失败 —— 否则「编译器删字段 + golden 同步重生」会双双变绿。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain.models import BusinessObjectDomain
from app.services.semantic_compiler import compileFromRows
from app.tests.unit._semantic_rows import (
    canonicalJson,
    makeBusinessObject,
    makeClass,
    makeDataSource,
    makeDomain,
    makeJoin,
    makeKpi,
    makeMetric,
    makeMetricSource,
    makeProperty,
)

_GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "semantic_manifest_golden.json"

_CLASS_KEYS = {
    "className",
    "classAlias",
    "description",
    "sourceTable",
    "objectType",
    "objectOwner",
    "parentClassName",
    "properties",
}
_PROPERTY_KEYS = {
    "propertyName",
    "propertyAlias",
    "businessAliases",
    "description",
    "dataType",
    "isPrimaryKey",
    "isForeignKey",
    "refClassName",
    "sourceColumn",
    "unit",
    "allowedValues",
    "isNotNull",
    "minValue",
    "maxValue",
    "regexPattern",
}
_JOIN_KEYS = {
    "joinKey",
    "sourceClassName",
    "sourceColumns",
    "targetClassName",
    "targetColumns",
    "joinType",
    "relationType",
    "description",
}
_METRIC_KEYS = {
    "metricName",
    "metricAlias",
    "formula",
    "aggFunction",
    "metricType",
    "targetClassName",
    "dimensionDefaults",
    "grainKeys",
    "aggregationOrder",
    "numeratorMeasure",
    "denominatorMeasure",
    "filterExpression",
    "status",
    "owner",
    "executionSource",
    "sources",
    "kpiCodes",
}
_KPI_KEYS = {
    "kpiCode",
    "kpiName",
    "businessDefinition",
    "formula",
    "numerator",
    "denominator",
    "grain",
    "unit",
    "grainKeys",
    "numeratorColumns",
    "denominatorColumns",
    "owner",
    "version",
    "status",
    "semanticKeywords",
    "metricName",
    "datasourceName",
}
_BUSINESS_OBJECT_KEYS = {
    "code",
    "name",
    "headerClassName",
    "primaryDomainCode",
    "attachedDomainCodes",
    "graphLabel",
    "description",
}
_DOMAIN_KEYS = {"code", "name", "description", "enabled"}


def buildGoldenRows() -> dict[str, Any]:
    """策展行集：每个字段至少出现一次，且带一条墓碑链路验证过滤。"""
    classes = [
        makeClass(
            1,
            "Supplier",
            alias="供应商",
            sourceTable="SUPPLIER",
            objectType="Master",
            objectOwner="采购部",
        ),
        makeClass(2, "Document"),
        makeClass(3, "PurchaseOrder", alias="采购订单", sourceTable="PORDER", parentClassId=2),
        # 墓碑：其属性与 JOIN 都必须被过滤
        makeClass(4, "LegacySupplier", validTo=datetime(2026, 1, 1, tzinfo=UTC)),
    ]
    properties = [
        makeProperty(
            1,
            "supplierCode",
            alias="供应商编码",
            businessAliases=["供应商代码", "供应商编号"],
            dataType="STRING",
            isPrimaryKey=True,
            sourceColumn="SUPPLIER_CODE",
            # 三态 True：明确非空。
            isNotNull=True,
        ),
        makeProperty(
            1,
            "supplierName",
            alias="供应商名称",
            businessAliases=["供应商名"],
            dataType="STRING",
            sourceColumn="SUPPLIER_NAME",
        ),
        makeProperty(
            1,
            "creditLimit",
            dataType="DECIMAL",
            unit="元",
            allowedValues=["0", "1000000"],
            # 三态 False：明确允许为空（必须与「未约束」的 None 区分开）。
            isNotNull=False,
            minValue="0",
            maxValue="1000000",
            regexPattern=r"^\d+(\.\d{1,2})?$",
        ),
        makeProperty(
            3,
            "supplierCode",
            dataType="STRING",
            isForeignKey=True,
            refClassId=1,
            sourceColumn="SUPPLIER_CODE",
        ),
        makeProperty(
            3, "onTimeQty", dataType="DECIMAL", sourceColumn="ON_TIME_QTY", unit="件"
        ),
        makeProperty(3, "qty", dataType="DECIMAL", sourceColumn="QTY", unit="件"),
        makeProperty(4, "legacyCode", dataType="STRING"),
    ]
    joins = [
        makeJoin(
            "PURCHASE_ORDER_SUPPLIER",
            3,
            ["SUPPLIER_CODE"],
            1,
            ["SUPPLIER_CODE"],
            joinType="LEFT",
            relationType="foreign_key",
        ),
        makeJoin("LEGACY_JOIN", 3, ["SUPPLIER_CODE"], 4, ["LEGACY_CODE"]),
    ]
    metrics = [
        makeMetric(
            10,
            "supplierOnTimeRate",
            alias="供应商准时率",
            formula="SUM(ON_TIME_QTY) / SUM(QTY)",
            aggFunction="SUM",
            targetClassId=3,
            metricType="RATIO",
            numeratorMeasure={
                "agg": "SUM",
                "expr": "ON_TIME_QTY",
                "filter": None,
                "class_id": 3,
                "class_alias": "采购订单",
            },
            denominatorMeasure={
                "agg": "SUM",
                "expr": "QTY",
                "filter": None,
                "class_id": 3,
                "class_alias": "采购订单",
            },
            aggregationOrder="WEIGHTED_RATIO",
            grainKeys=["supplierCode"],
            filterExpression="STATUS <> 'CANCELLED'",
            dimensionDefaults={"time": "LAST_12_MONTHS"},
            status="PUBLISHED",
            owner="采购部",
            executionSource="ESCAPE_HATCH",
        ),
        makeMetric(
            11,
            "purchaseAmount",
            alias="采购金额",
            formula="SUM(PO_AMOUNT)",
            aggFunction="SUM",
            targetClassId=None,
            metricType="MEASURE",
            status="DRAFT",
            owner="采购部",
        ),
    ]
    metricSources = [
        makeMetricSource(10, 3, "BASE"),
        makeMetricSource(10, 3, "NUMERATOR"),
        makeMetricSource(10, 3, "DENOMINATOR"),
        makeMetricSource(10, 4, "FILTER"),  # 墓碑类 ⇒ 丢弃
        makeMetricSource(11, 3, "FILTER"),
    ]
    kpis = [
        makeKpi(
            "KPI-001",
            "供应商准时率",
            metricId=10,
            datasourceId=3,
            businessDefinition="统计周期内准时交付数量占收货数量比例",
            formula="SUM(ON_TIME_QTY) / SUM(QTY)",
            numerator="SUM(ON_TIME_QTY)",
            denominator="SUM(QTY)",
            grain="月/供应商",
            unit="%",
            grainKeys=["month", "supplierCode"],
            numeratorColumns=["ON_TIME_QTY"],
            denominatorColumns=["QTY"],
            owner="采购部",
            version="v1.2",
            status="PUBLISHED",
            semanticKeywords=["供应商准时率", "准时交付率"],
        ),
        makeKpi(
            "KPI-002",
            "采购金额",
            metricId=11,
            unit="元",
            grain="月",
            status="DRAFT",
        ),
        makeKpi("KPI-003", "待实现指标"),
    ]
    businessObjects = [
        makeBusinessObject(
            "SUPPLIER", "供应商", 1, "PROCUREMENT", graphLabel="Supplier"
        ),
        makeBusinessObject("PURCHASE_ORDER", "采购订单", 3, "PROCUREMENT"),
    ]
    businessObjectDomains = [
        BusinessObjectDomain(bo_code="SUPPLIER", domain_id="QUALITY"),
        BusinessObjectDomain(bo_code="SUPPLIER", domain_id="LOGISTICS"),
        BusinessObjectDomain(bo_code="PURCHASE_ORDER", domain_id="QUALITY"),
    ]
    domains = [
        makeDomain("PROCUREMENT", "采购域", description="采购主流程"),
        makeDomain("QUALITY", "质量域", enabled=False),
        makeDomain("LOGISTICS", "物流域"),
    ]
    datasources = [makeDataSource(3, "THBI")]
    return {
        "classes": classes,
        "properties": properties,
        "joins": joins,
        "metrics": metrics,
        "metricSources": metricSources,
        "kpis": kpis,
        "businessObjects": businessObjects,
        "businessObjectDomains": businessObjectDomains,
        "domains": domains,
        "datasources": datasources,
    }


def compileGolden() -> dict[str, Any]:
    return compileFromRows(**buildGoldenRows())


def test_golden_manifest_matches_compiler_output() -> None:
    """编译器输出必须与 `tests/fixtures/semantic_manifest_golden.json` 完全一致。"""
    expected = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))

    assert canonicalJson(compileGolden()) == canonicalJson(expected)


def test_golden_manifest_is_deterministic() -> None:
    assert canonicalJson(compileGolden()) == canonicalJson(compileGolden())


def test_every_section_and_key_is_pinned() -> None:
    """反向守卫：分区与字段集合必须与本节列出的键集合一致。

    只改 golden 文件、不动编译器（或编译器少字段）会在这里失败。
    """
    manifest = compileGolden()

    assert set(manifest) == {
        "schemaVersion",
        "domains",
        "classes",
        "joins",
        "metrics",
        "kpis",
        "businessObjects",
    }
    for section in ("domains", "classes", "joins", "metrics", "kpis", "businessObjects"):
        assert manifest[section], f"{section} 不能为空，否则 golden 覆盖不到该分区"

    assert {c["className"] for c in manifest["classes"]} == {
        "Document",
        "PurchaseOrder",
        "Supplier",
    }
    assert {k for c in manifest["classes"] for k in c} == _CLASS_KEYS
    assert {k for c in manifest["classes"] for p in c["properties"] for k in p} == _PROPERTY_KEYS
    assert {k for j in manifest["joins"] for k in j} == _JOIN_KEYS
    assert {k for m in manifest["metrics"] for k in m} == _METRIC_KEYS
    assert {k for kpi in manifest["kpis"] for k in kpi} == _KPI_KEYS
    assert {k for b in manifest["businessObjects"] for k in b} == _BUSINESS_OBJECT_KEYS
    assert {k for d in manifest["domains"] for k in d} == _DOMAIN_KEYS


def test_property_nullability_is_three_state() -> None:
    """`isNotNull` 必须保留三态：None（未约束）/ True / False。

    用 `bool()` 压平会把「未约束」写成「允许为空」，语义悄悄收紧且无任何用例变红。
    """
    manifest = compileGolden()
    supplier = next(c for c in manifest["classes"] if c["className"] == "Supplier")
    byName = {prop["propertyName"]: prop for prop in supplier["properties"]}

    assert byName["supplierCode"]["isNotNull"] is True
    assert byName["creditLimit"]["isNotNull"] is False
    assert byName["supplierName"]["isNotNull"] is None


def test_golden_filters_tombstones_and_resolves_references() -> None:
    manifest = compileGolden()

    assert "LegacySupplier" not in {c["className"] for c in manifest["classes"]}
    assert [j["joinKey"] for j in manifest["joins"]] == ["PURCHASE_ORDER_SUPPLIER"]
    metric = next(m for m in manifest["metrics"] if m["metricName"] == "supplierOnTimeRate")
    assert metric["sources"] == [
        {"className": "PurchaseOrder", "role": "BASE"},
        {"className": "PurchaseOrder", "role": "DENOMINATOR"},
        {"className": "PurchaseOrder", "role": "NUMERATOR"},
    ]
    assert metric["kpiCodes"] == ["KPI-001"]
