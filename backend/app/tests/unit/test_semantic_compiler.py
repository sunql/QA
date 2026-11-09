"""语义编译器（P1a / D5–D6）：治理表行 → 规范化 Manifest。

完成定义（日级排程 D5）：
- 同输入两次输出**字节一致**（规范化 JSON 完全相同）；
- 空 / 单类 / 多类用例通过。

设计约束：
- **纯函数**：`compileFromRows` 不碰数据库（测试可直接构造 ORM 行、不 flush），
  数据库读取由 `compileManifest(session)` 负责（集成用例单独覆盖）。
- **无环境相关字段**：Manifest 里不出现自增 id、created_by、version/valid_* 等
  依赖实例的残留列 —— 换一个环境（测试库/生产）同样的治理内容必须得到同样的字节。
- **确定性**：类按 `className`、属性按 `propertyName`、JOIN 按 `joinKey` 稳定排序；
  JSON 序列化统一用 `sort_keys=True, ensure_ascii=False, separators=(",", ":")`。
- **软删除过滤**：`valid_to` 非空是墓碑（Phase 6 移除版本管理后 deleteClass 的形态）。
  墓碑类、其属性、以及**任一端点是墓碑**的 JOIN 都不属于活跃模型 ⇒ 不进入快照。
  （实测本地库：18 个墓碑类、463 条墓碑属性、72 条端点已删的 JOIN —— 若不过滤，
  快照里会带着已删除的类，NL2SQL 会重新用上它们。）
- **引用悬空**：指向墓碑类的 `refClassName` / `parentClassName` 编译为 `null`（目标已不在模型中）；
  指向**完全不存在**的 class_id 说明数据被写坏（FK 也挡不住被绕过的写入）⇒ 抛错。
- **重复活跃类名**：`uq_ontology_class_name_version` 只约束 (name, version)，同名多版本行在
  Phase 6 后属异常 ⇒ 抛错，不静默取其一。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.models import BusinessObjectDomain, OntologyProperty
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

# ---------------------------------------------------------------------------
# 1. 空输入
# ---------------------------------------------------------------------------


def test_empty_input_produces_minimal_manifest() -> None:
    manifest = compileFromRows(classes=[], properties=[], joins=[])

    # 空输入时每类集合都显式出现（空数组），不能省略键 —— 否则下游 `manifest["metrics"]`
    # 在旧快照上 KeyError，Schema 校验也要靠「键必须存在」。
    assert manifest == {
        "schemaVersion": "v1",
        "domains": [],
        "classes": [],
        "joins": [],
        "metrics": [],
        "kpis": [],
        "businessObjects": [],
    }


# ---------------------------------------------------------------------------
# 2. 单类
# ---------------------------------------------------------------------------


def test_single_class_compiles_properties_and_defaults() -> None:
    classes = [makeClass(1, "Supplier", sourceTable="SUPPLIER")]
    properties = [
        makeProperty(
            1,
            "supplierCode",
            dataType="STRING",
            isPrimaryKey=True,
            sourceColumn="SUPPLIER_CODE",
        )
    ]

    manifest = compileFromRows(classes=classes, properties=properties, joins=[])

    assert [c["className"] for c in manifest["classes"]] == ["Supplier"]
    cls = manifest["classes"][0]
    assert cls["sourceTable"] == "SUPPLIER"
    assert cls["classAlias"] is None
    assert cls["objectType"] is None
    assert cls["parentClassName"] is None

    [prop] = cls["properties"]
    assert prop == {
        "propertyName": "supplierCode",
        "propertyAlias": None,
        "businessAliases": [],
        "description": None,
        "dataType": "STRING",
        "isPrimaryKey": True,
        "isForeignKey": False,
        "refClassName": None,
        "sourceColumn": "SUPPLIER_CODE",
        "unit": None,
        "allowedValues": [],
        "isNotNull": None,
        "minValue": None,
        "maxValue": None,
        "regexPattern": None,
    }


def test_foreign_key_property_resolves_ref_class_name() -> None:
    classes = [makeClass(1, "Order"), makeClass(2, "Supplier")]
    properties = [
        makeProperty(
            1,
            "supplierCode",
            dataType="STRING",
            isForeignKey=True,
            refClassId=2,
        )
    ]

    manifest = compileFromRows(classes=classes, properties=properties, joins=[])

    [prop] = manifest["classes"][0]["properties"]
    assert prop["isForeignKey"] is True
    assert prop["refClassName"] == "Supplier"


def test_parent_class_resolves_to_name() -> None:
    classes = [makeClass(1, "Document"), makeClass(2, "PurchaseOrder", parentClassId=1)]
    properties: list[OntologyProperty] = []

    manifest = compileFromRows(classes=classes, properties=properties, joins=[])

    byName = {c["className"]: c for c in manifest["classes"]}
    assert byName["PurchaseOrder"]["parentClassName"] == "Document"
    assert byName["Document"]["parentClassName"] is None


# ---------------------------------------------------------------------------
# 3. 排序与字节一致
# ---------------------------------------------------------------------------


def test_output_is_byte_identical_regardless_of_input_order() -> None:
    """同一治理内容，行顺序不同（甚至两次编译）必须得到完全相同的字节。"""
    classA = makeClass(1, "Supplier", sourceTable="SUPPLIER")
    classB = makeClass(2, "PurchaseOrder", sourceTable="PO")
    prop1 = makeProperty(1, "supplierName", sourceColumn="NAME")
    prop2 = makeProperty(1, "supplierCode", isPrimaryKey=True)
    prop3 = makeProperty(2, "poNo", isPrimaryKey=True)
    join = makeJoin("SUPPLIER_CODE->PO.SUPPLIER_CODE", 2, ["SUPPLIER_CODE"], 1, ["SUPPLIER_CODE"])

    forward = compileFromRows(
        classes=[classA, classB],
        properties=[prop1, prop2, prop3],
        joins=[join],
    )
    backward = compileFromRows(
        classes=[classB, classA],
        properties=[prop3, prop1, prop2],
        joins=[join],
    )

    assert canonicalJson(forward) == canonicalJson(backward)
    assert canonicalJson(forward) == canonicalJson(compileFromRows(
        classes=[classA, classB],
        properties=[prop1, prop2, prop3],
        joins=[join],
    ))


def test_collections_are_sorted_by_stable_keys() -> None:
    classes = [makeClass(1, "Zeta"), makeClass(2, "Alpha"), makeClass(3, "Mike")]
    properties = [
        makeProperty(1, "zField"),
        makeProperty(1, "aField"),
        makeProperty(1, "mField"),
    ]
    joins = [
        makeJoin("b->a", 2, ["B"], 3, ["A"]),
        makeJoin("a->b", 2, ["A"], 3, ["B"]),
    ]

    manifest = compileFromRows(classes=classes, properties=properties, joins=joins)

    assert [c["className"] for c in manifest["classes"]] == ["Alpha", "Mike", "Zeta"]
    assert [p["propertyName"] for p in manifest["classes"][-1]["properties"]] == [
        "aField",
        "mField",
        "zField",
    ]
    assert [j["joinKey"] for j in manifest["joins"]] == ["a->b", "b->a"]


# ---------------------------------------------------------------------------
# 4. 软删除
# ---------------------------------------------------------------------------


def test_soft_deleted_class_and_its_properties_are_excluded() -> None:
    live = makeClass(1, "Supplier")
    tombstone = makeClass(2, "LegacySupplier", validTo=datetime(2026, 1, 1, tzinfo=UTC))
    properties = [
        makeProperty(1, "supplierCode"),
        makeProperty(2, "legacyCode"),
    ]

    manifest = compileFromRows(classes=[live, tombstone], properties=properties, joins=[])

    assert [c["className"] for c in manifest["classes"]] == ["Supplier"]
    assert [p["propertyName"] for p in manifest["classes"][0]["properties"]] == [
        "supplierCode"
    ]


# ---------------------------------------------------------------------------
# 5. 严格核心：数据完整性错误必须抛错
# ---------------------------------------------------------------------------


def test_duplicate_active_class_name_raises() -> None:
    """正常形态每 class_name 仅一行；出现重复说明治理数据被写坏了，不能静默取其一。"""
    classes = [makeClass(1, "Supplier"), makeClass(2, "Supplier")]

    with pytest.raises(ValueError, match="Supplier"):
        compileFromRows(classes=classes, properties=[], joins=[])


def test_property_pointing_at_unknown_class_raises() -> None:
    with pytest.raises(ValueError, match="class_id=99"):
        compileFromRows(
            classes=[makeClass(1, "Supplier")],
            properties=[makeProperty(99, "orphan")],
            joins=[],
        )


def test_property_ref_to_tombstoned_class_compiles_as_null() -> None:
    """目标类已删 ⇒ 外键语义仍保留（isForeignKey=True），但解析不出活跃类名。"""
    live = makeClass(1, "Supplier")
    tombstone = makeClass(2, "Legacy", validTo=datetime(2026, 1, 1, tzinfo=UTC))
    properties = [
        makeProperty(1, "legacyCode", isForeignKey=True, refClassId=2),
    ]

    manifest = compileFromRows(
        classes=[live, tombstone], properties=properties, joins=[]
    )

    [prop] = manifest["classes"][0]["properties"]
    assert prop["isForeignKey"] is True
    assert prop["refClassName"] is None


def test_property_ref_to_nonexistent_class_raises() -> None:
    with pytest.raises(ValueError, match="ref_class_id=42"):
        compileFromRows(
            classes=[makeClass(1, "Supplier")],
            properties=[makeProperty(1, "badRef", isForeignKey=True, refClassId=42)],
            joins=[],
        )


def test_join_to_tombstoned_class_is_excluded() -> None:
    live = makeClass(1, "Supplier")
    tombstone = makeClass(2, "Legacy", validTo=datetime(2026, 1, 1, tzinfo=UTC))
    join = makeJoin("x", 1, ["A"], 2, ["B"])

    manifest = compileFromRows(
        classes=[live, tombstone], properties=[], joins=[join]
    )

    assert manifest["joins"] == []


def test_join_pointing_at_nonexistent_class_raises() -> None:
    join = makeJoin("x", 1, ["A"], 99, ["B"])

    with pytest.raises(ValueError, match="99"):
        compileFromRows(classes=[makeClass(1, "Supplier")], properties=[], joins=[join])


def test_parent_pointing_at_tombstoned_class_compiles_as_root() -> None:
    live = makeClass(1, "PurchaseOrder", parentClassId=2)
    tombstone = makeClass(2, "Legacy", validTo=datetime(2026, 1, 1, tzinfo=UTC))

    manifest = compileFromRows(classes=[live, tombstone], properties=[], joins=[])

    assert manifest["classes"][0]["parentClassName"] is None


def test_parent_pointing_at_nonexistent_class_raises() -> None:
    classes = [makeClass(1, "PurchaseOrder", parentClassId=42)]

    with pytest.raises(ValueError, match="42"):
        compileFromRows(classes=classes, properties=[], joins=[])


# ---------------------------------------------------------------------------
# 6. JOIN 形态
# ---------------------------------------------------------------------------


def test_join_compiles_endpoint_names_and_column_pairs() -> None:
    classes = [makeClass(1, "Supplier"), makeClass(2, "PurchaseOrder")]
    join = makeJoin(
        "SUPPLIER_CODE->SUPPLIER.SUPPLIER_CODE",
        2,
        ["SUPPLIER_CODE"],
        1,
        ["SUPPLIER_CODE"],
        joinType="LEFT",
        relationType="foreign_key",
    )

    manifest = compileFromRows(classes=classes, properties=[], joins=[join])

    assert manifest["joins"] == [
        {
            "joinKey": "SUPPLIER_CODE->SUPPLIER.SUPPLIER_CODE",
            "sourceClassName": "PurchaseOrder",
            "sourceColumns": ["SUPPLIER_CODE"],
            "targetClassName": "Supplier",
            "targetColumns": ["SUPPLIER_CODE"],
            "joinType": "LEFT",
            "relationType": "foreign_key",
            "description": None,
        }
    ]


# ---------------------------------------------------------------------------
# 7. 指标（D6）：技术口径 + 多源角色 + KPI 反向引用
# ---------------------------------------------------------------------------


def test_metric_compiles_technical_form_and_sources() -> None:
    classes = [makeClass(1, "PurchaseOrder"), makeClass(2, "Supplier")]
    metric = makeMetric(
        10,
        "supplierOnTimeRate",
        formula="numerator / denominator",
        aggFunction="AVG",
        metricType="RATIO",
        numeratorMeasure={
            "agg": "SUM",
            "expr": "ON_TIME_QTY",
            "filter": None,
            "class_id": 1,
            "class_alias": "采购订单",
        },
        denominatorMeasure={
            "agg": "SUM",
            "expr": "QTY",
            "filter": None,
            "class_id": 1,
            "class_alias": "采购订单",
        },
        aggregationOrder="WEIGHTED_RATIO",
        grainKeys=["supplierCode"],
        filterExpression="STATUS <> 'CANCELLED'",
        dimensionDefaults={"time": "LAST_12_MONTHS"},
        status="DRAFT",
        owner="采购部",
        executionSource="ESCAPE_HATCH",
        alias="供应商准时率",
    )
    sources = [
        makeMetricSource(10, 2, "FILTER"),
        makeMetricSource(10, 1, "NUMERATOR"),
        makeMetricSource(10, 2, "BASE"),
    ]
    kpis = [makeKpi("KPI-002", "准时交付率", metricId=10), makeKpi("KPI-001", "采购金额", metricId=10)]

    manifest = compileFromRows(
        classes=classes,
        properties=[],
        joins=[],
        metrics=[metric],
        metricSources=sources,
        kpis=kpis,
    )

    [compiled] = manifest["metrics"]
    assert compiled == {
        "metricName": "supplierOnTimeRate",
        "metricAlias": "供应商准时率",
        "formula": "numerator / denominator",
        "aggFunction": "AVG",
        "metricType": "RATIO",
        "targetClassName": None,
        "dimensionDefaults": {"time": "LAST_12_MONTHS"},
        "grainKeys": ["supplierCode"],
        "aggregationOrder": "WEIGHTED_RATIO",
        "numeratorMeasure": {
            "agg": "SUM",
            "expr": "ON_TIME_QTY",
            "filter": None,
            "className": "PurchaseOrder",
            "classAlias": "采购订单",
        },
        "denominatorMeasure": {
            "agg": "SUM",
            "expr": "QTY",
            "filter": None,
            "className": "PurchaseOrder",
            "classAlias": "采购订单",
        },
        "filterExpression": "STATUS <> 'CANCELLED'",
        "status": "DRAFT",
        "owner": "采购部",
        "executionSource": "ESCAPE_HATCH",
        # 多源按 (role, className) 排序 ⇒ BASE 在 FILTER/NUMERATOR 之前
        "sources": [
            {"className": "Supplier", "role": "BASE"},
            {"className": "Supplier", "role": "FILTER"},
            {"className": "PurchaseOrder", "role": "NUMERATOR"},
        ],
        # 反向引用：kpi_catalog.metric_id → metric，按 kpiCode 排序
        "kpiCodes": ["KPI-001", "KPI-002"],
    }


def test_metric_target_class_resolves_and_tombstone_becomes_null() -> None:
    live = makeClass(1, "PurchaseOrder")
    tombstone = makeClass(2, "Legacy", validTo=datetime(2026, 1, 1, tzinfo=UTC))
    metrics = [
        makeMetric(10, "liveMetric", targetClassId=1),
        makeMetric(11, "tombstoneMetric", targetClassId=2),
    ]

    manifest = compileFromRows(
        classes=[live, tombstone], properties=[], joins=[], metrics=metrics
    )

    byName = {m["metricName"]: m for m in manifest["metrics"]}
    assert byName["liveMetric"]["targetClassName"] == "PurchaseOrder"
    assert byName["tombstoneMetric"]["targetClassName"] is None


def test_metric_target_class_nonexistent_raises() -> None:
    with pytest.raises(ValueError, match="target_class_id=99"):
        compileFromRows(
            classes=[makeClass(1, "Supplier")],
            properties=[],
            joins=[],
            metrics=[makeMetric(10, "bad", targetClassId=99)],
        )


def test_metric_source_to_tombstoned_class_is_excluded() -> None:
    live = makeClass(1, "PurchaseOrder")
    tombstone = makeClass(2, "Legacy", validTo=datetime(2026, 1, 1, tzinfo=UTC))
    sources = [
        makeMetricSource(10, 1, "BASE"),
        makeMetricSource(10, 2, "NUMERATOR"),
    ]

    manifest = compileFromRows(
        classes=[live, tombstone],
        properties=[],
        joins=[],
        metrics=[makeMetric(10, "m")],
        metricSources=sources,
    )

    assert manifest["metrics"][0]["sources"] == [
        {"className": "PurchaseOrder", "role": "BASE"}
    ]


def test_metric_source_with_unknown_metric_or_class_raises() -> None:
    classes = [makeClass(1, "Supplier")]
    with pytest.raises(ValueError, match="metric_id=99"):
        compileFromRows(
            classes=classes,
            properties=[],
            joins=[],
            metrics=[makeMetric(10, "m")],
            metricSources=[makeMetricSource(99, 1, "BASE")],
        )
    with pytest.raises(ValueError, match="class_id=88"):
        compileFromRows(
            classes=classes,
            properties=[],
            joins=[],
            metrics=[makeMetric(10, "m")],
            metricSources=[makeMetricSource(10, 88, "BASE")],
        )


# ---------------------------------------------------------------------------
# 8. KPI 业务层（D6）：与指标两层并存，弱引用可空
# ---------------------------------------------------------------------------


def test_kpi_compiles_business_layer_with_metric_and_datasource_names() -> None:
    kpi = makeKpi(
        "KPI-001",
        "采购金额",
        metricId=10,
        datasourceId=3,
        businessDefinition="统计周期内下单金额合计",
        formula="SUM(AMOUNT)",
        numerator="SUM(AMOUNT)",
        denominator=None,
        grain="月/供应商",
        unit="元",
        grainKeys=["month", "supplierCode"],
        numeratorColumns=["AMOUNT"],
        denominatorColumns=None,
        owner="采购部",
        version="v1.2",
        status="PUBLISHED",
        semanticKeywords=["采购金额", "采购额"],
    )

    manifest = compileFromRows(
        classes=[],
        properties=[],
        joins=[],
        metrics=[makeMetric(10, "purchaseAmount")],
        kpis=[kpi],
        datasources=[makeDataSource(3, "THBI")],
    )

    [compiled] = manifest["kpis"]
    assert compiled == {
        "kpiCode": "KPI-001",
        "kpiName": "采购金额",
        "businessDefinition": "统计周期内下单金额合计",
        "formula": "SUM(AMOUNT)",
        "numerator": "SUM(AMOUNT)",
        "denominator": None,
        "grain": "月/供应商",
        "unit": "元",
        "grainKeys": ["month", "supplierCode"],
        "numeratorColumns": ["AMOUNT"],
        "denominatorColumns": [],
        "owner": "采购部",
        "version": "v1.2",
        "status": "PUBLISHED",
        "semanticKeywords": ["采购金额", "采购额"],
        "metricName": "purchaseAmount",
        # 数据源只认名字：id 是自增主键，入档会让跨环境字节不一致。
        "datasourceName": "THBI",
    }
    assert "datasourceId" not in compiled


def test_kpi_without_metric_is_allowed_and_without_datasource_is_null() -> None:
    """KPI 可先于技术指标存在（业务定义先行，技术实现后置）。"""
    kpi = makeKpi("KPI-009", "待实现指标")

    manifest = compileFromRows(classes=[], properties=[], joins=[], kpis=[kpi])

    [compiled] = manifest["kpis"]
    assert compiled["metricName"] is None
    assert compiled["datasourceName"] is None


def test_kpi_with_dangling_metric_or_datasource_raises() -> None:
    with pytest.raises(ValueError, match="metric_id=77"):
        compileFromRows(
            classes=[], properties=[], joins=[], kpis=[makeKpi("K1", "x", metricId=77)]
        )
    with pytest.raises(ValueError, match="datasource_id=66"):
        compileFromRows(
            classes=[],
            properties=[],
            joins=[],
            kpis=[makeKpi("K1", "x", datasourceId=66)],
        )


def test_kpi_has_no_decimal_or_float_noise() -> None:
    """`kpi_catalog.match_threshold` 是 Numeric/Decimal —— 不进 Manifest。

    Decimal 无法被 stdlib json 序列化（历史事故：Decimal 直接炸 INSERT），
    而且它属于检索调参，不是语义定义。
    """
    manifest = compileFromRows(
        classes=[], properties=[], joins=[], kpis=[makeKpi("K1", "x")]
    )

    kpi = manifest["kpis"][0]
    assert "matchThreshold" not in kpi
    assert "match_threshold" not in kpi
    canonicalJson(manifest)  # 不抛 TypeError 即通过


# ---------------------------------------------------------------------------
# 9. 业务对象 / 数据域（D6）
# ---------------------------------------------------------------------------


def test_business_object_compiles_header_primary_and_attached_domains() -> None:
    classes = [makeClass(1, "Supplier")]
    bo = makeBusinessObject("SUPPLIER", "供应商", 1, "PROCUREMENT", graphLabel="Supplier")
    attached = [
        BusinessObjectDomain(bo_code="SUPPLIER", domain_id="QUALITY"),
        BusinessObjectDomain(bo_code="SUPPLIER", domain_id="LOGISTICS"),
    ]
    domains = [makeDomain("PROCUREMENT", "采购域"), makeDomain("LOGISTICS", "物流域"),
               makeDomain("QUALITY", "质量域")]

    manifest = compileFromRows(
        classes=classes,
        properties=[],
        joins=[],
        businessObjects=[bo],
        businessObjectDomains=attached,
        domains=domains,
    )

    assert [d["code"] for d in manifest["domains"]] == ["LOGISTICS", "PROCUREMENT", "QUALITY"]
    [compiled] = manifest["businessObjects"]
    assert compiled == {
        "code": "SUPPLIER",
        "name": "供应商",
        "headerClassName": "Supplier",
        "primaryDomainCode": "PROCUREMENT",
        "attachedDomainCodes": ["LOGISTICS", "QUALITY"],
        "graphLabel": "Supplier",
        "description": None,
    }


def test_domain_keeps_enabled_flag_in_snapshot() -> None:
    manifest = compileFromRows(
        classes=[],
        properties=[],
        joins=[],
        domains=[makeDomain("A", "甲", enabled=False), makeDomain("B", "乙", description="说明")],
    )

    assert manifest["domains"] == [
        {"code": "A", "name": "甲", "description": None, "enabled": False},
        {"code": "B", "name": "乙", "description": "说明", "enabled": True},
    ]


def test_business_object_with_tombstoned_header_raises() -> None:
    """BO 的身份就是 header 类 —— header 已删时不能编译成 null，只能报错。"""
    tombstone = makeClass(2, "Legacy", validTo=datetime(2026, 1, 1, tzinfo=UTC))

    with pytest.raises(ValueError, match="header_class_id=2"):
        compileFromRows(
            classes=[tombstone],
            properties=[],
            joins=[],
            businessObjects=[makeBusinessObject("LEGACY", "旧对象", 2, "PROCUREMENT")],
            domains=[makeDomain("PROCUREMENT", "采购域")],
        )


def test_business_object_with_unknown_header_or_domain_raises() -> None:
    with pytest.raises(ValueError, match="header_class_id=9"):
        compileFromRows(
            classes=[makeClass(1, "Supplier")],
            properties=[],
            joins=[],
            businessObjects=[makeBusinessObject("X", "x", 9, "PROCUREMENT")],
            domains=[makeDomain("PROCUREMENT", "采购域")],
        )
    with pytest.raises(ValueError, match="primary_domain_id=NOPE"):
        compileFromRows(
            classes=[makeClass(1, "Supplier")],
            properties=[],
            joins=[],
            businessObjects=[makeBusinessObject("X", "x", 1, "NOPE")],
            domains=[makeDomain("PROCUREMENT", "采购域")],
        )
    with pytest.raises(ValueError, match="NOPE"):
        compileFromRows(
            classes=[makeClass(1, "Supplier")],
            properties=[],
            joins=[],
            businessObjects=[makeBusinessObject("X", "x", 1, "PROCUREMENT")],
            businessObjectDomains=[BusinessObjectDomain(bo_code="X", domain_id="NOPE")],
            domains=[makeDomain("PROCUREMENT", "采购域")],
        )


# ---------------------------------------------------------------------------
# 10. 新分区同样与输入顺序无关
# ---------------------------------------------------------------------------


def test_d6_sections_are_byte_identical_regardless_of_input_order() -> None:
    classes = [makeClass(1, "Supplier")]
    metricA = makeMetric(10, "zetaMetric")
    metricB = makeMetric(11, "alphaMetric")
    kpiA = makeKpi("K2", "乙", metricId=11)
    kpiB = makeKpi("K1", "甲", metricId=10)
    domainA = makeDomain("Z", "z")
    domainB = makeDomain("A", "a")

    forward = compileFromRows(
        classes=classes,
        properties=[],
        joins=[],
        metrics=[metricA, metricB],
        kpis=[kpiA, kpiB],
        domains=[domainA, domainB],
    )
    backward = compileFromRows(
        classes=classes,
        properties=[],
        joins=[],
        metrics=[metricB, metricA],
        kpis=[kpiB, kpiA],
        domains=[domainB, domainA],
    )

    assert canonicalJson(forward) == canonicalJson(backward)
    assert [m["metricName"] for m in forward["metrics"]] == ["alphaMetric", "zetaMetric"]
    assert [k["kpiCode"] for k in forward["kpis"]] == ["K1", "K2"]
    assert [d["code"] for d in forward["domains"]] == ["A", "Z"]
