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
    FORBIDDEN_MANIFEST_KEYS,
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
    makePropertyBinding,
    makeSourceBinding,
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
_SOURCE_BINDING_KEYS = {
    "className",
    "datasourceName",
    "schemaName",
    "sourceTable",
    "bindingRole",
    "filterExpression",
    "filterAllowlist",
    "properties",
}
_SOURCE_BINDING_PROPERTY_KEYS = {
    "propertyName",
    "sourceColumn",
    "keyOrdinal",
    "transformExpression",
}


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
            propertyId=101,
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
            propertyId=102,
            alias="供应商名称",
            businessAliases=["供应商名"],
            dataType="STRING",
            sourceColumn="SUPPLIER_NAME",
        ),
        makeProperty(
            1,
            "creditLimit",
            propertyId=103,
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
            propertyId=104,
            dataType="STRING",
            isForeignKey=True,
            refClassId=1,
            sourceColumn="SUPPLIER_CODE",
        ),
        makeProperty(
            3,
            "onTimeQty",
            propertyId=105,
            dataType="DECIMAL",
            sourceColumn="ON_TIME_QTY",
            unit="件",
        ),
        makeProperty(
            3, "qty", propertyId=106, dataType="DECIMAL", sourceColumn="QTY", unit="件"
        ),
        makeProperty(4, "legacyCode", propertyId=107, dataType="STRING"),
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
    # 源绑定（v2 / M1-1.3）：四条源绑定覆盖四种排除口径 ——
    # ① 已确认且生效 ⇒ 进快照；② 候选（D-3）⇒ 排除；
    # ③ 指向墓碑类 ⇒ 排除；④ 已失效（valid_to 非空）⇒ 排除。
    sourceBindings = [
        makeSourceBinding(
            1,
            class_id=1,
            datasource_id=3,
            schema_name="ERP",
            source_table="SUPPLIER",
            binding_role="BASE",
            filter_expression="STATUS = 'A'",
            filter_allowlist=["STATUS"],
        ),
        makeSourceBinding(
            2,
            class_id=1,
            datasource_id=3,
            schema_name="ERP",
            source_table="SUPPLIER_DRAFT",
            binding_role="FILTER",
            is_candidate=True,
        ),
        makeSourceBinding(
            3,
            class_id=4,
            datasource_id=3,
            schema_name="ERP",
            source_table="LEGACY_SUPPLIER",
        ),
        makeSourceBinding(
            4,
            class_id=3,
            datasource_id=3,
            schema_name="ERP",
            source_table="PORDER",
            valid_to=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]
    sourceBindingProperties = [
        makePropertyBinding(1, 1, 101, "SUPPLIER_CODE", key_ordinal=1),
        makePropertyBinding(2, 1, 102, "SUPPLIER_NAME"),
        # 属性属于墓碑类 ⇒ 丢弃。
        makePropertyBinding(3, 1, 107, "LEGACY_CODE"),
        # 候选绑定的属性 ⇒ 丢弃。
        makePropertyBinding(4, 2, 101, "SUPPLIER_CODE"),
        # 失效绑定的属性 ⇒ 丢弃。
        makePropertyBinding(5, 4, 104, "SUPPLIER_CODE"),
    ]
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
        "sourceBindings": sourceBindings,
        "sourceBindingProperties": sourceBindingProperties,
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
        "sourceBindings",
    }
    for section in (
        "domains",
        "classes",
        "joins",
        "metrics",
        "kpis",
        "businessObjects",
        "sourceBindings",
    ):
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
    assert {k for b in manifest["sourceBindings"] for k in b} == _SOURCE_BINDING_KEYS
    assert {
        k for b in manifest["sourceBindings"] for p in b["properties"] for k in p
    } == _SOURCE_BINDING_PROPERTY_KEYS


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


def test_golden_source_bindings_exclude_candidates_and_inactive() -> None:
    """绑定分区只收「已确认 + 当前生效 + 类仍在活跃模型」的绑定。

    三条排除口径各有一条样本（见 `buildGoldenRows`）：候选（D-3）、墓碑类、已失效。
    """
    bindings = compileGolden()["sourceBindings"]

    assert len(bindings) == 1
    binding = bindings[0]
    assert binding["className"] == "Supplier"
    assert binding["datasourceName"] == "THBI"
    assert binding["schemaName"] == "ERP"
    assert binding["sourceTable"] == "SUPPLIER"
    assert binding["bindingRole"] == "BASE"
    assert binding["filterExpression"] == "STATUS = 'A'"
    assert binding["filterAllowlist"] == ["STATUS"]
    assert [p["propertyName"] for p in binding["properties"]] == [
        "supplierCode",
        "supplierName",
    ]
    assert binding["properties"][0]["keyOrdinal"] == 1
    assert binding["properties"][1]["keyOrdinal"] is None


def test_golden_snapshot_carries_no_instance_columns() -> None:
    """快照里不得出现自增 id / 时间戳 / 实例相关列（字节确定性铁律）。

    时间戳与自增 id 一旦进快照，同一治理内容两次编译的字节就不同 ⇒
    M1-a DoD 第 2 条（同输入两次编译字节一致）直接被破坏。

    禁词清单与 `test_semantic_manifest_schema.py` 的键名级守卫**共用同一份**
    （`FORBIDDEN_MANIFEST_KEYS`）⇒ 两处守卫不会各漏各的。
    """
    text = canonicalJson(compileGolden())

    for forbidden in sorted(FORBIDDEN_MANIFEST_KEYS):
        assert forbidden not in text, f"快照出现实例相关字段 {forbidden!r}"
