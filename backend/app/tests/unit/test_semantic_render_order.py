"""A4：渲染顺序统一 —— 直读侧与快照侧共享排序键（M0.4 方案 B）。

为什么需要这个文件
-------------------
G3 第 2 条（answer 级评估）不过的根因只有两条顺序轴：

1. 直读侧列序 = `OntologyClass.properties` 关系的**物理行序**（`relationship`
   没有 `order_by`）⇒ 连自己和自己都不保证一致；
2. JOIN 行序 = 调用方传入 `joins` 的顺序（召回 / 管理器给什么就渲染什么）。

快照侧则由语义编译器按 `propertyName` / `joinKey` 排好序送出。⇒ 开闸 = 静默
换 prompt，模型看到的重点顺序变了，SQL 跟着变。

A4 的修法是**共用一个键**（`semantic_manifest_common.propertyNameSortKey` /
`joinKeySortKey`）：编译侧按它产 manifest，渲染侧按它排直读输入。两侧因此
逐字节相同，开闸变成对 prompt 的**恒等变换**。

本文件钉三件事（M0.4 §3.1 的 V1 / V3 / V4）：

- **V1**：打乱属性 / JOIN 的输入顺序，渲染结果逐字节相同（3 次）；
- **V3**：渲染顺序 == 编译器 manifest 顺序 == 按共享键排序的结果；
- **V4**：排序键缺值（`None`）时排末尾、不抛错（构造实体可能缺名）。

V2（直读 vs 快照逐字节）在 `test_semantic_schema_equivalence.py` 与
`test_semantic_schema_gate.py`；本文件**只**管排序本身，不碰开关。
"""

from __future__ import annotations

from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty
from app.services.nl2sql_schema import buildSchemaText
from app.services.semantic_compiler import compileFromRows
from app.services.semantic_manifest_common import joinKeySortKey, propertyNameSortKey

# 类 id：Supplier=1 / PurchaseOrder=2 / Material=3 / Warehouse=4
SUPPLIER_ID = 1
PURCHASE_ORDER_ID = 2
MATERIAL_ID = 3
WAREHOUSE_ID = 4


# ---------------------------------------------------------------------------
# 数据构造：真实 ORM 实体（不落库）
# ---------------------------------------------------------------------------


def _prop(
    propId: int,
    classId: int,
    name: str | None,
    *,
    isPk: bool = False,
    isFk: bool = False,
    refClassId: int | None = None,
    description: str | None = None,
) -> OntologyProperty:
    return OntologyProperty(
        id=propId,
        class_id=classId,
        property_name=name,
        property_alias=None,
        business_aliases=None,
        description=description,
        data_type="STRING",
        is_primary_key=isPk,
        is_foreign_key=isFk,
        ref_class_id=refClassId,
        # 与列名同名 ⇒ 渲染端不追加 `(column=…)` 回声，列名解析更干净
        source_column=name,
        unit=None,
        allowed_values=None,
        is_not_null=None,
        min_value=None,
        max_value=None,
        regex_pattern=None,
    )


def _cls(
    classId: int,
    name: str,
    *,
    sourceTable: str,
    properties: tuple[OntologyProperty, ...],
) -> OntologyClass:
    cls = OntologyClass(
        id=classId,
        class_name=name,
        class_alias=None,
        description=None,
        source_table=sourceTable,
        object_type=None,
        object_owner=None,
        parent_class_id=None,
        valid_to=None,
    )
    cls.properties = list(properties)
    return cls


def _join(
    joinId: int,
    joinKey: str | None,
    sourceClassId: int,
    sourceColumn: str,
    targetClassId: int,
    targetColumn: str,
    description: str,
) -> OntologyJoin:
    return OntologyJoin(
        id=joinId,
        source_class_id=sourceClassId,
        source_columns=[sourceColumn],
        target_class_id=targetClassId,
        target_columns=[targetColumn],
        join_type="LEFT",
        relation_type="foreign_key",
        description=description,
        join_key=joinKey,
    )


