"""语义编译器核心（P1a / D5）：治理表行 → 规范化 Manifest。

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

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty
from app.services.semantic_compiler import compileFromRows


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


# ---------------------------------------------------------------------------
# 1. 空输入
# ---------------------------------------------------------------------------


def test_empty_input_produces_minimal_manifest() -> None:
    manifest = compileFromRows(classes=[], properties=[], joins=[])

    assert manifest == {
        "schemaVersion": "v1",
        "classes": [],
        "joins": [],
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
