"""P2：快照复原视图 vs ORM 实体路径的**逐字等价**golden（D14 / plan §4-③）。

为什么必须有这个文件
-------------------
D12 交付的复原层在 `daily/D12.md` F3 里声称「子集外的引用 = None ⇒ 与 ORM 路径
同口径」。这条不变量此前**只有自证**：拿复原视图自己的输出断言它「等于 ORM 路径」，
没有任何一条测试把**同一份数据**分别经两条路径渲染后对比。

评审（code-reviewer HIGH-1）进一步指出：ORM 路径的父类 / 外键解析有**两个变体**，
取决于会话里 `ref_class` / `parent` 关系有没有被加载：

- **变体 A（子集内解析）**：`prop.__dict__` 里**没有** `ref_class`、
  `cls.__dict__` 里**没有** `parent` ⇒ 渲染器回退到
  `classesById.get(ref_class_id / parent_class_id)` ⇒ 子集外的目标解析为 `None`。
- **变体 B（关系已加载）**：`ref_class` / `parent` 已在 `__dict__` 里 ⇒
  FK 目标表**不受子集限制**；父类块**即使不在子集内也会被渲染**。

生产 chat / NL2SQL 主路径走的是**变体 A**，实测依据：

- `OntologyService.listClasses` 用 `selectinload(OntologyClass.properties)`，
  实测 `ref_class` 加载数 **0 / 3267**、`parent` 不在 `cls.__dict__`；
- 主路径上没有任何地方把 `OntologyProperty` 当**主实体** select
  （`agent_tools_nl2sql.describe_table` / `data_quality_rule_*` 那几处都不在
  schema 文本路径上；后者自己拼 schema 文本，不走 `buildSchemaText`）。

⇒ 本文件把「变体 A」**钉成契约**：同一份数据，两条路径渲染出的 schema 文本
必须逐字相同。变体 B 是 ORM 路径的**潜在非确定性**（同一段代码换个会话就变），
已登记为 D12 F5，不在 P2 里改渲染器。

数据构造用的是**真实 ORM 实体**（`OntologyClass` / `OntologyProperty` /
`OntologyJoin`），但**不落库**：本文件验证的是「编译器输出 → 复原层 → 渲染器」
这一段纯函数链的映射正确性，不需要真实库。真实库上的证据是
`scripts/semantic_schema_shadow_diff.py`（D13 影子比对）。
"""

from __future__ import annotations

from collections import Counter

import pytest

from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty
from app.services.nl2sql_schema import buildSchemaText
from app.services.semantic_compiler import compileFromRows
from app.services.semantic_schema_adapter import restoreSnapshotSchema

# ---------------------------------------------------------------------------
# 数据构造：真实 ORM 实体（不落库）
# ---------------------------------------------------------------------------

# 类 id：Supplier=1 / PurchaseOrder=2 / Material=3 / Warehouse=4 / Receipt=5
SUPPLIER_ID = 1
PURCHASE_ORDER_ID = 2
MATERIAL_ID = 3
WAREHOUSE_ID = 4
RECEIPT_ID = 5


def _ormProperty(
    propId: int,
    classId: int,
    name: str,
    *,
    alias: str | None = None,
    business_aliases: list[str] | None = None,
    description: str | None = None,
    dataType: str = "STRING",
    isPk: bool = False,
    isFk: bool = False,
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
        id=propId,
        class_id=classId,
        property_name=name,
        property_alias=alias,
        business_aliases=business_aliases,
        description=description,
        data_type=dataType,
        is_primary_key=isPk,
        is_foreign_key=isFk,
        ref_class_id=refClassId,
        source_column=sourceColumn,
        unit=unit,
        allowed_values=allowedValues,
        is_not_null=isNotNull,
        min_value=minValue,
        max_value=maxValue,
        regex_pattern=regexPattern,
    )


def _ormClass(
    classId: int,
    name: str,
    *,
    alias: str | None = None,
    description: str | None = None,
    sourceTable: str | None = None,
    parentClassId: int | None = None,
    properties: tuple[OntologyProperty, ...] = (),
) -> OntologyClass:
    cls = OntologyClass(
        id=classId,
        class_name=name,
        class_alias=alias,
        description=description,
        source_table=sourceTable,
        object_type=None,
        object_owner=None,
        parent_class_id=parentClassId,
        valid_to=None,
    )
    # 赋值走 back_populates：与 ORM 加载后的形态一致（properties 在 __dict__ 里）。
    cls.properties = list(properties)
    return cls