# 属性输入顺序刻意「名字序 / id 序 / 输入序」三者全不同；
# JOIN 输入顺序同样不按 joinKey 排。这样任一侧退回原顺序，断言都会红。
_PROPERTY_INPUT_ORDER = {
    SUPPLIER_ID: ("SUPPLIER_NAME", "CREDIT_AMT", "SUPPLIER_CODE"),
    PURCHASE_ORDER_ID: ("SUPPLIER_CODE", "ORDER_CODE"),
}
_PROPERTY_SORTED_ORDER = {
    SUPPLIER_ID: ["CREDIT_AMT", "SUPPLIER_CODE", "SUPPLIER_NAME"],
    PURCHASE_ORDER_ID: ["ORDER_CODE", "SUPPLIER_CODE"],
}
_DESCRIPTION_BY_JOIN_KEY = {
    "A_PO_TO_MATERIAL": "订单挂物料",
    "B_MATERIAL_TO_WAREHOUSE": "物料挂仓库",
    "M_SUPPLIER_TO_MATERIAL": "供方挂物料",
    "Z_PO_TO_SUPPLIER": "订单挂供方",
}


def _dataset(
    *, propertyOrder: dict[int, tuple[str | None, ...]] | None = None
) -> tuple[list[OntologyClass], list[OntologyProperty], list[OntologyJoin]]:
    """4 类 / 5 属性 / 4 条 JOIN 的数据集。

    形状覆盖三条排序轴：类内多列、JOIN 跨类、以及一条 2 跳间接路径
    （Supplier → Material → Warehouse，走 `_buildIndirectJoinHints`）。
    """
    order = propertyOrder or _PROPERTY_INPUT_ORDER
    nextPropId = 100
    propsByClass: dict[int, list[OntologyProperty]] = {}
    for classId, names in order.items():
        names = tuple(names)
        propsByClass[classId] = [
            _prop(
                nextPropId + index,
                classId,
                name,
                isPk=(name == "SUPPLIER_CODE" or name == "ORDER_CODE")
                and classId != PURCHASE_ORDER_ID,
                isFk=(name == "SUPPLIER_CODE" and classId == PURCHASE_ORDER_ID),
                refClassId=SUPPLIER_ID if classId == PURCHASE_ORDER_ID and name == "SUPPLIER_CODE" else None,
                # 让摘录段（`_buildCriticalColumnsDigest`）有内容 ⇒ V1 顺带证明
                # 摘要段对输入顺序不敏感（该段内部按限定名排序）
                description="信用额度上限（仅统计未作废订单的采购金额）" if name == "CREDIT_AMT" else None,
            )
            for index, name in enumerate(names)
        ]
    propsByClass.setdefault(MATERIAL_ID, [_prop(301, MATERIAL_ID, "MATERIAL_CODE")])
    propsByClass.setdefault(WAREHOUSE_ID, [_prop(401, WAREHOUSE_ID, "WAREHOUSE_CODE")])

    classes = [
        _cls(SUPPLIER_ID, "Supplier", sourceTable="SUPPLIER", properties=tuple(propsByClass[SUPPLIER_ID])),
        _cls(
            PURCHASE_ORDER_ID,
            "PurchaseOrder",
            sourceTable="PURCHASE_ORDER",
            properties=tuple(propsByClass[PURCHASE_ORDER_ID]),
        ),
        _cls(MATERIAL_ID, "Material", sourceTable="MATERIAL", properties=tuple(propsByClass[MATERIAL_ID])),
        _cls(WAREHOUSE_ID, "Warehouse", sourceTable="WAREHOUSE", properties=tuple(propsByClass[WAREHOUSE_ID])),
    ]
    properties = [prop for cls in classes for prop in cls.properties]

    joins = [
        _join(1, "Z_PO_TO_SUPPLIER", PURCHASE_ORDER_ID, "SUPPLIER_CODE", SUPPLIER_ID, "SUPPLIER_CODE", _DESCRIPTION_BY_JOIN_KEY["Z_PO_TO_SUPPLIER"]),
        _join(2, "A_PO_TO_MATERIAL", PURCHASE_ORDER_ID, "ORDER_CODE", MATERIAL_ID, "MATERIAL_CODE", _DESCRIPTION_BY_JOIN_KEY["A_PO_TO_MATERIAL"]),
        _join(3, "M_SUPPLIER_TO_MATERIAL", SUPPLIER_ID, "SUPPLIER_CODE", MATERIAL_ID, "MATERIAL_CODE", _DESCRIPTION_BY_JOIN_KEY["M_SUPPLIER_TO_MATERIAL"]),
        _join(4, "B_MATERIAL_TO_WAREHOUSE", MATERIAL_ID, "MATERIAL_CODE", WAREHOUSE_ID, "WAREHOUSE_CODE", _DESCRIPTION_BY_JOIN_KEY["B_MATERIAL_TO_WAREHOUSE"]),
    ]
    return classes, properties, joins