def _dataset() -> tuple[list[OntologyClass], list[OntologyProperty], list[OntologyJoin]]:
    """一套能同时覆盖「继承 / 外键 / JOIN / 子集外引用」的数据。

    属性顺序**刻意不按名字排**（与生产物理行序同形）—— D12 F4 的列序差异
    曾让本文件只能判「内容等价」；A4 起两侧在渲染期共用排序键 ⇒ 可以（并且
    必须）**逐字节**判。
    """
    supplierProps = (
        _ormProperty(101, SUPPLIER_ID, "SUPPLIER_NAME", alias="供应商名称",
                     business_aliases=["供方名称"], description="供方全称",
                     sourceColumn="SUPPLIER_NAME"),
        _ormProperty(102, SUPPLIER_ID, "SUPPLIER_CODE", isPk=True,
                     sourceColumn="SUPPLIER_CODE"),
    )
    orderProps = (
        _ormProperty(201, PURCHASE_ORDER_ID, "SUPPLIER_CODE", isFk=True,
                     refClassId=SUPPLIER_ID, sourceColumn="SUPPLIER_CODE"),
        _ormProperty(202, PURCHASE_ORDER_ID, "ORDER_CODE", isPk=True,
                     sourceColumn="ORDER_CODE"),
    )
    materialProps = (
        _ormProperty(301, MATERIAL_ID, "MATERIAL_CODE", isPk=True,
                     sourceColumn="MATERIAL_CODE"),
    )
    warehouseProps = (
        _ormProperty(401, WAREHOUSE_ID, "WAREHOUSE_CODE", isPk=True,
                     sourceColumn="WAREHOUSE_CODE"),
    )
    receiptProps = (
        # 外键指向 Warehouse —— 召回子集里没有 Warehouse ⇒ 子集外引用
        _ormProperty(501, RECEIPT_ID, "WAREHOUSE_CODE", isFk=True,
                     refClassId=WAREHOUSE_ID, sourceColumn="WAREHOUSE_CODE"),
        _ormProperty(502, RECEIPT_ID, "RECEIPT_CODE", isPk=True,
                     sourceColumn="RECEIPT_CODE"),
    )

    classes = [
        _ormClass(SUPPLIER_ID, "Supplier", alias="供应商", description="供应商主数据",
                  sourceTable="SUPPLIER", properties=supplierProps),
        _ormClass(PURCHASE_ORDER_ID, "PurchaseOrder", alias="采购订单",
                  sourceTable="PURCHASE_ORDER", parentClassId=SUPPLIER_ID,
                  properties=orderProps),
        _ormClass(MATERIAL_ID, "Material", sourceTable="MATERIAL",
                  properties=materialProps),
        _ormClass(WAREHOUSE_ID, "Warehouse", sourceTable="WAREHOUSE",
                  properties=warehouseProps),
        _ormClass(RECEIPT_ID, "Receipt", sourceTable="RECEIPT",
                  properties=receiptProps),
    ]
    properties = [prop for cls in classes for prop in cls.properties]
    joins = [
        OntologyJoin(
            id=1,
            source_class_id=PURCHASE_ORDER_ID,
            source_columns=["SUPPLIER_CODE"],
            target_class_id=SUPPLIER_ID,
            target_columns=["SUPPLIER_CODE"],
            join_type="LEFT",
            relation_type="foreign_key",
            description="订单挂供方",
            join_key="PURCHASE_ORDER_SUPPLIER",
        )
    ]
    return classes, properties, joins


def _manifestOf(classes, properties, joins) -> dict:
    """走**真实编译器**（纯函数，不碰库）产快照。"""
    return compileFromRows(classes=classes, properties=properties, joins=joins)


def _renderBoth(classNames: list[str]):
    """同一份数据经两条路径渲染，返回 `(ormText, snapshotText)`。"""
    classes, properties, joins = _dataset()
    byName = {cls.class_name: cls for cls in classes}
    subsetOrm = [byName[name] for name in classNames]
    manifest = _manifestOf(classes, properties, joins)
    restored, restoredJoins = restoreSnapshotSchema(manifest, classNames)
    ormText = buildSchemaText(subsetOrm, joins=joins)
    snapshotText = buildSchemaText(restored, joins=restoredJoins)
    return ormText, snapshotText


def _assertByteIdentical(ormText: str, snapshotText: str) -> None:
    """**逐字节**相等（A4 起；此前只判行多重集，因为两侧列序不同）。

    强于内容等价：内容等价会放过多余的**排序差异**，而排序差异正是
    G3 第 2 条不过的根因（F14：内容等价 ≠ 顺序等价）。
    失败时先报行多重集差（定位「少了/多了哪行」），再报逐字节不等。
    """
    if ormText != snapshotText:
        onlyOrm = Counter(ormText.splitlines()) - Counter(snapshotText.splitlines())
        onlySnapshot = Counter(snapshotText.splitlines()) - Counter(ormText.splitlines())
        pytest.fail(
            "两条路径的 schema 文本必须逐字节相同（开关切换 = prompt 恒等变换）；"
            f"仅直读有 {sum(onlyOrm.values())} 行（示例 {list(onlyOrm)[:3]}），"
            f"仅快照有 {sum(onlySnapshot.values())} 行（示例 {list(onlySnapshot)[:3]}）"
        )


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


class Test与ORM路径等价:
    """变体 A（子集内解析）钉成契约。"""

    def test_全量类集两条路径逐字节一致(self) -> None:
        ormText, snapshotText = _renderBoth(
            ["Supplier", "PurchaseOrder", "Material", "Warehouse", "Receipt"]
        )
        _assertByteIdentical(ormText, snapshotText)

    def test_召回子集两条路径逐字节一致(self) -> None:
        ormText, snapshotText = _renderBoth(["PurchaseOrder", "Receipt"])
        _assertByteIdentical(ormText, snapshotText)

    def test_单类子集两条路径逐字节一致(self) -> None:
        ormText, snapshotText = _renderBoth(["Receipt"])
        _assertByteIdentical(ormText, snapshotText)

    def test_子类单独成集时父类不出现在两条路径的文本里(self) -> None:
        """变体 A 的核心断言：子集外的父类**不**被拉进来渲染。"""
        ormText, snapshotText = _renderBoth(["PurchaseOrder"])
        assert "继承 Supplier" not in ormText
        assert "### Supplier" not in ormText
        assert "继承 Supplier" not in snapshotText
        assert "### Supplier" not in snapshotText

    def test_子集外的外键目标两条路径都不渲染目标表(self) -> None:
        """变体 A 的核心断言：子集外的 FK 目标只出列名，不出目标表。"""
        ormText, snapshotText = _renderBoth(["Receipt"])
        assert "WAREHOUSE_CODE: STRING [FK]" in ormText
        assert "FK → WAREHOUSE" not in ormText
        assert "WAREHOUSE_CODE: STRING [FK]" in snapshotText
        assert "FK → WAREHOUSE" not in snapshotText

    def test_ORM实体未被挂上惰性关系时复原路径与之同形(self) -> None:
        """前提守卫：本文件的等价性建立在「ORM 实体上 `ref_class` / `parent` 未加载」之上。

        若哪天 ORM 实体的形态变了（例如 `listClasses` 改用会加载关系的加载器），
        这条会先红 —— 提醒复核 D12 F5 的变体判定，而不是让等价断言悄悄失效。
        """
        classes, _properties, _joins = _dataset()
        for cls in classes:
            assert "parent" not in cls.__dict__
            for prop in cls.properties:
                assert "ref_class" not in prop.__dict__