def _shuffles(rows: list) -> list[list]:
    """3 个确定性的输入顺序（身份 / 倒序 / 左旋一位）—— 不用随机，失败可复现。"""
    rotated = rows[1:] + rows[:1]
    return [list(rows), list(reversed(rows)), rotated]


def _render(classes, properties, joins) -> str:
    """模拟直读侧：属性按给定输入顺序挂到类上，JOIN 按给定输入顺序传入。"""
    byClass: dict[int, list[OntologyProperty]] = {}
    for prop in properties:
        byClass.setdefault(prop.class_id, []).append(prop)
    for cls in classes:
        cls.properties = list(byClass.get(cls.id, []))
    return buildSchemaText(classes, joins=joins)


def _columnOrder(text: str, className: str) -> list[str]:
    """取某个类块里的列名顺序（未命名的列返回空串）。"""
    order: list[str] = []
    inBlock = False
    for line in text.splitlines():
        if line.startswith("### "):
            inBlock = line.startswith(f"### {className}")
            continue
        if inBlock and line.startswith("    ") and ":" in line:
            order.append(line.strip().split(":")[0].strip())
    return order


def _joinDescriptionOrder(
    text: str, descriptions: list[str] | None = None
) -> list[str]:
    """按渲染顺序取 JOIN 行的描述（描述是这几条 JOIN 唯一的区分标识）。"""
    found: list[tuple[int, str]] = []
    for description in descriptions or list(_DESCRIPTION_BY_JOIN_KEY.values()):
        index = text.find(description)
        if index >= 0:
            found.append((index, description))
    return [description for _index, description in sorted(found)]


# ---------------------------------------------------------------------------
# V1：输入顺序无关 ⇒ 渲染逐字节稳定
# ---------------------------------------------------------------------------


class TestV1输入顺序无关:
    def test_打乱属性与JOIN输入顺序三次渲染逐字节相同(self) -> None:
        classes, properties, joins = _dataset()
        texts = [
            _render(classes, list(permutedProps), list(permutedJoins))
            for permutedProps, permutedJoins in zip(
                _shuffles(properties), _shuffles(joins), strict=True
            )
        ]
        assert texts[0] == texts[1] == texts[2], (
            "同一份数据换输入顺序必须渲染出逐字节相同的 schema 文本"
            "（否则开闸 = 静默换 prompt）"
        )

    def test_打乱类列表顺序渲染逐字节相同(self) -> None:
        """类块顺序由 `_topoSortByInheritance` 按 `(class_name, id)` 决定，与输入无关。"""
        classes, properties, joins = _dataset()
        texts = [
            _render(list(permutedClasses), list(properties), list(joins))
            for permutedClasses in _shuffles(classes)
        ]
        assert texts[0] == texts[1] == texts[2]

    def test_摘要段也随输入顺序稳定(self) -> None:
        """`_buildCriticalColumnsDigest` 在类块之前输出，同样不能受行序影响。"""
        classes, properties, joins = _dataset()
        texts = [_render(classes, list(p), list(j)) for p, j in zip(_shuffles(properties), _shuffles(joins), strict=True)]
        digests = [text.split("\n### ", 1)[0] for text in texts]
        assert digests[0] == digests[1] == digests[2]
        assert "信用额度上限" in digests[0], "摘要段必须真的渲染出内容（否则本条是空集通过）"


# ---------------------------------------------------------------------------
# V3：渲染顺序 == 编译器顺序 == 按共享键排序
# ---------------------------------------------------------------------------