class Test复原视图与ORM实体字段等价:
    """plan §3.1 的**窄字段面**逐字段比对 —— 复原层不得丢字段、串字段。"""

    def test_类字段逐项相等(self) -> None:
        classes, properties, joins = _dataset()
        manifest = _manifestOf(classes, properties, joins)
        restored, _ = restoreSnapshotSchema(manifest, ["Supplier", "PurchaseOrder"])
        byName = {cls.class_name: cls for cls in classes}
        for view in restored:
            orm = byName[view.class_name]
            assert view.class_alias == orm.class_alias
            assert view.description == orm.description
            assert view.source_table == orm.source_table
            assert view.object_type == orm.object_type
            assert view.object_owner == orm.object_owner

    def test_属性字段逐项相等(self) -> None:
        classes, properties, joins = _dataset()
        manifest = _manifestOf(classes, properties, joins)
        restored, _ = restoreSnapshotSchema(manifest, ["Supplier", "PurchaseOrder"])
        byName = {cls.class_name: cls for cls in classes}
        for view in restored:
            ormProps = {p.property_name: p for p in byName[view.class_name].properties}
            for prop in view.properties:
                orm = ormProps[prop.property_name]
                assert prop.property_alias == orm.property_alias
                assert prop.business_aliases == tuple(orm.business_aliases or [])
                assert prop.description == orm.description
                assert prop.data_type == orm.data_type
                assert prop.is_primary_key == orm.is_primary_key
                assert prop.is_foreign_key == orm.is_foreign_key
                assert prop.source_column == orm.source_column
                assert prop.unit == orm.unit
                assert prop.allowed_values == tuple(orm.allowed_values or [])
                assert prop.is_not_null == orm.is_not_null
                assert prop.min_value == orm.min_value
                assert prop.max_value == orm.max_value
                assert prop.regex_pattern == orm.regex_pattern

    def test_子集内引用解析到同一个目标类(self) -> None:
        classes, properties, joins = _dataset()
        manifest = _manifestOf(classes, properties, joins)
        restored, _ = restoreSnapshotSchema(manifest, ["Supplier", "PurchaseOrder"])
        byName = {cls.class_name: cls for cls in restored}
        order = byName["PurchaseOrder"]
        assert order.parent_class_id == byName["Supplier"].id
        fk = next(p for p in order.properties if p.property_name == "SUPPLIER_CODE")
        assert fk.ref_class_id == byName["Supplier"].id


class Test子集不变量:
    """plan §4-⑦：召回子集**不因快照而扩大**（条数 + 类名清单逐项相等）。"""

    def test_复原后的类名清单与传入子集逐项相等(self) -> None:
        subset = ["Material", "Supplier", "PurchaseOrder"]
        classes, properties, joins = _dataset()
        manifest = _manifestOf(classes, properties, joins)
        restored, _ = restoreSnapshotSchema(manifest, subset)
        assert [c.class_name for c in restored] == subset

    def test_复原后的类数等于传入子集类数(self) -> None:
        subset = ["Material", "Supplier"]
        classes, properties, joins = _dataset()
        manifest = _manifestOf(classes, properties, joins)
        restored, _ = restoreSnapshotSchema(manifest, subset)
        assert len(restored) == len(subset)

    def test_子集外的类不会混进复原结果(self) -> None:
        classes, properties, joins = _dataset()
        manifest = _manifestOf(classes, properties, joins)
        restored, _ = restoreSnapshotSchema(manifest, ["Material"])
        assert "Warehouse" not in {c.class_name for c in restored}


class Testid映射稳定性:
    """合成 id 必须是 manifest **内容**的常量映射，不受清单顺序影响。"""

    def test_打乱manifest类顺序不改变类id映射(self) -> None:
        classes, properties, joins = _dataset()
        manifest = _manifestOf(classes, properties, joins)
        shuffled = dict(manifest)
        shuffled["classes"] = list(reversed(manifest["classes"]))
        first, _ = restoreSnapshotSchema(manifest, ["Supplier", "PurchaseOrder"])
        second, _ = restoreSnapshotSchema(shuffled, ["Supplier", "PurchaseOrder"])
        assert {c.class_name: c.id for c in first} == {c.class_name: c.id for c in second}

    def test_打乱manifest属性顺序不改变属性id映射(self) -> None:
        classes, properties, joins = _dataset()
        manifest = _manifestOf(classes, properties, joins)
        shuffled = dict(manifest)
        shuffledClasses = []
        for raw in manifest["classes"]:
            copied = dict(raw)
            copied["properties"] = list(reversed(raw["properties"]))
            shuffledClasses.append(copied)
        shuffled["classes"] = shuffledClasses
        first, _ = restoreSnapshotSchema(manifest, ["Supplier"])
        second, _ = restoreSnapshotSchema(shuffled, ["Supplier"])
        firstIds = {p.property_name: p.id for p in first[0].properties}
        secondIds = {p.property_name: p.id for p in second[0].properties}
        assert firstIds == secondIds