class TestV3与编译器同序:
    @staticmethod
    def _manifestOf(classes, properties, joins) -> dict:
        return compileFromRows(classes=classes, properties=properties, joins=joins)

    def test_列渲染顺序等于编译器manifest的属性顺序(self) -> None:
        classes, properties, joins = _dataset()
        manifest = self._manifestOf(classes, properties, joins)
        text = _render(classes, list(properties), list(joins))

        manifestOrder = {
            cls["className"]: [p["propertyName"] for p in cls["properties"]]
            for cls in manifest["classes"]
        }
        for className in ("Supplier", "PurchaseOrder"):
            assert _columnOrder(text, className) == manifestOrder[className], (
                f"{className} 的渲染列序必须等于编译器 manifest 顺序"
            )
        assert manifestOrder["Supplier"] == _PROPERTY_SORTED_ORDER[SUPPLIER_ID]
        assert manifestOrder["PurchaseOrder"] == _PROPERTY_SORTED_ORDER[PURCHASE_ORDER_ID]

    def test_JOIN渲染顺序等于编译器manifest的JOIN顺序(self) -> None:
        classes, properties, joins = _dataset()
        manifest = self._manifestOf(classes, properties, joins)
        text = _render(classes, list(properties), list(joins))

        assert _joinDescriptionOrder(text) == [
            _DESCRIPTION_BY_JOIN_KEY[join["joinKey"]] for join in manifest["joins"]
        ]
        # 编译器的 JOIN 顺序必须就是共享排序键的顺序（含缺键兜底）
        assert [join["joinKey"] for join in manifest["joins"]] == sorted(
            _DESCRIPTION_BY_JOIN_KEY, key=joinKeySortKey
        )

    def test_两侧的排序键就是共享原语(self) -> None:
        """反向守卫：键一旦只改一侧，本条立刻红。

        `propertyNameSortKey` / `joinKeySortKey` 是**唯一**的排序真源 ——
        编译器与渲染器都 import 它。这里直接按原语排序，与两侧的实际产出对照。
        """
        classes, properties, joins = _dataset()
        manifest = self._manifestOf(classes, properties, joins)
        text = _render(classes, list(properties), list(joins))

        expectedProps = sorted(_PROPERTY_INPUT_ORDER[SUPPLIER_ID], key=propertyNameSortKey)
        supplierManifest = next(
            cls for cls in manifest["classes"] if cls["className"] == "Supplier"
        )
        assert [p["propertyName"] for p in supplierManifest["properties"]] == expectedProps
        assert _columnOrder(text, "Supplier") == expectedProps
        assert [join["joinKey"] for join in manifest["joins"]] == sorted(
            _DESCRIPTION_BY_JOIN_KEY.keys(), key=joinKeySortKey
        )


# ---------------------------------------------------------------------------
# V4：排序键缺值 ⇒ 排末尾、不抛错（构造实体，DB 列 NOT NULL）
# ---------------------------------------------------------------------------


class TestV4缺键兜底:
    def test_缺名属性排末尾且不抛错(self) -> None:
        classes, properties, joins = _dataset()
        classes[0].properties = [
            _prop(910, SUPPLIER_ID, "SUPPLIER_CODE"),
            _prop(911, SUPPLIER_ID, None),
            _prop(912, SUPPLIER_ID, "CREDIT_AMT"),
        ]
        text = buildSchemaText(classes, joins=joins)
        assert _columnOrder(text, "Supplier") == ["CREDIT_AMT", "SUPPLIER_CODE", ""], (
            "缺名属性必须排在最后，而不是让整段渲染挂掉"
        )

    def test_缺join_key的JOIN排末尾且不抛错(self) -> None:
        classes, properties, joins = _dataset()
        joins.append(
            _join(9, None, MATERIAL_ID, "MATERIAL_CODE", WAREHOUSE_ID, "WAREHOUSE_CODE", "缺键JOIN")
        )
        text = buildSchemaText(classes, joins=joins)
        order = _joinDescriptionOrder(
            text, [*_DESCRIPTION_BY_JOIN_KEY.values(), "缺键JOIN"]
        )
        assert len(order) == 5, "其余 JOIN 仍应全部渲染（缺键不能导致整段丢失）"
        assert order[-1] == "缺键JOIN", "缺键的 JOIN 必须排末尾"
        assert order == [
            _DESCRIPTION_BY_JOIN_KEY[joinKey]
            for joinKey in sorted(_DESCRIPTION_BY_JOIN_KEY, key=joinKeySortKey)
        ] + ["缺键JOIN"]
